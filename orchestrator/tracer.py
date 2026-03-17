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


@dataclass
class PacketTrace:
    """Everything we know about one logical packet (identified by fingerprint)."""
    fingerprint:    str          # stable correlation key
    payload_type:   int
    first_seen_at:  float        # loop time of first record_tx() call
    first_sender:   str          # node that first transmitted this packet
    hops:           list[HopRecord] = field(default_factory=list)

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

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_tx(self, sender: str, hex_data: str, t: float) -> Optional[int]:
        """
        Register a TX event.  Creates a new PacketTrace if this fingerprint
        is new.  Returns a tx_id (monotonic int) that should be passed to every
        record_rx() call that belongs to this broadcast, or None if the packet
        cannot be decoded.
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
        return self._tx_counter

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
        trace.hops.append(HopRecord(
            t=t,
            sender=sender,
            receiver=receiver,
            route_type=info.route_type,
            path_count=info.path_count,
            tx_id=tx_id,
        ))

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @property
    def traces(self) -> dict[str, PacketTrace]:
        """All observed packet traces, keyed by fingerprint."""
        return dict(self._traces)

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
    ) -> dict:
        """
        Return a JSON-serialisable dict of all trace data.

        Optional metadata parameters:
          topology_path — path to the topology JSON that was simulated; the
                          filename (basename) is stored so the visualiser can
                          warn when a trace is opened with the wrong topology.
          node_names    — list of node names in the simulation; stored so the
                          visualiser can cross-check node identity.

        Schema (version 1):
          {
            "schema_version": 1,
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
                   "route_type": int, "path_count": int},
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
                "hops": [
                    {
                        "t":          h.t,
                        "sender":     h.sender,
                        "receiver":   h.receiver,
                        "route_type": h.route_type,
                        "path_count": h.path_count,
                        "tx_id":      h.tx_id,
                    }
                    for h in tr.hops
                ],
            })
        # Sort by first_seen_at so the file reads chronologically
        packets.sort(key=lambda p: p["first_seen_at"])
        result: dict = {"schema_version": 1}
        if topology_path is not None:
            result["topology"] = Path(topology_path).name
        if node_names is not None:
            result["nodes"] = sorted(node_names)
        result["packets"] = packets
        return result

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> str:
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
