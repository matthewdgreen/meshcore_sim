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
    extract_payload_ids,
    packet_fingerprint,
    path_hash_list,
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
    path_hashes: Optional[list] = None
                           # list of hex-encoded relay hashes observed at this hop,
                           # e.g. ["0a1b", "c3d4"] for 2-byte hashes.
                           # Each entry is path_size*2 hex chars.
    path_hash_size: int = 1  # bytes per path hash entry (1, 2, or 3)


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
    # Payload-level observable fields (populated from the first TX)
    dest_hash_hex:  Optional[str] = None   # 1-byte dest hash (hex) for data pkts
    src_hash_hex:   Optional[str] = None   # 1-byte src hash (hex) for data pkts
    advert_pub_hex: Optional[str] = None   # 32-byte pub key (hex) for ADVERT pkts

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
            ids = extract_payload_ids(info)
            self._traces[fp] = PacketTrace(
                fingerprint=fp,
                payload_type=info.payload_type,
                first_seen_at=t,
                first_sender=sender,
                dest_hash_hex=ids.get("dest_hash_hex"),
                src_hash_hex=ids.get("src_hash_hex"),
                advert_pub_hex=ids.get("advert_pub_hex"),
            )
        self._tx_counter += 1
        self._tx_airtime[self._tx_counter] = airtime_ms
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
            path_hashes=path_hash_list(info),
            path_hash_size=info.path_size,
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

        Schema (version 3):
          {
            "schema_version": 3,
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
                "dest_hash_hex":    str|null,     # 1-byte dest hash (data pkts)
                "src_hash_hex":     str|null,     # 1-byte src hash (data pkts)
                "advert_pub_hex":   str|null,     # 32-byte pub key (ADVERT pkts)
                "hops": [
                  {"t": float, "sender": str, "receiver": str,
                   "route_type": int, "path_count": int,
                   "tx_id": int|null, "airtime_ms": float,
                   "path_hashes": [str, ...], "path_hash_size": int},
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
            pkt_dict: dict = {
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
            }
            # Privacy-observable fields (only present when non-None)
            if tr.dest_hash_hex is not None:
                pkt_dict["dest_hash_hex"] = tr.dest_hash_hex
            if tr.src_hash_hex is not None:
                pkt_dict["src_hash_hex"] = tr.src_hash_hex
            if tr.advert_pub_hex is not None:
                pkt_dict["advert_pub_hex"] = tr.advert_pub_hex
            pkt_dict["hops"] = [
                    {
                        "t":              h.t,
                        "sender":         h.sender,
                        "receiver":       h.receiver,
                        "route_type":     h.route_type,
                        "path_count":     h.path_count,
                        "tx_id":          h.tx_id,
                        "airtime_ms":     h.airtime_ms,
                        "size_bytes":     h.size_bytes,
                        "path_hashes":    h.path_hashes,
                        "path_hash_size": h.path_hash_size,
                    }
                    for h in tr.hops
                ]
            pkt_dict["collisions"] = [
                    {
                        "t":        c.t,
                        "sender":   c.sender,
                        "receiver": c.receiver,
                        "tx_id":    c.tx_id,
                    }
                    for c in tr.collisions
                ]
            packets.append(pkt_dict)
        # Sort by first_seen_at so the file reads chronologically
        packets.sort(key=lambda p: p["first_seen_at"])
        result: dict = {"schema_version": 3}
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
