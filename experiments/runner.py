"""
runner.py — core simulation runner for experiments.

Provides Scenario (what to run), SimResult (what came out), and
run_scenario() (the synchronous entry point that wraps the async machinery).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from orchestrator.channel import ChannelModel
from orchestrator.config import RadioConfig, TopologyConfig
from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.packet import PAYLOAD_TYPE_TXT_MSG
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.traffic import TrafficGenerator
from orchestrator.tracer import PacketTrace, PacketTracer


# ---------------------------------------------------------------------------
# Scenario — describes a reusable experiment configuration
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """
    A named simulation configuration that can be run with any binary.

    Attributes
    ----------
    name:
        Human-readable identifier, e.g. "grid/3x3".  Used as a key in
        ComparisonTable output and in the CLI ``--scenario`` filter.
    topo_factory:
        Zero-argument callable that returns a fresh TopologyConfig each time
        a variant is run.  The runner overrides ``simulation.default_binary``
        before starting agents, so the factory need not hard-code a binary.
    warmup_secs:
        How long to wait after the initial advert flood before sending traffic.
        Longer values give nodes more time to build routing tables.
    settle_secs:
        How long to wait after each message send for propagation to complete.
    rounds:
        Number of text messages to send from source to destination.
    seed:
        RNG seed passed to PacketRouter and TrafficGenerator for reproducibility.
    readvert_interval_secs:
        When set, re-flood advertisements every this many seconds throughout
        the warmup period so nodes can recover from collision losses in the
        initial flood.  Essential for contention-model scenarios where the
        initial burst may be heavily collided: without re-advertising the
        source may never learn the destination and delivery will be 0%.
        Set to ``None`` (default) to send only one initial advert round.
    """
    name: str
    topo_factory: Callable[[], TopologyConfig]
    warmup_secs: float = 3.0
    settle_secs: float = 3.0
    rounds: int = 2
    seed: int = 42
    readvert_interval_secs: Optional[float] = None
    stagger_secs: Optional[float] = None
    """
    Width of the uniform-random stagger window used for each advert flood round.

    When ``None`` (default), ``TrafficGenerator`` auto-computes a safe stagger
    from the radio config and node count (``node_count × airtime × 2``).
    Pass an explicit value to override — e.g. ``20.0`` for a 9-node grid
    contention scenario where relay cascades must complete between staggers.
    """


# ---------------------------------------------------------------------------
# SimResult — output of one scenario × binary run
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    """
    Collects all measurable outputs from a single (Scenario, binary) run.

    Computed properties provide the key metrics used for comparison.
    """
    label: str                        # e.g. "nexthop / grid/3x3"
    binary: str                       # absolute or relative path to binary
    scenario_name: str
    metrics: MetricsCollector
    tracer: PacketTracer
    elapsed_s: float                  # wall-clock seconds for the whole run

    # ---- convenience accessors ----

    @property
    def delivery_rate(self) -> float:
        """Fraction of messages delivered (0.0–1.0)."""
        return self.metrics.delivery_rate

    @property
    def txt_traces(self) -> list[PacketTrace]:
        """All PacketTrace objects for TXT_MSG packets."""
        return [t for t in self.tracer.traces.values()
                if t.payload_type == PAYLOAD_TYPE_TXT_MSG]

    @property
    def avg_witness_count(self) -> float:
        """Mean witness_count across all TXT_MSG traces (0.0 if none)."""
        traces = self.txt_traces
        if not traces:
            return 0.0
        return sum(t.witness_count for t in traces) / len(traces)

    @property
    def flood_witness_count(self) -> int:
        """witness_count of the first (flood-routed) TXT_MSG, or 0."""
        traces = self.txt_traces
        return traces[0].witness_count if traces else 0

    @property
    def direct_witness_count(self) -> int:
        """witness_count of the second TXT_MSG (post-learning), or 0."""
        traces = self.txt_traces
        return traces[1].witness_count if len(traces) > 1 else 0

    @property
    def avg_latency_ms(self) -> float:
        return self.metrics.avg_latency_ms

    @property
    def avg_packet_size_bytes(self) -> float:
        """
        Mean wire-format size (bytes) of TXT_MSG packets across all hops.

        Flood packets grow as relays append relay hashes, so this captures both
        the payload size and the path-overhead accumulated in transit.  Direct
        packets have a fixed size (path is pre-specified by the sender).
        Returns 0.0 if no TXT_MSG hops were recorded.
        """
        all_sizes = [
            h.size_bytes
            for t in self.txt_traces
            for h in t.hops
            if h.size_bytes > 0
        ]
        return sum(all_sizes) / len(all_sizes) if all_sizes else 0.0

    @property
    def total_hops(self) -> int:
        """Total hop records across all packet traces."""
        return sum(len(t.hops) for t in self.tracer.traces.values())

    @property
    def collision_count(self) -> int:
        return self.metrics.collision_count

    @property
    def avg_relay_delay_ms(self) -> float:
        """Mean relay retransmit delay in ms (0.0 if no relay events)."""
        delays = self.tracer.compute_relay_delays()
        return sum(delays) / len(delays) if delays else 0.0

    @property
    def binary_name(self) -> str:
        """Basename of the binary (e.g. 'nexthop_agent')."""
        return os.path.basename(self.binary)


# ---------------------------------------------------------------------------
# Internal async simulation runner
# ---------------------------------------------------------------------------

async def _run_async(
    scenario: Scenario,
    binary: str,
    trace_out: Optional[str] = None,
) -> tuple[MetricsCollector, PacketTracer]:
    """
    Bring up all agents for one (scenario, binary) pair, run traffic, shut down.

    Returns raw (metrics, tracer) for the caller to wrap in a SimResult.
    """
    topo_cfg = scenario.topo_factory()
    topo_cfg.simulation.default_binary = binary

    rng     = random.Random(scenario.seed)
    tracer  = PacketTracer()
    metrics = MetricsCollector()
    topology = Topology(topo_cfg)

    agents: dict[str, NodeAgent] = {
        n.name: NodeAgent(n, topo_cfg.simulation, radio=topo_cfg.radio)
        for n in topo_cfg.nodes
    }

    # Start all agents (batch of 50 to avoid FD exhaustion on large grids).
    names = list(agents.keys())
    for i in range(0, len(names), 50):
        batch = names[i:i + 50]
        await asyncio.gather(*(agents[n].start() for n in batch))
    await asyncio.gather(*(a.wait_ready(timeout=15.0) for a in agents.values()))

    # Build RF model objects — always use contention (airtime + collision).
    radio = topo_cfg.radio or RadioConfig()
    link_rssi: dict[str, dict[str, float]] = {}
    for name in topology.all_names():
        link_rssi[name] = {
            link.other: link.rssi
            for link in topology.neighbours(name)
        }
    channel = ChannelModel(link_rssi=link_rssi)

    # Wire routing and traffic.
    PacketRouter(topology, agents, metrics, rng, tracer=tracer,
                 radio=radio, channel=channel)
    traffic = TrafficGenerator(agents, topology, topo_cfg.simulation, metrics, rng,
                               radio=radio)

    # Explicit stagger from scenario, or None → auto-computed by TrafficGenerator.
    stagger = scenario.stagger_secs
    await traffic.run_initial_adverts(stagger_secs=stagger)
    if scenario.readvert_interval_secs is not None:
        # Re-flood adverts periodically so nodes can recover from collision
        # losses in the initial burst.  Stop early enough that the last flood
        # has at least readvert_interval_secs to propagate before warmup ends.
        elapsed = 0.0
        interval = scenario.readvert_interval_secs
        while elapsed + interval * 2 < scenario.warmup_secs:
            await asyncio.sleep(interval)
            elapsed += interval
            await traffic.run_initial_adverts(stagger_secs=stagger)
        await asyncio.sleep(scenario.warmup_secs - elapsed)
    else:
        await asyncio.sleep(scenario.warmup_secs)

    # Identify source and destination: first and last endpoint (non-relay, non-room-server).
    endpoints = [n.name for n in topo_cfg.nodes
                 if not n.relay and not getattr(n, "room_server", False)]
    if len(endpoints) >= 2:
        src_name = endpoints[0]
        dst_name = endpoints[-1]
        dst_pub  = agents[dst_name].state.pub_key
        for i in range(scenario.rounds):
            # Use a unique text per round so MetricsCollector can correlate
            # send→receive events correctly (it keys on message text).
            msg = f"experiment-msg-{i}"
            metrics.record_send_attempt(src_name, dst_pub, msg)
            await agents[src_name].send_text(dst_pub, msg)
            await asyncio.sleep(scenario.settle_secs)

    await asyncio.gather(*(a.quit() for a in agents.values()),
                         return_exceptions=True)

    if trace_out is not None:
        trace_dict = tracer.to_dict(node_names=list(agents.keys()))
        os.makedirs(os.path.dirname(os.path.abspath(trace_out)), exist_ok=True)
        with open(trace_out, "w") as _f:
            json.dump(trace_dict, _f, indent=2)

    return metrics, tracer


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_scenario(
    scenario: Scenario,
    binary: str,
    label: Optional[str] = None,
    trace_out: Optional[str] = None,
) -> SimResult:
    """
    Run *scenario* using *binary* and return a SimResult.

    This is a synchronous wrapper around the async simulation; it must not be
    called from within a running event loop.  For use inside async test code,
    call ``_run_async`` directly (as test_privacy_baseline.py does).

    Parameters
    ----------
    scenario:
        The Scenario to run.
    binary:
        Path to the node_agent-compatible binary to use for all nodes (unless
        individual nodes have a ``binary`` override in the topology JSON).
    label:
        Optional human-readable label for this result row.  Defaults to
        ``"<binary_basename> / <scenario.name>"``.
    trace_out:
        If provided, write the PacketTracer JSON export to this path after the
        run completes.  The file can be opened with
        ``python3 -m workbench <topology.json> --trace <trace_out>`` for interactive
        packet-path visualisation.
    """
    if label is None:
        label = f"{os.path.basename(binary)} / {scenario.name}"

    t0 = time.monotonic()
    metrics, tracer = asyncio.run(_run_async(scenario, binary, trace_out=trace_out))
    elapsed = time.monotonic() - t0

    return SimResult(
        label=label,
        binary=binary,
        scenario_name=scenario.name,
        metrics=metrics,
        tracer=tracer,
        elapsed_s=elapsed,
    )
