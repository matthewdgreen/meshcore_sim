"""
config.py — Topology JSON loading and dataclasses.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AdversarialConfig:
    mode: str                     # "drop" | "replay" | "corrupt"
    probability: float = 1.0      # fraction of packets that trigger behaviour
    replay_delay_ms: float = 5000.0
    corrupt_byte_count: int = 1


@dataclass
class NodeConfig:
    name: str
    relay: bool = False
    prv_key: Optional[str] = None          # 128 hex chars or None
    adversarial: Optional[AdversarialConfig] = None


@dataclass
class EdgeConfig:
    a: str
    b: str
    loss: float = 0.0         # packet loss probability [0, 1]
    latency_ms: float = 0.0   # one-way propagation delay
    snr: float = 6.0          # SNR delivered to receiver (dB)
    rssi: float = -90.0       # RSSI delivered to receiver (dBm)


@dataclass
class SimulationConfig:
    warmup_secs: float = 5.0
    duration_secs: float = 60.0
    traffic_interval_secs: float = 10.0   # mean seconds between random sends
    advert_interval_secs: float = 30.0
    epoch: int = 0                         # 0 → use wall-clock time
    agent_binary: str = "./node_agent/build/node_agent"
    seed: Optional[int] = None


@dataclass
class TopologyConfig:
    nodes: list[NodeConfig]
    edges: list[EdgeConfig]
    simulation: SimulationConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_topology(path: str) -> TopologyConfig:
    with open(path) as f:
        raw = json.load(f)

    nodes = []
    for n in raw.get("nodes", []):
        adv = None
        if n.get("adversarial"):
            a = n["adversarial"]
            adv = AdversarialConfig(
                mode=a["mode"],
                probability=float(a.get("probability", 1.0)),
                replay_delay_ms=float(a.get("replay_delay_ms", 5000.0)),
                corrupt_byte_count=int(a.get("corrupt_byte_count", 1)),
            )
        nodes.append(NodeConfig(
            name=n["name"],
            relay=bool(n.get("relay", False)),
            prv_key=n.get("prv_key"),
            adversarial=adv,
        ))

    edges = []
    for e in raw.get("edges", []):
        edges.append(EdgeConfig(
            a=e["a"],
            b=e["b"],
            loss=float(e.get("loss", 0.0)),
            latency_ms=float(e.get("latency_ms", 0.0)),
            snr=float(e.get("snr", 6.0)),
            rssi=float(e.get("rssi", -90.0)),
        ))

    sim_raw = raw.get("simulation", {})
    sim = SimulationConfig(
        warmup_secs=float(sim_raw.get("warmup_secs", 5.0)),
        duration_secs=float(sim_raw.get("duration_secs", 60.0)),
        traffic_interval_secs=float(sim_raw.get("traffic_interval_secs", 10.0)),
        advert_interval_secs=float(sim_raw.get("advert_interval_secs", 30.0)),
        epoch=int(sim_raw.get("epoch", 0)),
        agent_binary=sim_raw.get("agent_binary", "./node_agent/build/node_agent"),
        seed=sim_raw.get("seed"),
    )
    if sim.epoch == 0:
        sim.epoch = int(time.time())

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)
