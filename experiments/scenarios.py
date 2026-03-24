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
PATH2_BINARY          = os.path.join(_REPO_ROOT, "privatemesh", "path2", "build", "path2_agent")
PRIVATEROUTING1_BINARY = os.path.join(_REPO_ROOT, "privatemesh", "privaterouting1", "build", "privaterouting1_agent")

# All experiment binaries in registration order (used by the CLI).
ALL_BINARIES: list[str] = [BASELINE_BINARY, NEXTHOP_BINARY, ADAPTIVE_DELAY_BINARY,
                           PATH2_BINARY, PRIVATEROUTING1_BINARY]


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
    funnel_topo_config,
    boston_topo_config,
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

#: 3×3 grid — path privacy experiment (2-byte hashes + PNI).
#: Compare baseline (1-byte), path2 (2-byte), and privaterouting1 (2-byte+PNI).
#: Delivery rates should be identical across all three variants.
GRID_3X3_PNI = Scenario(
    name="grid/3x3/pni",
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

#: 10×10 grid — path privacy experiment (2-byte hashes + PNI at scale).
GRID_10X10_PNI = Scenario(
    name="grid/10x10/pni",
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
#: Baseline produces collisions; adaptive_agent reduces them.
#: Uses MeshCore defaults: SF10/BW250 kHz → ~330 ms airtime per packet.
#:
#: Timing rationale
#: ----------------
#: The 3×3 grid has a structural collision problem: the center node n_1_1
#: is adjacent to every edge node.  With a 1 s stagger and 533 ms airtime,
#: n_1_1 almost always overlaps the corner nodes' initial TX windows
#: (P ≈ 78%), causing n_0_0 and n_2_2 adverts to be lost at the first hop
#: in every round → 0% delivery regardless of the retransmit strategy.
#:
#: stagger_secs=20.0: ensures relay retransmissions of the FIRST advert
#: in the stagger complete before the LAST node's initial TX.
#:
#: Root-cause analysis (seed=42, hard-collision model):
#:   n_1_0 stagger TX ends at 4.99 s (= 1.116 s × 20/5).
#:   n_1_1 receives n_1_0's advert at 4.99 s; adaptive relay ends by at
#:   most 4.99+1.65+0.53 = 7.17 s.
#:   n_2_2 (last corner node) starts TX at 8.44 s (= 2.110 × 20/5).
#:   Gap = 8.44 − 7.17 = 1.27 s > 0 ✓.
#:   With stagger=5 s: n_2_2 starts at 2.11 s but n_1_1's relay ends at
#:   6.06 s → 3.95 s BEFORE n_2_2 even starts. Wait, no — with stagger=5:
#:   n_1_0 at 1.116, ends 1.649; n_1_1 relay ends ≤1.649+1.65+0.53=3.83 s
#:   which is AFTER n_2_2 at 2.11 s → collision! Stagger=20 s fixes this.
#:
#: The adaptive delay improvement applies to DATA floods only.  Adverts use
#: zero retransmit delay (same as node_agent / nexthop_agent) so network
#: discovery is deterministic.  Applying a random non-zero delay to advert
#: relays in a symmetric grid causes ~61% symmetric last-hop collision
#: probability at corner nodes (independent draws from [0,1415ms] overlap
#: within one airtime with P ≈ 0.61 per round), causing persistent 0%
#: delivery.  Return 0 for adverts is the correct scope boundary: the
#: adaptive_delay proposal targets DATA flood collisions, not advert flooding.
#:
#: readvert_interval=35s: full round = stagger (20 s) + relay cascade
#: (4 × 2678 ms ≈ 10.7 s) ≈ 30.7 s.  35 s gives a 4.3 s margin.
#:
#: warmup=75s: loop condition fires one re-advert at t=35 s; its cascade
#: ends by t≈66 s, leaving 9 s of quiet before traffic begins.
#:
#: settle=20s > worst-case text-flood cascade (~10s). ✓
GRID_3X3_CONTENTION = Scenario(
    name="grid/3x3/contention",
    topo_factory=lambda: _grid_with_radio(
        3, 3,
        warmup_secs=75.0,
        duration_secs=120.0,
        seed=42,
    ),
    warmup_secs=75.0,
    settle_secs=20.0,
    rounds=2,
    seed=42,
    rf_model="contention",
    stagger_secs=20.0,
    readvert_interval_secs=35.0,
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
    readvert_interval_secs=5.0,
    # Rationale: 100 nodes, 330 ms airtime.  Re-advertising every 5 s gives
    # 3 recovery rounds within 30 s warmup (last at t≈20 s, 10 s remaining).
)

#: Funnel / hourglass — PNI table stress test (within capacity).
#: 10 left + 10 right endpoints = 23 total nodes.  The bottleneck relay B
#: sees ~23 advert floods + a handful of text floods, well within 128.
FUNNEL_SMALL = Scenario(
    name="funnel/small",
    topo_factory=lambda: funnel_topo_config(
        left_count=10, right_count=10,
        warmup_secs=10.0,
        duration_secs=60.0,
        seed=42,
    ),
    warmup_secs=10.0,
    settle_secs=5.0,
    rounds=5,
    seed=42,
)

#: Funnel — PNI table stress test (exceeds 128 capacity).
#: 70 left + 70 right endpoints = 143 total nodes.  The bottleneck relay B
#: must forward ~143 adverts alone, exceeding the 128-entry PNI ring buffer.
#: Expect PNI eviction → some direct-route failures for privaterouting1.
FUNNEL_STRESS = Scenario(
    name="funnel/stress",
    topo_factory=lambda: funnel_topo_config(
        left_count=70, right_count=70,
        warmup_secs=20.0,
        duration_secs=120.0,
        seed=42,
    ),
    warmup_secs=20.0,
    settle_secs=10.0,
    rounds=5,
    seed=42,
)

#: Boston real-world mesh — 157-node main component from live.bostonme.sh.
#: Two well-separated leaf nodes are auto-selected as source/destination
#: (double-BFS diameter endpoints, ~7 hops apart).
#: 155 relays + high degree variance (1–52) make this a realistic stress test.
BOSTON = Scenario(
    name="boston",
    topo_factory=lambda: boston_topo_config(
        warmup_secs=15.0,
        duration_secs=120.0,
        seed=42,
    ),
    warmup_secs=15.0,
    settle_secs=10.0,
    rounds=5,
    seed=42,
)

#: All scenarios in the default run order (fastest first).
ALL_SCENARIOS: list[Scenario] = [
    LINEAR,
    GRID_3X3,
    GRID_3X3_PNI,
    GRID_10X10,
    GRID_10X10_PNI,
    FUNNEL_SMALL,
    FUNNEL_STRESS,
    BOSTON,
    GRID_3X3_CONTENTION,
    GRID_10X10_CONTENTION,
]

#: Map name → Scenario for CLI lookup.
SCENARIO_BY_NAME: dict[str, Scenario] = {s.name: s for s in ALL_SCENARIOS}
