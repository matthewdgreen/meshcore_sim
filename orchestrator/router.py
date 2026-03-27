"""
router.py — PacketRouter: receives TX callbacks from all nodes and schedules
delivery to neighbours, honouring link loss, latency, and adversarial filters.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from .adversarial import AdversarialFilter
from .airtime import lora_airtime_ms
from .channel import ChannelModel
from .config import RadioConfig
from .metrics import MetricsCollector
from .node import NodeAgent
from .topology import EdgeLink, Topology
from .tracer import PacketTracer

log = logging.getLogger(__name__)

# Interval at which the replay-drainer polls adversarial nodes (seconds)
_REPLAY_POLL_INTERVAL = 0.05


class PacketRouter:
    """
    Central dispatcher.  Instantiated once, wires tx_callback and
    event_callback onto every NodeAgent, then provides coroutines that
    run as background tasks.
    """

    def __init__(
        self,
        topology: Topology,
        agents: dict[str, NodeAgent],
        metrics: MetricsCollector,
        rng: random.Random,
        tracer: Optional[PacketTracer] = None,
        radio: Optional[RadioConfig] = None,
        channel: Optional[ChannelModel] = None,
    ) -> None:
        self._topology = topology
        self._agents = agents
        self._metrics = metrics
        # Derive a private RNG so that link-loss decisions are independent of
        # the traffic generator's RNG consumption (stagger jitter, sender
        # selection, etc.).  Without this, concurrent async tasks sharing a
        # single RNG create coupling where changing the stagger algorithm
        # shifts which packets hit the loss threshold.
        self._rng = random.Random(rng.randrange(2**63))
        self._tracer = tracer
        self._radio = radio
        self._channel = channel

        # Build adversarial filters for nodes that have an adversarial config
        self._filters: dict[str, AdversarialFilter] = {}
        for name, agent in agents.items():
            if agent.config.adversarial is not None:
                self._filters[name] = AdversarialFilter(
                    agent.config.adversarial, self._rng
                )

        # Register callbacks on every agent
        for name, agent in agents.items():
            agent.tx_callback = self._on_tx
            agent.event_callback = metrics.on_event

    # ------------------------------------------------------------------
    # TX callback — fires in the reader loop of the sending node
    # ------------------------------------------------------------------

    async def _on_tx(self, sender_name: str, event: dict) -> None:
        hex_data: str = event.get("hex", "")
        self._metrics.record_tx(sender_name)
        log.debug("[router] tx from %s  len=%d", sender_name, len(hex_data) // 2)

        # Compute on-air time when a radio model is configured.
        airtime_ms = 0.0
        if self._radio is not None:
            airtime_ms = lora_airtime_ms(
                sf=self._radio.sf,
                bw_hz=self._radio.bw_hz,
                cr=self._radio.cr,
                payload_bytes=len(hex_data) // 2,
                preamble_symbols=self._radio.preamble_symbols,
            )

        tx_start = asyncio.get_event_loop().time()
        tx_end   = tx_start + airtime_ms / 1000.0

        # Register the transmission with the path tracer; capture tx_id so all
        # concurrent deliveries from this broadcast share the same identifier.
        tx_id: Optional[int] = None
        if self._tracer is not None:
            tx_id = self._tracer.record_tx(sender_name, hex_data, tx_start,
                                           airtime_ms=airtime_ms)

        # Register with the channel model (contention detection).
        if self._channel is not None and tx_id is not None:
            self._channel.register_tx(sender_name, tx_start, tx_end, tx_id)
            # Prune records older than 5 s to bound memory growth.
            self._channel.expire_before(tx_start - 5.0)

        for link in self._topology.neighbours(sender_name):
            # Fire-and-forget: each delivery is independent
            asyncio.create_task(
                self._deliver_to(sender_name, link, hex_data, tx_id,
                                 tx_start=tx_start, tx_end=tx_end),
                name=f"deliver-{sender_name}->{link.other}",
            )

    # ------------------------------------------------------------------
    # Per-delivery coroutine
    # ------------------------------------------------------------------

    async def _deliver_to(
        self,
        sender: str,
        link: EdgeLink,
        hex_data: str,
        tx_id: Optional[int] = None,
        tx_start: Optional[float] = None,
        tx_end: Optional[float] = None,
    ) -> None:
        receiver_name = link.other

        # 0. SNR decode gate: below SF threshold the radio cannot demodulate.
        #    Also skips LBT notification — real CAD doesn't trigger for
        #    below-threshold signals (RadioLibWrappers.cpp:171).
        if self._radio is not None:
            from .airtime import SNR_THRESHOLD
            snr_min = SNR_THRESHOLD.get(self._radio.sf, -20.0)
            if link.snr < snr_min:
                self._metrics.record_snr_drop(sender, receiver_name)
                log.debug("[router] SNR drop %s→%s (%.1f < %.1f dB)",
                          sender, receiver_name, link.snr, snr_min)
                return

        # 1. Link-level loss
        if link.loss > 0.0 and self._rng.random() < link.loss:
            self._metrics.record_link_loss(sender, receiver_name)
            log.debug("[router] link loss %s→%s", sender, receiver_name)
            return

        # 2. Adversarial filter on the RECEIVING node
        adv_filter = self._filters.get(receiver_name)
        if adv_filter is not None and adv_filter.should_apply():
            now = asyncio.get_event_loop().time()
            result = adv_filter.filter_packet(hex_data, now)
            mode = self._agents[receiver_name].config.adversarial.mode  # type: ignore[union-attr]
            if result is None:
                if mode == "replay":
                    self._metrics.record_adversarial_replay(receiver_name)
                    log.debug("[router] adv-replay queued %s→%s", sender, receiver_name)
                else:
                    self._metrics.record_adversarial_drop(receiver_name)
                    log.debug("[router] adv-drop %s→%s", sender, receiver_name)
                return
            else:
                # corrupt: result is the modified hex
                self._metrics.record_adversarial_corrupt(receiver_name)
                log.debug("[router] adv-corrupt %s→%s", sender, receiver_name)
                hex_data = result

        # 2b. Listen-Before-Talk: notify receiver that a preamble has arrived.
        # On real hardware the radio detects the preamble at rx_start
        # (= tx_start + propagation delay), before the full packet is decoded.
        # This must fire even for packets that will later fail half-duplex or
        # collision checks — the radio would still sense the energy.
        if tx_start is not None and tx_end is not None:
            preamble_arrival = tx_start + link.latency_ms / 1000.0
            now = asyncio.get_event_loop().time()
            preamble_wait = max(preamble_arrival - now, 0.0)
            if preamble_wait > 0.0:
                await asyncio.sleep(preamble_wait)
            airtime_ms = (tx_end - tx_start) * 1000.0
            receiver = self._agents.get(receiver_name)
            if receiver is not None:
                await receiver.notify_rx_start(airtime_ms)

        # 3. Propagation delay.
        # When airtime is modelled (tx_end is set), we wait until the full
        # transmission completes at the sender plus the link propagation delay.
        # This means delivery_time = tx_end + latency_ms, which is always ≥ the
        # old behaviour of just sleeping latency_ms.
        # When tx_end is None (no RF model / replay drainer), fall back to the
        # original sleep(latency_ms) behaviour.
        if tx_end is not None:
            now  = asyncio.get_event_loop().time()
            wait = max((tx_end + link.latency_ms / 1000.0) - now, 0.0)
            if wait > 0.0:
                await asyncio.sleep(wait)
        elif link.latency_ms > 0.0:
            await asyncio.sleep(link.latency_ms / 1000.0)

        # 3b. Half-duplex check: receiver cannot RX while it is TXing.
        if (self._channel is not None
                and tx_start is not None
                and tx_end is not None):
            rx_start = tx_start + link.latency_ms / 1000.0
            rx_end   = tx_end   + link.latency_ms / 1000.0
            if self._channel.is_receiver_busy(receiver_name, rx_start, rx_end):
                self._metrics.record_halfduplex_drop(sender, receiver_name)
                log.debug("[router] half-duplex drop %s->%s (receiver busy)",
                          sender, receiver_name)
                return

        # 4. RF collision check (contention model).
        if (self._channel is not None
                and tx_id is not None
                and tx_start is not None
                and tx_end is not None):
            if self._channel.is_lost(sender, receiver_name,
                                     tx_start, tx_end, tx_id):
                self._metrics.record_collision(sender, receiver_name)
                log.debug("[router] collision %s→%s", sender, receiver_name)
                if self._tracer is not None:
                    t_col = asyncio.get_event_loop().time()
                    self._tracer.record_collision(
                        sender, receiver_name, hex_data, t_col, tx_id
                    )
                return

        # 5. Record successful delivery in the path tracer
        if self._tracer is not None:
            t = asyncio.get_event_loop().time()
            self._tracer.record_rx(sender, receiver_name, hex_data, t, tx_id)

        # 6. Deliver
        receiver = self._agents.get(receiver_name)
        if receiver is None:
            return
        self._metrics.record_rx(receiver_name)
        await receiver.deliver_rx(hex_data.lower(), link.snr, link.rssi)

    # ------------------------------------------------------------------
    # Replay drainer — background task
    # ------------------------------------------------------------------

    async def run_replay_drainer(self) -> None:
        """
        Periodically re-inject replayed packets from adversarial 'replay' nodes.
        Each replayed packet is re-broadcast as if that node had transmitted it.
        """
        while True:
            await asyncio.sleep(_REPLAY_POLL_INTERVAL)
            now = asyncio.get_event_loop().time()
            for name, adv_filter in self._filters.items():
                for replayed_hex in adv_filter.drain_replays(now):
                    log.debug("[router] replaying packet from %s", name)
                    for link in self._topology.neighbours(name):
                        asyncio.create_task(
                            self._deliver_to(name, link, replayed_hex),
                            name=f"replay-{name}->{link.other}",
                        )
