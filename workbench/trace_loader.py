"""trace_loader.py — Parse trace JSON, flatten events, compute stats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_trace(path: str | Path) -> dict[str, Any]:
    """Load a trace JSON file and return the raw dict."""
    with open(path) as f:
        return json.load(f)


def flatten_events(trace: dict) -> list[dict]:
    """Flatten all hops + collisions into one time-sorted event list.

    Each event dict contains:
      type:      'hop' | 'collision'
      t:         float (absolute sim time)
      pkt_idx:   int (index into trace['packets'])
      sender:    str
      receiver:  str
      tx_id:     int | None
      airtime_ms: float (hop only)
    """
    events: list[dict] = []
    for pkt_idx, pkt in enumerate(trace.get("packets", [])):
        for hop in pkt.get("hops", []):
            events.append({
                "type": "hop",
                "t": hop["t"],
                "pkt_idx": pkt_idx,
                "sender": hop["sender"],
                "receiver": hop["receiver"],
                "tx_id": hop.get("tx_id"),
                "airtime_ms": hop.get("airtime_ms", 0),
            })
        for col in pkt.get("collisions", []):
            events.append({
                "type": "collision",
                "t": col["t"],
                "pkt_idx": pkt_idx,
                "sender": col["sender"],
                "receiver": col["receiver"],
                "tx_id": col.get("tx_id"),
            })
    events.sort(key=lambda e: e["t"])
    return events


def broadcast_steps(pkt: dict) -> list[list[dict]]:
    """Group a packet's hops into broadcast steps ordered by tx_id.

    All hops sharing a tx_id came from the same on-air transmission.
    Hops without a tx_id each form their own singleton group.
    """
    seen: dict = {}
    for h in pkt.get("hops", []):
        key = h.get("tx_id")
        if key is None:
            key = id(h)
        if key not in seen:
            seen[key] = []
        seen[key].append(h)
    return list(seen.values())


def compute_node_trace_stats(trace: dict) -> dict[str, dict]:
    """Per-node trace statistics.

    Returns {node_name: {tx_count, rx_count, packets_originated: [idx...],
             packets_transited: [idx...], collisions_involved: int}}.
    """
    stats: dict[str, dict] = {}

    def _ensure(name: str) -> dict:
        if name not in stats:
            stats[name] = {
                "tx_count": 0,
                "rx_count": 0,
                "packets_originated": [],
                "packets_transited": [],
                "collisions_involved": 0,
            }
        return stats[name]

    for pkt_idx, pkt in enumerate(trace.get("packets", [])):
        first_sender = pkt.get("first_sender")
        if first_sender:
            _ensure(first_sender)["packets_originated"].append(pkt_idx)

        senders_seen: set[str] = set()
        receivers_seen: set[str] = set()
        for hop in pkt.get("hops", []):
            s, r = hop["sender"], hop["receiver"]
            if s not in senders_seen:
                _ensure(s)["tx_count"] += 1
                senders_seen.add(s)
                # Transit = forwarded (sent but not the originator)
                if s != first_sender and pkt_idx not in _ensure(s)["packets_transited"]:
                    _ensure(s)["packets_transited"].append(pkt_idx)
            if r not in receivers_seen:
                _ensure(r)["rx_count"] += 1
                receivers_seen.add(r)

        for col in pkt.get("collisions", []):
            _ensure(col["receiver"])["collisions_involved"] += 1

    return stats


def compute_edge_trace_stats(trace: dict) -> dict[tuple[str, str], dict]:
    """Per-edge trace statistics.

    Returns {(a,b): {hop_count, collision_count, packets: [idx...]}}.
    Keys are sorted so (a,b)==(b,a).
    """
    stats: dict[tuple[str, str], dict] = {}

    def _key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def _ensure(k: tuple[str, str]) -> dict:
        if k not in stats:
            stats[k] = {"hop_count": 0, "collision_count": 0, "packets": []}
        return stats[k]

    for pkt_idx, pkt in enumerate(trace.get("packets", [])):
        for hop in pkt.get("hops", []):
            k = _key(hop["sender"], hop["receiver"])
            entry = _ensure(k)
            entry["hop_count"] += 1
            if pkt_idx not in entry["packets"]:
                entry["packets"].append(pkt_idx)
        for col in pkt.get("collisions", []):
            k = _key(col["sender"], col["receiver"])
            _ensure(k)["collision_count"] += 1

    return stats


def compute_trace_stats(trace: dict) -> dict:
    """Compute comprehensive statistics from trace data.

    Returns a dict with sections: summary, timing, metrics (if embedded),
    and per-type breakdown.
    """
    packets = trace.get("packets", [])
    n = len(packets)
    if n == 0:
        return {
            "n_packets": 0, "flood_pct": 0.0,
            "avg_witnesses": 0.0, "n_collisions": 0,
        }

    n_flood = sum(1 for p in packets if p.get("is_flood"))
    total_hops = sum(p.get("witness_count", 0) for p in packets)
    avg_w = total_hops / n
    n_col = sum(len(p.get("collisions", [])) for p in packets)
    n_direct = n - n_flood

    result: dict = {
        "n_packets": n,
        "flood_pct": 100.0 * n_flood / n,
        "avg_witnesses": avg_w,
        "n_collisions": n_col,
        "total_hops": total_hops,
        "n_flood": n_flood,
        "n_direct": n_direct,
        "flood_amplification": total_hops / n if n else 0.0,
    }

    # Per payload-type breakdown
    by_type: dict[str, dict] = {}
    for p in packets:
        tname = p.get("payload_type_name", "?")
        if tname not in by_type:
            by_type[tname] = {"count": 0, "witnesses": [], "collisions": 0}
        by_type[tname]["count"] += 1
        by_type[tname]["witnesses"].append(p.get("witness_count", 0))
        by_type[tname]["collisions"] += len(p.get("collisions", []))
    type_breakdown = {}
    for tname, info in sorted(by_type.items()):
        ws = info["witnesses"]
        type_breakdown[tname] = {
            "count": info["count"],
            "avg_witnesses": sum(ws) / len(ws) if ws else 0.0,
            "max_witnesses": max(ws) if ws else 0,
            "collisions": info["collisions"],
        }
    result["by_type"] = type_breakdown

    # Timing stats (embedded by tracer.to_dict)
    timing = trace.get("timing")
    if timing:
        result["timing"] = timing

    # Metrics (embedded by metrics.to_dict via tracer.to_dict)
    metrics = trace.get("metrics")
    if metrics:
        result["metrics"] = metrics

    return result
