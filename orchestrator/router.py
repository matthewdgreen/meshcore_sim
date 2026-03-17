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
    ) -> None:
        self._topology = topology
        self._agents = agents
        self._metrics = metrics
        self._rng = rng
        self._tracer = tracer

        # Build adversarial filters for nodes that have an adversarial config
        self._filters: dict[str, AdversarialFilter] = {}
        for name, agent in agents.items():
            if agent.config.adversarial is not None:
                self._filters[name] = AdversarialFilter(
                    agent.config.adversarial, rng
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

        # Register the transmission with the path tracer; capture tx_id so all
        # concurrent deliveries from this broadcast share the same identifier.
        tx_id: Optional[int] = None
        if self._tracer is not None:
            t = asyncio.get_event_loop().time()
            tx_id = self._tracer.record_tx(sender_name, hex_data, t)

        for link in self._topology.neighbours(sender_name):
            # Fire-and-forget: each delivery is independent
            asyncio.create_task(
                self._deliver_to(sender_name, link, hex_data, tx_id),
                name=f"deliver-{sender_name}->{link.other}",
            )

    # ------------------------------------------------------------------
    # Per-delivery coroutine
    # ------------------------------------------------------------------

    async def _deliver_to(
        self, sender: str, link: EdgeLink, hex_data: str,
        tx_id: Optional[int] = None,
    ) -> None:
        receiver_name = link.other

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

        # 3. Propagation delay
        if link.latency_ms > 0.0:
            await asyncio.sleep(link.latency_ms / 1000.0)

        # 4. Record successful delivery in the path tracer
        if self._tracer is not None:
            t = asyncio.get_event_loop().time()
            self._tracer.record_rx(sender, receiver_name, hex_data, t, tx_id)

        # 5. Deliver
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
