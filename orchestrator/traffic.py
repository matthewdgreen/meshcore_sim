"""
traffic.py — TrafficGenerator: advertisement floods and random text sends.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from .airtime import _TYPICAL_ADVERT_BYTES, advert_stagger_secs, lora_airtime_ms
from .config import RadioConfig, SimulationConfig
from .metrics import MetricsCollector
from .node import NodeAgent
from .topology import Topology

log = logging.getLogger(__name__)


class TrafficGenerator:
    def __init__(
        self,
        agents: dict[str, NodeAgent],
        topology: Topology,
        sim_config: SimulationConfig,
        metrics: MetricsCollector,
        rng: random.Random,
        radio: RadioConfig | None = None,
    ) -> None:
        self._agents = agents
        self._topology = topology
        self._sim = sim_config
        self._metrics = metrics
        self._rng = rng
        self._endpoint_pubs: Optional[set[str]] = None

        # Auto-compute stagger from radio config and node count so that
        # initial adverts don't collide under the hard-collision RF model.
        if radio is not None:
            self._stagger_secs = advert_stagger_secs(
                radio.sf, radio.bw_hz, radio.cr, len(agents),
                radio.preamble_symbols)
            # Single-advert airtime for per-node jitter calculation.
            self._advert_airtime_secs = lora_airtime_ms(
                radio.sf, radio.bw_hz, radio.cr, _TYPICAL_ADVERT_BYTES,
                radio.preamble_symbols) / 1000.0
        else:
            self._stagger_secs = 1.0
            self._advert_airtime_secs = 0.1

    # ------------------------------------------------------------------
    # Advertisement flooding
    # ------------------------------------------------------------------

    async def run_initial_adverts(self, stagger_secs: float | None = None) -> None:
        """Flood advertisements from all nodes once, staggered over a time window.

        Uses a slot-based stagger: the window is divided into N equal slots
        (one per node), nodes are randomly assigned to slots, and a small
        random jitter is added within each slot.  This guarantees a minimum
        separation of ~0.7 × slot_width between any two adjacent transmissions
        — enough to prevent hidden-terminal collisions at shared receivers
        while still providing realistic randomness.

        Parameters
        ----------
        stagger_secs:
            Width of the stagger window in seconds.  When ``None`` (default),
            the auto-computed value based on radio config and node count is
            used.  Pass an explicit value to override.
        """
        stagger = stagger_secs if stagger_secs is not None else self._stagger_secs
        agents_list = list(self._agents.values())
        self._rng.shuffle(agents_list)
        slot = stagger / max(len(agents_list), 1)
        tasks = []
        for i, agent in enumerate(agents_list):
            # Place each node in a separate time slot.  Jitter within ±30 %
            # of the slot width adds realistic variation while preserving the
            # minimum guard interval (≥ 1 airtime) between adjacent slots.
            delay = i * slot + self._rng.uniform(0.0, slot * 0.3)
            tasks.append(self._delayed_advert(agent, delay))
        await asyncio.gather(*tasks)

    async def run_periodic_adverts(self) -> None:
        """Re-flood advertisements at the configured interval with jitter.

        A ±20 % jitter on the interval models real-world clock drift and
        prevents periodic collision patterns from repeating indefinitely.
        """
        interval = self._sim.advert_interval_secs
        while True:
            jitter = self._rng.uniform(-0.2 * interval, 0.2 * interval)
            await asyncio.sleep(interval + jitter)
            await self.run_initial_adverts()

    async def _delayed_advert(self, agent: NodeAgent, delay: float) -> None:
        await asyncio.sleep(delay)
        log.debug("[traffic] advert from %s", agent.config.name)
        await agent.broadcast_advert(agent.config.name)

    # ------------------------------------------------------------------
    # Random text traffic
    # ------------------------------------------------------------------

    async def run_traffic(self) -> None:
        """
        After warmup_secs, generate Poisson-distributed random text messages
        between endpoint pairs that have already exchanged advertisements.

        Stops sending new messages after duration_secs so that the grace
        period (handled by the wall-clock timer) can drain in-flight ACKs.
        """
        await asyncio.sleep(self._sim.warmup_secs)
        log.info("[traffic] warmup complete — starting random text sends")

        endpoints = self._topology.endpoint_names()
        if len(endpoints) < 2:
            log.warning("[traffic] fewer than 2 endpoints; no text traffic will be generated")
            return

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._sim.duration_secs
        while loop.time() < deadline:
            # Exponential inter-arrival for Poisson traffic
            wait = self._rng.expovariate(1.0 / self._sim.traffic_interval_secs)
            await asyncio.sleep(wait)
            if loop.time() >= deadline:
                break
            await self._send_random(endpoints)
        log.info("[traffic] traffic window closed — grace period for in-flight messages")

    async def _send_random(self, endpoints: list[str]) -> None:
        sender_name = self._rng.choice(endpoints)
        sender = self._agents[sender_name]

        # ~15% chance of sending a channel (group) message instead of direct
        if self._rng.random() < 0.15:
            text = f"ch from {sender_name} t={int(time.time() * 1000) % 1_000_000}"
            log.info("[traffic] send_channel  %s  %r", sender_name, text)
            self._metrics.record_channel_send(sender_name, text)
            await sender.send_channel(text)
            return

        # Build the set of endpoint pub_keys once (agents are all ready by now)
        if self._endpoint_pubs is None:
            self._endpoint_pubs = {
                self._agents[n].state.pub_key
                for n in endpoints
                if self._agents[n].state.pub_key
            }

        # Only send to other endpoints (companions) — never to relays.
        known = list(
            sender.state.known_peers & self._endpoint_pubs - {sender.state.pub_key}
        )

        if not known:
            log.debug("[traffic] %s has no known endpoints yet — skipping send", sender_name)
            return

        dest_pub = self._rng.choice(known)
        # Use first 8 hex chars as dest prefix (unambiguous in small networks;
        # MeshCore does a prefix match internally)
        dest_prefix = dest_pub[:8]

        # Embed a timestamp so every message text is unique for delivery tracking
        text = f"hello from {sender_name} t={int(time.time() * 1000) % 1_000_000}"

        log.info("[traffic] send_text  %s → %s...  %r", sender_name, dest_prefix, text)
        self._metrics.record_send_attempt(sender_name, dest_pub, text)
        await sender.send_text(dest_prefix, text)
