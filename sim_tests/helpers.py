"""
helpers.py — Shared utilities for sim_tests.

No test classes here — just configuration constants, skip decorators,
and factory functions for building TopologyConfig objects in-process.
"""

from __future__ import annotations

import json
import os
import unittest
from collections import deque

from orchestrator.config import (
    AdversarialConfig,
    EdgeConfig,
    NodeConfig,
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

# ---------------------------------------------------------------------------
# Topology factories
# ---------------------------------------------------------------------------

def linear_three_config(**sim_overrides) -> TopologyConfig:
    """
    alice (endpoint) -- relay1 (relay) -- bob (endpoint)
    5 % loss, 20 ms latency, SNR 8 dB.
    """
    sim = SimulationConfig(
        warmup_secs=1.0,
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
    )


def two_node_direct_config(**sim_overrides) -> TopologyConfig:
    """
    alice (endpoint) -- bob (endpoint)  No relay, perfect link.
    """
    sim = SimulationConfig(
        warmup_secs=0.5,
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
        warmup_secs=5.0,
        duration_secs=60.0,
        traffic_interval_secs=8.0,
        advert_interval_secs=30.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)


def adversarial_config(mode: str, probability: float = 1.0, **adv_extras) -> TopologyConfig:
    """
    sender (endpoint) -- evil_relay (relay, adversarial) -- receiver (endpoint)
    Zero link loss so any packet drop is purely adversarial.
    """
    adv = AdversarialConfig(mode=mode, probability=probability, **adv_extras)
    sim = SimulationConfig(
        warmup_secs=1.0,
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
    )


def funnel_topo_config(
    left_count: int = 10,
    right_count: int = 10,
    **sim_overrides,
) -> TopologyConfig:
    """
    Hourglass / funnel topology for PNI table stress testing.

    Structure::

        L_0 ──┐                    ┌── R_0
        L_1 ──┤                    ├── R_1
        ...   ├── relay_L ── B ── relay_R ──┤  ...
        L_N ──┘                    └── R_N

    - ``L_0`` through ``L_{left_count-1}``: left-side endpoints
    - ``R_0`` through ``R_{right_count-1}``: right-side endpoints
    - ``relay_L`` and ``relay_R``: relays flanking the bottleneck
    - ``B``: the single bottleneck relay

    Every advert and flood must traverse B.  With N = left + right nodes,
    B forwards up to N adverts + round × text floods = N + rounds PNI entries.
    Set left_count = right_count = 70 to push past the 128-entry PNI table.

    The first endpoint is ``L_0`` (source); the last is ``R_{right_count-1}``
    (destination), matching the runner's endpoints[0] / endpoints[-1] logic.
    """
    nodes = []
    edges = []

    # Left-side endpoints
    for i in range(left_count):
        nodes.append(NodeConfig(name=f"L_{i}", relay=False))

    # Relays
    nodes.append(NodeConfig(name="relay_L", relay=True))
    nodes.append(NodeConfig(name="B", relay=True))
    nodes.append(NodeConfig(name="relay_R", relay=True))

    # Right-side endpoints
    for i in range(right_count):
        nodes.append(NodeConfig(name=f"R_{i}", relay=False))

    # Edges: each left endpoint connects to relay_L
    for i in range(left_count):
        edges.append(EdgeConfig(
            a=f"L_{i}", b="relay_L",
            loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
        ))

    # Bottleneck chain: relay_L -- B -- relay_R
    edges.append(EdgeConfig(
        a="relay_L", b="B",
        loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
    ))
    edges.append(EdgeConfig(
        a="B", b="relay_R",
        loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
    ))

    # Each right endpoint connects to relay_R
    for i in range(right_count):
        edges.append(EdgeConfig(
            a="relay_R", b=f"R_{i}",
            loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
        ))

    sim = SimulationConfig(
        warmup_secs=10.0,
        duration_secs=120.0,
        traffic_interval_secs=8.0,
        advert_interval_secs=60.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)


def boston_topo_config(
    topo_file: str = "boston_relays.json",
    **sim_overrides,
) -> TopologyConfig:
    """
    Load the Boston mesh topology from JSON and prepare it for experiments.

    The real-world Boston topology has all nodes marked as relays.  This
    factory:

    1. Filters to the largest connected component (the main file contains
       isolated singletons that would never participate in routing).
    2. Picks two well-separated leaf nodes (degree-1) as source/destination
       endpoints by doing a double-BFS to approximate the graph diameter.
       These two nodes are re-marked ``relay=False`` so the experiment
       runner recognises them as endpoints.
    3. Preserves lat/lon coordinates if the topology file contains them
       (``boston_relays_2026_03_17.json``).

    Parameters
    ----------
    topo_file:
        Filename inside the ``topologies/`` directory.
    **sim_overrides:
        Keyword overrides applied to the ``SimulationConfig`` (e.g.
        ``warmup_secs=20.0``).
    """
    path = os.path.join(TOPO_DIR, topo_file)
    with open(path) as f:
        raw = json.load(f)

    # Build adjacency list.
    adj: dict[str, list[str]] = {}
    for n in raw["nodes"]:
        adj.setdefault(n["name"], [])
    for e in raw["edges"]:
        adj.setdefault(e["a"], []).append(e["b"])
        adj.setdefault(e["b"], []).append(e["a"])

    # BFS helper returning (farthest_node, distance, {node: dist}).
    def _bfs(start: str) -> tuple[str, int, dict[str, int]]:
        visited = {start: 0}
        q: deque[str] = deque([start])
        farthest = start
        while q:
            node = q.popleft()
            for nb in adj.get(node, []):
                if nb not in visited:
                    visited[nb] = visited[node] + 1
                    q.append(nb)
                    if visited[nb] > visited[farthest]:
                        farthest = nb
        return farthest, visited[farthest], visited

    # Find the largest connected component.
    all_names = {n["name"] for n in raw["nodes"]}
    visited_global: set[str] = set()
    best_component: set[str] = set()
    for name in all_names:
        if name not in visited_global:
            _, _, dists = _bfs(name)
            component = set(dists.keys())
            visited_global |= component
            if len(component) > len(best_component):
                best_component = component

    # Double-BFS to find two well-separated nodes (approximate diameter).
    any_node = next(iter(best_component))
    far1, _, _ = _bfs(any_node)
    far2, diameter, _ = _bfs(far1)

    # Index raw nodes/edges by name for quick lookup.
    raw_nodes_by_name = {n["name"]: n for n in raw["nodes"]}

    # Build NodeConfigs — only nodes in the main component.
    nodes = []
    for name in sorted(best_component):
        rn = raw_nodes_by_name[name]
        is_endpoint = name in (far1, far2)
        nc = NodeConfig(
            name=name,
            relay=not is_endpoint,
            lat=rn.get("lat"),
            lon=rn.get("lon"),
        )
        nodes.append(nc)

    # Build EdgeConfigs — only edges where both ends are in the main component.
    edges = []
    for e in raw["edges"]:
        if e["a"] in best_component and e["b"] in best_component:
            edges.append(EdgeConfig(
                a=e["a"],
                b=e["b"],
                loss=e.get("loss", 0.05),
                latency_ms=e.get("latency_ms", 20.0),
                snr=e.get("snr", 6.0),
                rssi=e.get("rssi", -90.0),
            ))

    sim = SimulationConfig(
        warmup_secs=15.0,
        duration_secs=120.0,
        traffic_interval_secs=10.0,
        advert_interval_secs=60.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    for k, v in sim_overrides.items():
        setattr(sim, k, v)

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)
