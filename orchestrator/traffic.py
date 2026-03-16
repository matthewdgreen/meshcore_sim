"""
traffic.py — TrafficGenerator: advertisement floods and random text sends.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from .config import SimulationConfig
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
    ) -> None:
        self._agents = agents
        self._topology = topology
        self._sim = sim_config
        self._metrics = metrics
        self._rng = rng

    # ------------------------------------------------------------------
    # Advertisement flooding
    # ------------------------------------------------------------------

    async def run_initial_adverts(self) -> None:
        """Flood advertisements from all nodes once, staggered over ~1 s."""
        tasks = [
            self._delayed_advert(agent, self._rng.uniform(0.0, 1.0))
            for agent in self._agents.values()
        ]
        await asyncio.gather(*tasks)

    async def run_periodic_adverts(self) -> None:
        """Re-flood advertisements at the configured interval."""
        interval = self._sim.advert_interval_secs
        while True:
            await asyncio.sleep(interval)
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
        """
        await asyncio.sleep(self._sim.warmup_secs)
        log.info("[traffic] warmup complete — starting random text sends")

        endpoints = self._topology.endpoint_names()
        if len(endpoints) < 2:
            log.warning("[traffic] fewer than 2 endpoints; no text traffic will be generated")
            return

        while True:
            # Exponential inter-arrival for Poisson traffic
            wait = self._rng.expovariate(1.0 / self._sim.traffic_interval_secs)
            await asyncio.sleep(wait)
            await self._send_random(endpoints)

    async def _send_random(self, endpoints: list[str]) -> None:
        sender_name = self._rng.choice(endpoints)
        sender = self._agents[sender_name]

        # Only send to peers whose advertisement the sender has already received
        known = list(sender.state.known_peers)
        if not known:
            log.debug("[traffic] %s has no known peers yet — skipping send", sender_name)
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
