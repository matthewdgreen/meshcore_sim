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
    max_heap_kb: Optional[int] = None      # per-node heap limit in KB (None → use SimulationConfig.default_max_heap_kb)
    lat: Optional[float] = None            # WGS-84 latitude  (ignored by simulator; for visualisation)
    lon: Optional[float] = None            # WGS-84 longitude (ignored by simulator; for visualisation)


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
class RadioConfig:
    """LoRa radio parameters shared by all nodes in the simulation.

    Defaults match the MeshCore source (simple_repeater/MyMesh.cpp):
      SF=10, BW=250 kHz, CR4/5.

    Note on cr: we use the coding-rate *offset* (RadioLib denominator minus 4),
    i.e. cr=1 means CR4/5 (RadioLib CR=5), cr=4 means CR4/8 (RadioLib CR=8).
    MeshCore source uses the denominator directly (LORA_CR 5 = CR4/5 = cr=1 here).
    """
    sf: int = 10             # spreading factor (7–12); MeshCore default 10
    bw_hz: int = 250_000     # bandwidth in Hz; MeshCore default 250 kHz
    cr: int = 1              # coding-rate offset: 1=CR4/5, 2=CR4/6, 3=CR4/7, 4=CR4/8
    preamble_symbols: int = 8


@dataclass
class SimulationConfig:
    warmup_secs: float = 5.0
    duration_secs: float = 60.0
    traffic_interval_secs: float = 10.0   # mean seconds between random sends
    advert_interval_secs: float = 30.0
    epoch: int = 0                         # 0 → use wall-clock time
    default_binary: str = "./node_agent/build/node_agent"
    default_max_heap_kb: Optional[int] = None  # heap limit applied to all nodes (None = no limit)
    seed: Optional[int] = None


@dataclass
class TopologyConfig:
    nodes: list[NodeConfig]
    edges: list[EdgeConfig]
    simulation: SimulationConfig
    radio: Optional[RadioConfig] = None   # present only when topology defines RF params


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


def topology_to_dict(topo: "TopologyConfig") -> dict:
    """
    Serialise a TopologyConfig back to the standard topology JSON dict.

    The returned dict is JSON-serialisable and can be passed to json.dump().
    It round-trips through load_topology() without loss (optional fields are
    omitted when they hold their default / None value).
    """
    nodes_out = []
    for n in topo.nodes:
        d: dict = {"name": n.name}
        if n.relay:
            d["relay"] = True
        if n.room_server:
            d["room_server"] = True
        if n.prv_key is not None:
            d["prv_key"] = n.prv_key
        if n.binary is not None:
            d["binary"] = n.binary
        if n.max_heap_kb is not None:
            d["max_heap_kb"] = n.max_heap_kb
        if n.lat is not None:
            d["lat"] = n.lat
        if n.lon is not None:
            d["lon"] = n.lon
        if n.adversarial is not None:
            a = n.adversarial
            ad: dict = {"mode": a.mode}
            if a.probability != 1.0:
                ad["probability"] = a.probability
            if a.replay_delay_ms != 5000.0:
                ad["replay_delay_ms"] = a.replay_delay_ms
            if a.corrupt_byte_count != 1:
                ad["corrupt_byte_count"] = a.corrupt_byte_count
            d["adversarial"] = ad
        nodes_out.append(d)

    def _dir_overrides(o: Optional["DirectionalOverrides"]) -> Optional[dict]:
        if o is None:
            return None
        d = {}
        if o.loss       is not None: d["loss"]       = o.loss
        if o.latency_ms is not None: d["latency_ms"] = o.latency_ms
        if o.snr        is not None: d["snr"]        = o.snr
        if o.rssi       is not None: d["rssi"]       = o.rssi
        return d or None

    edges_out = []
    for e in topo.edges:
        ed: dict = {"a": e.a, "b": e.b}
        if e.loss       != 0.0:  ed["loss"]       = e.loss
        if e.latency_ms != 0.0:  ed["latency_ms"] = e.latency_ms
        if e.snr        != 6.0:  ed["snr"]        = e.snr
        if e.rssi       != -90.0: ed["rssi"]      = e.rssi
        atob = _dir_overrides(e.a_to_b)
        btoa = _dir_overrides(e.b_to_a)
        if atob: ed["a_to_b"] = atob
        if btoa: ed["b_to_a"] = btoa
        edges_out.append(ed)

    s = topo.simulation
    sim_out: dict = {
        "warmup_secs":           s.warmup_secs,
        "duration_secs":         s.duration_secs,
        "traffic_interval_secs": s.traffic_interval_secs,
        "advert_interval_secs":  s.advert_interval_secs,
        "epoch":                 s.epoch,
        "default_binary":        s.default_binary,
    }
    if s.default_max_heap_kb is not None:
        sim_out["default_max_heap_kb"] = s.default_max_heap_kb
    if s.seed is not None:
        sim_out["seed"] = s.seed

    result: dict = {"nodes": nodes_out, "edges": edges_out, "simulation": sim_out}
    if topo.radio is not None:
        r = topo.radio
        result["radio"] = {
            "sf":              r.sf,
            "bw_hz":           r.bw_hz,
            "cr":              r.cr,
            "preamble_symbols": r.preamble_symbols,
        }
    return result


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
        raw_lat = n.get("lat")
        raw_lon = n.get("lon")
        raw_heap = n.get("max_heap_kb")
        nodes.append(NodeConfig(
            name=n["name"],
            relay=bool(n.get("relay", False)),
            room_server=bool(n.get("room_server", False)),
            prv_key=n.get("prv_key"),
            adversarial=adv,
            binary=n.get("binary"),
            max_heap_kb=int(raw_heap) if raw_heap is not None else None,
            lat=float(raw_lat) if raw_lat is not None else None,
            lon=float(raw_lon) if raw_lon is not None else None,
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
        default_max_heap_kb=(
            int(sim_raw["default_max_heap_kb"])
            if sim_raw.get("default_max_heap_kb") is not None else None
        ),
        seed=sim_raw.get("seed"),
    )
    if sim.epoch == 0:
        sim.epoch = int(time.time())

    radio = None
    radio_raw = raw.get("radio")
    if radio_raw is not None:
        radio = RadioConfig(
            sf=int(radio_raw.get("sf", 9)),
            bw_hz=int(radio_raw.get("bw_hz", 125_000)),
            cr=int(radio_raw.get("cr", 1)),
            preamble_symbols=int(radio_raw.get("preamble_symbols", 8)),
        )

    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim, radio=radio)
