"""
tracer.py — PacketTracer: correlates packet copies across nodes for path analysis.

Each packet is identified by its fingerprint (payload type + encrypted payload),
which is stable across all hops.  The tracer records every (sender→receiver) hop
observed by the orchestrator, building a per-packet witness list.

This is the foundational observability tool for privacy-preserving routing
research: it answers questions like "how many nodes witnessed this message?",
"which relays forwarded it?", and "was it flood- or direct-routed?".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .packet import (
    PacketInfo,
    decode_packet,
    packet_fingerprint,
    payload_type_name,
    route_type_name,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HopRecord:
    """One observed radio transmission (sender → receiver) of a given packet."""
    t: float               # asyncio event-loop time of delivery
    sender: str            # node that transmitted
    receiver: str          # node that received (after latency + filters)
    route_type: int        # route type from the packet header at this hop
    path_count: int        # number of relay hashes in path[] at TX time
    tx_id: Optional[int]   # monotonic counter, one per TX event;
                           # all hops sharing a tx_id came from the same broadcast
    airtime_ms: float = 0.0  # on-air time of the TX event (0 when not modelled)
    size_bytes: int = 0    # wire-format byte length of the packet at this hop
                           # (flood packets grow as relays append their hashes)


@dataclass
class CollisionRecord:
    """One RF collision event: a delivery blocked by simultaneous interference."""
    t: float               # asyncio event-loop time when collision was detected
    sender: str            # node that transmitted
    receiver: str          # intended receiver that did not get the packet
    tx_id: Optional[int]   # same tx_id as the corresponding record_tx() call


@dataclass
class PacketTrace:
    """Everything we know about one logical packet (identified by fingerprint)."""
    fingerprint:    str          # stable correlation key
    payload_type:   int
    first_seen_at:  float        # loop time of first record_tx() call
    first_sender:   str          # node that first transmitted this packet
    hops:           list[HopRecord]       = field(default_factory=list)
    collisions:     list[CollisionRecord] = field(default_factory=list)

    @property
    def witness_count(self) -> int:
        """Number of (sender, receiver) pairs that observed this packet."""
        return len(self.hops)

    @property
    def unique_senders(self) -> set[str]:
        return {h.sender for h in self.hops}

    @property
    def unique_receivers(self) -> set[str]:
        return {h.receiver for h in self.hops}

    @property
    def avg_size_bytes(self) -> float:
        """
        Mean wire-format packet size in bytes across all hops.

        For flood packets this grows as relays append relay hashes; for direct
        packets it is constant.  Returns 0.0 if no hops have been recorded yet.
        """
        sizes = [h.size_bytes for h in self.hops if h.size_bytes > 0]
        return sum(sizes) / len(sizes) if sizes else 0.0

    def is_flood(self) -> bool:
        return any(
            h.route_type in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD)
            for h in self.hops
        )


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------

class PacketTracer:
    """
    Correlates every packet transmission and delivery across the simulated network.

    Usage (called by PacketRouter):
        fingerprint = tracer.record_tx(sender_name, hex_data, loop_time)
        # ... after latency + filters pass ...
        tracer.record_rx(sender_name, receiver_name, hex_data, loop_time)

    After simulation, call tracer.report() for a human-readable summary.
    All trace data is also available via tracer.traces for programmatic analysis.
    """

    def __init__(self) -> None:
        # fingerprint → PacketTrace
        self._traces: dict[str, PacketTrace] = {}
        # Monotonic counter: incremented once per TX event so all deliveries
        # from the same broadcast share a tx_id.
        self._tx_counter: int = 0
        # tx_id → airtime_ms recorded at TX time (0.0 when not modelled)
        self._tx_airtime: dict[int, float] = {}
        # tx_id → (sender, tx_time) for relay-delay computation
        self._tx_events: dict[int, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_tx(
        self, sender: str, hex_data: str, t: float, airtime_ms: float = 0.0
    ) -> Optional[int]:
        """
        Register a TX event.  Creates a new PacketTrace if this fingerprint
        is new.  Returns a tx_id (monotonic int) that should be passed to every
        record_rx() call that belongs to this broadcast, or None if the packet
        cannot be decoded.

        airtime_ms — on-air duration of this broadcast (0.0 when not modelled).
        """
        info = decode_packet(hex_data)
        if info is None:
            return None
        fp = packet_fingerprint(info)
        if fp not in self._traces:
            self._traces[fp] = PacketTrace(
                fingerprint=fp,
                payload_type=info.payload_type,
                first_seen_at=t,
                first_sender=sender,
            )
        self._tx_counter += 1
        self._tx_airtime[self._tx_counter] = airtime_ms
        self._tx_events[self._tx_counter] = (sender, t)
        return self._tx_counter

    def record_collision(
        self,
        sender: str,
        receiver: str,
        hex_data: str,
        t: float,
        tx_id: Optional[int] = None,
    ) -> None:
        """
        Register an RF collision: a delivery was blocked because another
        transmission overlapped at the receiver.  Called from
        PacketRouter._deliver_to after the collision check fires.

        tx_id should be the value returned by the corresponding record_tx() call.
        """
        info = decode_packet(hex_data)
        if info is None:
            return
        fp = packet_fingerprint(info)
        trace = self._traces.get(fp)
        if trace is None:
            # record_tx should always precede this call; create defensively.
            trace = PacketTrace(
                fingerprint=fp,
                payload_type=info.payload_type,
                first_seen_at=t,
                first_sender=sender,
            )
            self._traces[fp] = trace
        trace.collisions.append(CollisionRecord(
            t=t,
            sender=sender,
            receiver=receiver,
            tx_id=tx_id,
        ))

    def record_rx(
        self,
        sender: str,
        receiver: str,
        hex_data: str,
        t: float,
        tx_id: Optional[int] = None,
    ) -> None:
        """
        Register a successful delivery.  This is called *after* the link loss
        check and adversarial filter, so it reflects packets that actually reach
        the receiving node's radio queue.

        tx_id should be the value returned by the corresponding record_tx() call;
        all deliveries sharing a tx_id originated from the same broadcast event.
        """
        info = decode_packet(hex_data)
        if info is None:
            return
        fp = packet_fingerprint(info)
        trace = self._traces.get(fp)
        if trace is None:
            # record_tx should always have been called first; create defensively.
            trace = PacketTrace(
                fingerprint=fp,
                payload_type=info.payload_type,
                first_seen_at=t,
                first_sender=sender,
            )
            self._traces[fp] = trace
        airtime_ms = self._tx_airtime.get(tx_id, 0.0) if tx_id is not None else 0.0
        trace.hops.append(HopRecord(
            t=t,
            sender=sender,
            receiver=receiver,
            route_type=info.route_type,
            path_count=info.path_count,
            tx_id=tx_id,
            airtime_ms=airtime_ms,
            size_bytes=len(hex_data) // 2,
        ))

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @property
    def traces(self) -> dict[str, PacketTrace]:
        """All observed packet traces, keyed by fingerprint."""
        return dict(self._traces)

    def compute_relay_delays(self) -> list[float]:
        """Relay retransmit delays in ms (rx-to-tx gap for relay forwarding).

        For each packet, finds nodes that received it and later retransmitted
        it (excluding the packet's originator).  Returns a list of delay
        values in milliseconds.
        """
        delays: list[float] = []
        for trace in self._traces.values():
            # Earliest time each node received this packet
            first_rx: dict[str, float] = {}
            tx_ids: set[int] = set()
            for hop in trace.hops:
                if hop.receiver not in first_rx or hop.t < first_rx[hop.receiver]:
                    first_rx[hop.receiver] = hop.t
                if hop.tx_id is not None:
                    tx_ids.add(hop.tx_id)

            # For each TX event, check if sender previously received this packet
            for tx_id in tx_ids:
                ev = self._tx_events.get(tx_id)
                if ev is None:
                    continue
                sender, tx_time = ev
                if sender in first_rx and sender != trace.first_sender:
                    delay_ms = (tx_time - first_rx[sender]) * 1000.0
                    if delay_ms >= 0:
                        delays.append(delay_ms)
        return delays

    def traces_by_type(self) -> dict[int, list[PacketTrace]]:
        """Group traces by payload type."""
        groups: dict[int, list[PacketTrace]] = defaultdict(list)
        for tr in self._traces.values():
            groups[tr.payload_type].append(tr)
        return dict(groups)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(
        self,
        topology_path: Optional[str] = None,
        node_names: Optional[list] = None,
        metrics: Optional[dict] = None,
        total_sim_secs: float = 0.0,
    ) -> dict:
        """
        Return a JSON-serialisable dict of all trace data.

        Optional metadata parameters:
          topology_path — path to the topology JSON that was simulated; the
                          filename (basename) is stored so the visualiser can
                          warn when a trace is opened with the wrong topology.
          node_names    — list of node names in the simulation; stored so the
                          visualiser can cross-check node identity.

        Schema (version 2):
          {
            "schema_version": 2,
            "topology": "<basename>.json",   # if topology_path provided
            "nodes": ["name", ...],           # if node_names provided
            "packets": [
              {
                "fingerprint":      str,
                "payload_type":     int,
                "payload_type_name": str,
                "first_seen_at":    float,
                "first_sender":     str,
                "is_flood":         bool,
                "witness_count":    int,
                "unique_senders":   [str, ...],
                "unique_receivers": [str, ...],
                "hops": [
                  {"t": float, "sender": str, "receiver": str,
                   "route_type": int, "path_count": int,
                   "tx_id": int|null, "airtime_ms": float},
                  ...
                ],
                "collisions": [
                  {"t": float, "sender": str, "receiver": str, "tx_id": int|null},
                  ...
                ]
              },
              ...
            ]
          }
        """
        packets = []
        for tr in self._traces.values():
            packets.append({
                "fingerprint":       tr.fingerprint,
                "payload_type":      tr.payload_type,
                "payload_type_name": payload_type_name(tr.payload_type),
                "first_seen_at":     tr.first_seen_at,
                "first_sender":      tr.first_sender,
                "is_flood":          tr.is_flood(),
                "witness_count":     tr.witness_count,
                "unique_senders":    sorted(tr.unique_senders),
                "unique_receivers":  sorted(tr.unique_receivers),
                "avg_size_bytes":     tr.avg_size_bytes,
                "hops": [
                    {
                        "t":           h.t,
                        "sender":      h.sender,
                        "receiver":    h.receiver,
                        "route_type":  h.route_type,
                        "path_count":  h.path_count,
                        "tx_id":       h.tx_id,
                        "airtime_ms":  h.airtime_ms,
                        "size_bytes":  h.size_bytes,
                    }
                    for h in tr.hops
                ],
                "collisions": [
                    {
                        "t":        c.t,
                        "sender":   c.sender,
                        "receiver": c.receiver,
                        "tx_id":    c.tx_id,
                    }
                    for c in tr.collisions
                ],
            })
        # Sort by first_seen_at so the file reads chronologically
        packets.sort(key=lambda p: p["first_seen_at"])
        result: dict = {"schema_version": 2}
        if topology_path is not None:
            result["topology"] = Path(topology_path).name
        if node_names is not None:
            result["nodes"] = sorted(node_names)
        result["packets"] = packets

        # Embed timing analysis computed from trace data
        timing: dict = {}
        all_airtimes = [h.airtime_ms for tr in self._traces.values()
                        for h in tr.hops if h.airtime_ms > 0]
        if all_airtimes:
            timing["avg_airtime_ms"] = sum(all_airtimes) / len(all_airtimes)
            timing["n_hops"] = len(all_airtimes)

        relay_delays = self.compute_relay_delays()
        if relay_delays:
            timing["relay_delay_min_ms"] = min(relay_delays)
            timing["relay_delay_avg_ms"] = sum(relay_delays) / len(relay_delays)
            timing["relay_delay_max_ms"] = max(relay_delays)
            timing["n_relays"] = len(relay_delays)

        flood_props: list[float] = []
        for tr in self._traces.values():
            if tr.is_flood() and tr.hops:
                span = (max(h.t for h in tr.hops) - tr.first_seen_at) * 1000.0
                if span > 0:
                    flood_props.append(span)
        if flood_props:
            timing["flood_prop_min_ms"] = min(flood_props)
            timing["flood_prop_avg_ms"] = sum(flood_props) / len(flood_props)
            timing["flood_prop_max_ms"] = max(flood_props)
            timing["n_floods"] = len(flood_props)

        if total_sim_secs > 0:
            unique_tx_airtime: dict[int, float] = {}
            for tr in self._traces.values():
                for h in tr.hops:
                    if h.tx_id is not None and h.airtime_ms > 0:
                        unique_tx_airtime[h.tx_id] = h.airtime_ms
            if unique_tx_airtime:
                total_airtime_s = sum(unique_tx_airtime.values()) / 1000.0
                timing["channel_utilization_pct"] = total_airtime_s / total_sim_secs * 100.0
                timing["total_airtime_s"] = total_airtime_s
                timing["n_unique_tx"] = len(unique_tx_airtime)
                timing["total_sim_secs"] = total_sim_secs

        if timing:
            result["timing"] = timing

        # Embed MetricsCollector data (delivery, latency, drops, etc.)
        if metrics is not None:
            result["metrics"] = metrics

        return result

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self, total_sim_secs: float = 0.0) -> str:
        """
        Return a formatted summary suitable for the end-of-simulation report.

        Sections:
          1. Overall summary (unique packets, total hops, etc.)
          2. Per-payload-type breakdown (witness counts, route mode distribution)
          3. High-exposure packets — those seen by the most nodes (privacy risk)
        """
        lines: list[str] = [
            "",
            "=" * 60,
            "  Packet Path Trace",
            "=" * 60,
            "",
        ]

        if not self._traces:
            lines.append("  (no packets recorded)")
            lines.append("=" * 60)
            return "\n".join(lines)

        total_pkts   = len(self._traces)
        total_hops   = sum(tr.witness_count for tr in self._traces.values())
        flood_pkts   = sum(1 for tr in self._traces.values() if tr.is_flood())
        direct_pkts  = total_pkts - flood_pkts

        lines.append(f"  Unique packets:   {total_pkts}")
        lines.append(f"  Total deliveries: {total_hops}")
        lines.append(f"  Flood-routed:     {flood_pkts}")
        lines.append(f"  Direct-routed:    {direct_pkts}")
        if total_pkts > 0:
            amp = total_hops / total_pkts
            lines.append(f"  Flood amplification: {amp:.1f}x  ({total_hops} hops / {total_pkts} packets)")
        lines.append("")

        # --- Per-type breakdown ---
        by_type = self.traces_by_type()
        lines.append(f"  {'Type':<16}  {'Count':>5}  {'Avg witnesses':>13}  {'Max witnesses':>13}")
        lines.append(f"  {'-'*16}  {'-'*5}  {'-'*13}  {'-'*13}")
        for pt in sorted(by_type.keys()):
            trs = by_type[pt]
            witnesses = [tr.witness_count for tr in trs]
            avg_w = sum(witnesses) / len(witnesses) if witnesses else 0.0
            max_w = max(witnesses) if witnesses else 0
            name  = payload_type_name(pt)
            lines.append(
                f"  {name:<16}  {len(trs):>5}  {avg_w:>13.1f}  {max_w:>13}"
            )
        lines.append("")

        # --- Timing ---
        all_airtimes = [h.airtime_ms for tr in self._traces.values()
                        for h in tr.hops if h.airtime_ms > 0]
        relay_delays = self.compute_relay_delays()
        flood_props: list[float] = []
        for tr in self._traces.values():
            if tr.is_flood() and tr.hops:
                span = (max(h.t for h in tr.hops) - tr.first_seen_at) * 1000.0
                if span > 0:
                    flood_props.append(span)

        has_timing = all_airtimes or relay_delays or flood_props
        if has_timing:
            lines.append("  Timing:")
            if all_airtimes:
                avg_at = sum(all_airtimes) / len(all_airtimes)
                lines.append(
                    f"    Avg airtime per hop:       {avg_at:.1f} ms"
                    f"  ({len(all_airtimes)} hops)"
                )
            if relay_delays:
                lines.append(
                    f"    Relay retransmit delay:    "
                    f"min={min(relay_delays):.0f}ms  "
                    f"avg={sum(relay_delays)/len(relay_delays):.0f}ms  "
                    f"max={max(relay_delays):.0f}ms"
                    f"  ({len(relay_delays)} relays)"
                )
            if flood_props:
                lines.append(
                    f"    Flood propagation time:    "
                    f"min={min(flood_props):.0f}ms  "
                    f"avg={sum(flood_props)/len(flood_props):.0f}ms  "
                    f"max={max(flood_props):.0f}ms"
                    f"  ({len(flood_props)} floods)"
                )
            # Channel utilization: deduplicate by tx_id so each radio
            # transmission is counted once (not once per receiver).
            if total_sim_secs > 0:
                unique_tx_airtime: dict[int, float] = {}
                for tr in self._traces.values():
                    for h in tr.hops:
                        if h.tx_id is not None and h.airtime_ms > 0:
                            unique_tx_airtime[h.tx_id] = h.airtime_ms
                if unique_tx_airtime:
                    total_airtime_s = sum(unique_tx_airtime.values()) / 1000.0
                    n_tx = len(unique_tx_airtime)
                    util = total_airtime_s / total_sim_secs * 100.0
                    lines.append(
                        f"    Channel utilization:       {util:.1f}%"
                        f"  ({total_airtime_s:.1f}s airtime from {n_tx} TXs in {total_sim_secs:.0f}s)"
                    )
            lines.append("")

        # --- High-exposure packets (top 10 by witness count) ---
        sorted_traces = sorted(
            self._traces.values(),
            key=lambda tr: tr.witness_count,
            reverse=True,
        )[:10]

        lines.append("  Highest-exposure packets (witnesses = nodes that received a copy):")
        lines.append("")
        for tr in sorted_traces:
            pname = payload_type_name(tr.payload_type)
            fp_short = tr.fingerprint[:16] + ("…" if len(tr.fingerprint) > 16 else "")
            # Build a compact hop summary: first_sender → unique intermediate nodes
            senders = [tr.first_sender] + sorted(tr.unique_senders - {tr.first_sender})
            receivers = sorted(tr.unique_receivers)
            route = "FLOOD" if tr.is_flood() else "DIRECT"
            lines.append(
                f"    [{pname:<12}] {fp_short}  "
                f"witnesses={tr.witness_count}  route={route}"
            )
            lines.append(
                f"      senders:   {', '.join(senders)}"
            )
            lines.append(
                f"      receivers: {', '.join(receivers)}"
            )

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)
