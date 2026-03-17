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
    room_server: bool = False              # spawn with --room-server flag
    prv_key: Optional[str] = None          # 128 hex chars or None
    adversarial: Optional[AdversarialConfig] = None
    binary: Optional[str] = None           # per-node binary override (None → use SimulationConfig.default_binary)


@dataclass
class DirectionalOverrides:
    """Per-direction parameter overrides for one side of an edge.
    Any field left as None inherits the symmetric EdgeConfig default."""
    loss:       Optional[float] = None
    latency_ms: Optional[float] = None
    snr:        Optional[float] = None
    rssi:       Optional[float] = None


@dataclass
class EdgeConfig:
    a: str
    b: str
    loss: float = 0.0         # packet loss probability [0, 1]
    latency_ms: float = 0.0   # one-way propagation delay
    snr: float = 6.0          # SNR delivered to receiver (dB)
    rssi: float = -90.0       # RSSI delivered to receiver (dBm)
    # Optional per-direction overrides.  None means "use the symmetric default".
    # a_to_b: parameters as seen by b when a transmits.
    # b_to_a: parameters as seen by a when b transmits.
    a_to_b: Optional[DirectionalOverrides] = None
    b_to_a: Optional[DirectionalOverrides] = None


@dataclass
class SimulationConfig:
    warmup_secs: float = 5.0
    duration_secs: float = 60.0
    traffic_interval_secs: float = 10.0   # mean seconds between random sends
    advert_interval_secs: float = 30.0
    epoch: int = 0                         # 0 → use wall-clock time
    default_binary: str = "./node_agent/build/node_agent"
    seed: Optional[int] = None


@dataclass
class TopologyConfig:
    nodes: list[NodeConfig]
    edges: list[EdgeConfig]
    simulation: SimulationConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _parse_directional(raw: dict) -> Optional[DirectionalOverrides]:
    """Parse an a_to_b / b_to_a sub-object.  Returns None if absent or empty."""
    if not raw:
        return None
    return DirectionalOverrides(
        loss=       float(raw["loss"])       if "loss"       in raw else None,
        latency_ms= float(raw["latency_ms"]) if "latency_ms" in raw else None,
        snr=        float(raw["snr"])        if "snr"        in raw else None,
        rssi=       float(raw["rssi"])       if "rssi"       in raw else None,
    )


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
            room_server=bool(n.get("room_server", False)),
            prv_key=n.get("prv_key"),
            adversarial=adv,
            binary=n.get("binary"),
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
            a_to_b=_parse_directional(e.get("a_to_b", {})),
            b_to_a=_parse_directional(e.get("b_to_a", {})),
        ))

    sim_raw = raw.get("simulation", {})
    sim = SimulationConfig(
        warmup_secs=float(sim_raw.get("warmup_secs", 5.0)),
        duration_secs=float(sim_raw.get("duration_secs", 60.0)),
        traffic_interval_secs=float(sim_raw.get("traffic_interval_secs", 10.0)),
        advert_interval_secs=float(sim_raw.get("advert_interval_secs", 30.0)),
        epoch=int(sim_raw.get("epoch", 0)),
        # Accept both "default_binary" (current) and "agent_binary" (legacy) in JSON.
        default_binary=(
            sim_raw.get("default_binary")
            or sim_raw.get("agent_binary", "./node_agent/build/node_agent")
        ),
        seed=sim_raw.get("seed"),
    )
    if sim.epoch == 0:
        sim.epoch = int(time.time())

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)
