"""
scenarios.py — pre-defined experiment scenarios and binary paths.

Each Scenario encapsulates a topology factory + timing parameters.
Binary constants point to the compiled agents in the repo tree.

Usage:

    from experiments.scenarios import GRID_3X3, BASELINE_BINARY, NEXTHOP_BINARY
    from experiments import run_scenario, compare

    results = [run_scenario(GRID_3X3, b) for b in ALL_BINARIES]
    compare(results).print()
"""

from __future__ import annotations

import os

from experiments.runner import Scenario
from orchestrator.config import RadioConfig

# Resolve paths relative to the repo root (two levels above this file).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Binary paths
# ---------------------------------------------------------------------------

BASELINE_BINARY       = os.path.join(_REPO_ROOT, "node_agent", "build", "node_agent")
NEXTHOP_BINARY        = os.path.join(_REPO_ROOT, "privatemesh", "nexthop", "build", "nexthop_agent")
ADAPTIVE_DELAY_BINARY = os.path.join(_REPO_ROOT, "privatemesh", "adaptive_delay", "build", "adaptive_agent")

# All experiment binaries in registration order (used by the CLI).
ALL_BINARIES: list[str] = [BASELINE_BINARY, NEXTHOP_BINARY, ADAPTIVE_DELAY_BINARY]


def available_binaries() -> list[str]:
    """Return only the binaries that exist on disk."""
    return [b for b in ALL_BINARIES if os.path.isfile(b) and os.access(b, os.X_OK)]


# ---------------------------------------------------------------------------
# Topology factories (imported from sim_tests helpers to avoid duplication)
# ---------------------------------------------------------------------------
# These factories are the same ones used in the integration test suite,
# ensuring experiment results are directly comparable to test baselines.

from sim_tests.helpers import (  # noqa: E402 (import after path setup)
    grid_topo_config,
    linear_three_config,
)

# MeshCore default LoRa parameters (from simple_repeater/MyMesh.cpp).
# SF10 / BW250 kHz / CR4-5 — used for contention-model scenarios.
_MESHCORE_RADIO = RadioConfig(sf=10, bw_hz=250_000, cr=1)


def _grid_with_radio(rows: int, cols: int, **sim_overrides):
    """Like grid_topo_config but adds the MeshCore default radio section."""
    cfg = grid_topo_config(rows, cols, **sim_overrides)
    cfg.radio = _MESHCORE_RADIO
    return cfg


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

#: Quick sanity check: 3-node linear topology.
#: Expected baseline behaviour: flood on round 1, direct on round 2.
LINEAR = Scenario(
    name="linear/3-node",
    topo_factory=lambda: linear_three_config(
        warmup_secs=2.0,
        duration_secs=30.0,
        seed=42,
    ),
    warmup_secs=2.0,
    settle_secs=2.0,
    rounds=2,
    seed=42,
)

#: 3×3 grid — matches the privacy-baseline test topology exactly.
#: Flood witness count ≈ 22; direct ≈ 12–14.
GRID_3X3 = Scenario(
    name="grid/3x3",
    topo_factory=lambda: grid_topo_config(
        3, 3,
        warmup_secs=3.0,
        duration_secs=30.0,
        seed=42,
    ),
    warmup_secs=3.0,
    settle_secs=3.0,
    rounds=2,
    seed=42,
)

#: 10×10 grid — stress test; 100 nodes, routing-table eviction exercised.
GRID_10X10 = Scenario(
    name="grid/10x10",
    topo_factory=lambda: grid_topo_config(
        10, 10,
        warmup_secs=10.0,
        duration_secs=60.0,
        seed=42,
    ),
    warmup_secs=10.0,
    settle_secs=5.0,
    rounds=3,
    seed=42,
)

#: 3×3 grid with RF contention model.
#: Baseline (txdelay=0) produces collisions; adaptive_agent reduces them.
#: Uses MeshCore defaults: SF10/BW250 kHz → ~330 ms airtime per packet.
#:
#: Timing rationale for adaptive_agent:
#:   Center relays get up to 4 neighbors → txdelay=1.3 → max retransmit delay
#:   = 5 × 330 ms × 1.3 ≈ 2.15 s per hop.  Worst-case flood path in a 3×3 grid
#:   is 4 hops (corner-to-corner).  warmup: 4 × 2.15 s ≈ 8.6 s → 20 s warmup.
#:   Message settle: same path length → 20 s settle.
GRID_3X3_CONTENTION = Scenario(
    name="grid/3x3/contention",
    topo_factory=lambda: _grid_with_radio(
        3, 3,
        warmup_secs=20.0,
        duration_secs=120.0,
        seed=42,
    ),
    warmup_secs=20.0,
    settle_secs=20.0,
    rounds=2,
    seed=42,
    rf_model="contention",
)

#: 10×10 grid with RF contention model.
#: Dense mesh (up to 4 neighbors per relay) → many collisions at baseline.
#:
#: Warning: this scenario runs slowly (~10 min) with adaptive_agent because
#: the 18-hop source→dest path × ~2.15 s/hop ≈ 39 s per message.
#: Use grid/3x3/contention for routine comparison runs.
GRID_10X10_CONTENTION = Scenario(
    name="grid/10x10/contention",
    topo_factory=lambda: _grid_with_radio(
        10, 10,
        warmup_secs=30.0,
        duration_secs=300.0,
        seed=42,
    ),
    warmup_secs=30.0,
    settle_secs=60.0,
    rounds=3,
    seed=42,
    rf_model="contention",
)

#: All scenarios in the default run order (fastest first).
ALL_SCENARIOS: list[Scenario] = [
    LINEAR,
    GRID_3X3,
    GRID_10X10,
    GRID_3X3_CONTENTION,
    GRID_10X10_CONTENTION,
]

#: Map name → Scenario for CLI lookup.
SCENARIO_BY_NAME: dict[str, Scenario] = {s.name: s for s in ALL_SCENARIOS}
