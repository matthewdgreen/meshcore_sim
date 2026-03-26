"""
helpers.py — Shared utilities for sim_tests.

No test classes here — just configuration constants, skip decorators,
and factory functions for building TopologyConfig objects in-process.
"""

from __future__ import annotations

import os
import unittest

from orchestrator.config import (
    AdversarialConfig,
    EdgeConfig,
    NodeConfig,
    RadioConfig,
    SimulationConfig,
    TopologyConfig,
)

# ---------------------------------------------------------------------------
# Binary path
# ---------------------------------------------------------------------------

# Resolve relative to the repository root, not the CWD at test time.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY_PATH = os.path.join(_REPO_ROOT, "node_agent", "build", "node_agent")
TOPO_DIR    = os.path.join(_REPO_ROOT, "topologies")


def binary_available() -> bool:
    return os.path.isfile(BINARY_PATH) and os.access(BINARY_PATH, os.X_OK)


SKIP_IF_NO_BINARY = unittest.skipUnless(
    binary_available(),
    "node_agent binary not found at %s — skipping integration tests" % BINARY_PATH,
)

# Matches C++ node_agent built-in defaults (SF8, BW 62.5 kHz, CR4/8).
# Providing this explicitly ensures Python-side airtime/stagger calculations
# are correct even when the topology JSON doesn't specify radio params.
_DEFAULT_RADIO = RadioConfig(sf=8, bw_hz=62_500, cr=4)

# ---------------------------------------------------------------------------
# Topology factories
# ---------------------------------------------------------------------------

def linear_three_config(**sim_overrides) -> TopologyConfig:
    """
    alice (endpoint) -- relay1 (relay) -- bob (endpoint)
    5 % loss, 20 ms latency, SNR 8 dB.
    """
    sim = SimulationConfig(
        warmup_secs=5.0,
        duration_secs=10.0,
        traffic_interval_secs=2.0,
        advert_interval_secs=30.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(
        nodes=[
            NodeConfig(name="alice",  relay=False),
            NodeConfig(name="relay1", relay=True),
            NodeConfig(name="bob",    relay=False),
        ],
        edges=[
            EdgeConfig(a="alice",  b="relay1", loss=0.05, latency_ms=20.0, snr=8.0, rssi=-85.0),
            EdgeConfig(a="relay1", b="bob",    loss=0.05, latency_ms=20.0, snr=8.0, rssi=-85.0),
        ],
        simulation=sim,
        radio=_DEFAULT_RADIO,
    )


def two_node_direct_config(**sim_overrides) -> TopologyConfig:
    """
    alice (endpoint) -- bob (endpoint)  No relay, perfect link.
    """
    sim = SimulationConfig(
        warmup_secs=3.0,
        duration_secs=5.0,
        traffic_interval_secs=1.0,
        advert_interval_secs=30.0,
        default_binary=BINARY_PATH,
        seed=1,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(
        nodes=[
            NodeConfig(name="alice", relay=False),
            NodeConfig(name="bob",   relay=False),
        ],
        edges=[
            EdgeConfig(a="alice", b="bob", loss=0.0, latency_ms=0.0, snr=10.0, rssi=-80.0),
        ],
        simulation=sim,
        radio=_DEFAULT_RADIO,
    )


def grid_topo_config(rows: int, cols: int, **sim_overrides) -> TopologyConfig:
    """
    rows×cols orthogonal grid topology.

    * n_0_0               → SOURCE endpoint
    * n_{rows-1}_{cols-1} → DESTINATION endpoint
    * all others          → relays

    Edges: 4-connectivity (N/S/E/W).  Default: 2 % loss, 20 ms latency.
    """
    def _name(r: int, c: int) -> str:
        return f"n_{r}_{c}"

    src = _name(0, 0)
    dst = _name(rows - 1, cols - 1)

    nodes = []
    for r in range(rows):
        for c in range(cols):
            name = _name(r, c)
            nodes.append(NodeConfig(name=name, relay=(name not in (src, dst))))

    edges = []
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                edges.append(EdgeConfig(
                    a=_name(r, c), b=_name(r, c + 1),
                    loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
                ))
            if r + 1 < rows:
                edges.append(EdgeConfig(
                    a=_name(r, c), b=_name(r + 1, c),
                    loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
                ))

    sim = SimulationConfig(
        warmup_secs=15.0,
        duration_secs=60.0,
        traffic_interval_secs=8.0,
        advert_interval_secs=30.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim, radio=_DEFAULT_RADIO)


def adversarial_config(mode: str, probability: float = 1.0, **adv_extras) -> TopologyConfig:
    """
    sender (endpoint) -- evil_relay (relay, adversarial) -- receiver (endpoint)
    Zero link loss so any packet drop is purely adversarial.
    """
    adv = AdversarialConfig(mode=mode, probability=probability, **adv_extras)
    sim = SimulationConfig(
        warmup_secs=5.0,
        duration_secs=8.0,
        traffic_interval_secs=1.5,
        advert_interval_secs=30.0,
        default_binary=BINARY_PATH,
        seed=7,
    )
    return TopologyConfig(
        nodes=[
            NodeConfig(name="sender",    relay=False),
            NodeConfig(name="evil_relay", relay=True, adversarial=adv),
            NodeConfig(name="receiver",  relay=False),
        ],
        edges=[
            EdgeConfig(a="sender",    b="evil_relay", loss=0.0, latency_ms=0.0, snr=9.0, rssi=-82.0),
            EdgeConfig(a="evil_relay", b="receiver",  loss=0.0, latency_ms=0.0, snr=9.0, rssi=-82.0),
        ],
        simulation=sim,
        radio=_DEFAULT_RADIO,
    )
