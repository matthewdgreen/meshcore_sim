"""
privacy.py — Privacy metrics for MeshCore routing experiments.

Analyzes trace data to quantify information leakage under different attacker
models.  Designed to compare baseline (stock MeshCore) against privacy-
enhanced variants like PNI (Permuted Neighbor Identifiers).

Attacker models
===============

1. Passive Eavesdropper (single relay)
   Sees all packets that pass through it.  Can it identify which relay
   forwarded a given packet based on path hashes?

   Metric: **Path Hash Entropy** — for each relay, the ratio of unique
   path hashes emitted to total forwards.  1.0 = fully unlinkable,
   1/N = fully deterministic.

2. Colluding Endpoints
   Two endpoints compare their received paths.  Can they determine whether
   they share relay(s)?

   Metric: **Cross-Path Relay Linkability** — fraction of relay-position
   pairs that match across different delivered messages.  Lower = better.

3. Global Observer
   Sees all radio traffic.  Tries to reconstruct relay identity from
   observed path hashes across all packets.

   Metric: **Relay Anonymity Set** — for each observed hash value, how many
   real relays could have produced it?  Higher = better privacy.

4. Identity Leakage via Adverts & Payload Headers
   Adverts broadcast the full 32-byte pub_key.  Data packet payloads
   contain 1-byte src_hash and dest_hash visible to all relays.

   Metric: **Identity Exposure Count** — number of distinct pub_keys and
   hash values observable in cleartext.

5. PNI Table Stress
   As the PNI table fills (128 FIFO entries), old entries are evicted.
   If a direct reply arrives after eviction, the relay won't recognise its
   own PNI — causing a routing failure.

   Metric: **PNI Forwards per Relay** — max forwards through the busiest
   relay. Values > PNI_TABLE_CAP (128) indicate eviction risk.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from orchestrator.packet import (
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_TXT_MSG,
    PAYLOAD_TYPE_REQ,
    PAYLOAD_TYPE_RESPONSE,
    PAYLOAD_TYPE_PATH,
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RelayPrivacy:
    """Per-relay privacy metrics."""
    name: str
    total_forwards: int         # times this relay appended a path hash
    unique_hashes: int          # distinct hash values emitted
    entropy_ratio: float        # unique_hashes / total_forwards (1.0 = ideal)
    hashes: list                # list of (hash_hex, count) most-frequent-first


@dataclass
class PrivacyReport:
    """Aggregate privacy metrics for one experiment run."""

    # -- Path Hash Entropy (Attacker 1: passive eavesdropper) --
    relay_privacy: list         # list[RelayPrivacy], one per relay
    avg_entropy_ratio: float    # mean entropy_ratio across all relays
    min_entropy_ratio: float    # worst-case relay

    # -- Cross-Path Linkability (Attacker 2: colluding endpoints) --
    linkable_pairs: int         # number of matching (position, hash) across msg pairs
    total_comparisons: int      # total (position, hash) pairs compared
    linkability_rate: float     # linkable_pairs / total_comparisons

    # -- Relay Anonymity Set (Attacker 3: global observer) --
    # For each observed hash value, how many distinct real relays emitted it
    avg_anonymity_set: float    # mean across all observed hash values
    min_anonymity_set: int      # worst case (1 = fully identified)

    # -- Identity Exposure (Attacker 4: advert/payload leakage) --
    advert_pub_keys_exposed: int    # distinct pub_keys broadcast in adverts
    src_hashes_observed: int        # distinct src_hash values in data packets
    dest_hashes_observed: int       # distinct dest_hash values in data packets

    # -- PNI Table Stress (Attacker 5 / functional correctness) --
    max_relay_forwards: int     # highest forward count for any single relay
    pni_table_cap: int          # PNI_TABLE_CAP for reference (128)
    eviction_risk: bool         # True if max_relay_forwards > pni_table_cap


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _extract_relay_forwards(trace_data: dict) -> dict:
    """
    For each relay that forwarded packets (appended a path hash), collect
    the hash values it wrote.

    Returns: {relay_name: [hash_hex, hash_hex, ...]}

    Logic: when a packet's path_hashes at a hop has more entries than at the
    previous hop from the same packet (i.e., the path grew), the new entry
    was written by the sender of this hop.  For flood packets, path grows;
    for direct, path shrinks.
    """
    # Group hops by packet fingerprint, sorted by time, to track path growth.
    relay_hashes: dict = defaultdict(list)  # relay_name -> [hash_hex, ...]

    for pkt in trace_data.get("packets", []):
        # Track path hashes at each tx_id to detect when a relay appends.
        # Within a packet, group hops by tx_id (same broadcast).
        hops_by_tx: dict = defaultdict(list)
        for h in pkt.get("hops", []):
            tx_id = h.get("tx_id")
            if tx_id is not None:
                hops_by_tx[tx_id].append(h)

        # For flood packets: all hops from the same tx_id share the same
        # path_hashes (the sender's state at TX time).  The sender appended
        # the LAST hash in path_hashes if path_count > 0 and it wasn't
        # present in the "previous" transmission of this packet.
        #
        # Simpler approach: track path_hashes length at each tx_id.
        # If tx_id N has path_count = K, and there exists a prior tx_id M
        # with path_count = K-1, then the sender of tx_id N appended the
        # last hash.
        seen_path_counts: dict = {}  # path_count -> first tx_id
        # Sort tx_ids chronologically
        sorted_txids = sorted(hops_by_tx.keys())
        for tx_id in sorted_txids:
            tx_hops = hops_by_tx[tx_id]
            if not tx_hops:
                continue
            h0 = tx_hops[0]  # all hops from same tx share the same path
            phashes = h0.get("path_hashes") or []
            pc = h0.get("path_count", 0)
            sender = h0["sender"]
            route = h0.get("route_type", 1)

            # Flood: sender appends to path → new last hash
            if route in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
                if pc > 0 and phashes:
                    # Did the path grow from a previous transmission?
                    if (pc - 1) in seen_path_counts or pc == 1:
                        # The sender appended the last hash
                        relay_hashes[sender].append(phashes[-1])

            seen_path_counts[pc] = tx_id

    return dict(relay_hashes)


def analyze_privacy(trace_data: dict, pni_table_cap: int = 128) -> PrivacyReport:
    """
    Compute privacy metrics from trace JSON data (schema version 3).

    Parameters
    ----------
    trace_data : dict
        Parsed JSON from a trace file (as produced by PacketTracer.to_dict()).
    pni_table_cap : int
        PNI table capacity for stress analysis (default: 128).

    Returns
    -------
    PrivacyReport
        Comprehensive privacy metrics.
    """
    packets = trace_data.get("packets", [])

    # ===== Metric 1: Path Hash Entropy =====
    relay_forwards = _extract_relay_forwards(trace_data)

    relay_privacy_list = []
    for relay_name in sorted(relay_forwards.keys()):
        hashes = relay_forwards[relay_name]
        total = len(hashes)
        from collections import Counter as _Counter
        counts = _Counter(hashes)
        unique = len(counts)
        ratio = unique / total if total > 0 else 0.0
        top_hashes = counts.most_common(10)
        relay_privacy_list.append(RelayPrivacy(
            name=relay_name,
            total_forwards=total,
            unique_hashes=unique,
            entropy_ratio=ratio,
            hashes=top_hashes,
        ))

    if relay_privacy_list:
        avg_entropy = sum(r.entropy_ratio for r in relay_privacy_list) / len(relay_privacy_list)
        min_entropy = min(r.entropy_ratio for r in relay_privacy_list)
    else:
        avg_entropy = 0.0
        min_entropy = 0.0

    # ===== Metric 2: Cross-Path Linkability =====
    # Colluding endpoints compare the relay paths in *different* flood
    # packets.  If the same hash appears at the same position in two
    # unrelated floods, those floods shared a relay — a privacy leak.
    #
    # We use ADVERT floods (every node sends one) rather than TXT_MSG,
    # because the current runner only has one source-dest pair so there
    # are very few TXT_MSG conversations to compare.  Adverts provide
    # N-choose-2 cross-conversation comparisons where N = number of nodes.
    #
    # We take only flood hops, extract the longest path per packet, and
    # compare ONLY across distinct fingerprints.
    fp_to_path: dict = {}  # fingerprint -> longest path_hashes list
    for pkt in packets:
        fp = pkt.get("fingerprint", "")
        # Only consider flood hops (path grows during flooding).
        max_path: list = []
        for h in pkt.get("hops", []):
            rt = h.get("route_type", 0)
            if rt not in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
                continue
            ph = h.get("path_hashes") or []
            if len(ph) > len(max_path):
                max_path = list(ph)
        if max_path:
            fp_to_path[fp] = max_path

    delivered_paths = list(fp_to_path.values())
    linkable_pairs = 0
    total_comparisons = 0
    # Cap comparisons at ~10000 to avoid O(N^2) blow-up on large traces.
    import itertools
    pair_limit = 10000
    pair_iter = itertools.combinations(range(len(delivered_paths)), 2)
    for i, j in itertools.islice(pair_iter, pair_limit):
        p1 = delivered_paths[i]
        p2 = delivered_paths[j]
        # Compare by position: do the hashes at position k match?
        min_len = min(len(p1), len(p2))
        for k in range(min_len):
            total_comparisons += 1
            if p1[k] == p2[k]:
                linkable_pairs += 1

    linkability_rate = linkable_pairs / total_comparisons if total_comparisons > 0 else 0.0

    # ===== Metric 3: Relay Anonymity Set =====
    # For each observed hash value, count how many distinct relay nodes emitted it.
    hash_to_relays: dict = defaultdict(set)  # hash_hex -> {relay_name, ...}
    for relay_name, hashes in relay_forwards.items():
        for h in hashes:
            hash_to_relays[h].add(relay_name)

    if hash_to_relays:
        anon_sizes = [len(relays) for relays in hash_to_relays.values()]
        avg_anon = sum(anon_sizes) / len(anon_sizes)
        min_anon = min(anon_sizes)
    else:
        avg_anon = 0.0
        min_anon = 0

    # ===== Metric 4: Identity Exposure =====
    advert_pubs = set()
    src_hashes = set()
    dest_hashes = set()
    for pkt in packets:
        pub = pkt.get("advert_pub_hex")
        if pub:
            advert_pubs.add(pub)
        src = pkt.get("src_hash_hex")
        if src:
            src_hashes.add(src)
        dest = pkt.get("dest_hash_hex")
        if dest:
            dest_hashes.add(dest)

    # ===== Metric 5: PNI Table Stress =====
    max_forwards = max(
        (len(h) for h in relay_forwards.values()),
        default=0,
    )

    return PrivacyReport(
        relay_privacy=relay_privacy_list,
        avg_entropy_ratio=avg_entropy,
        min_entropy_ratio=min_entropy,
        linkable_pairs=linkable_pairs,
        total_comparisons=total_comparisons,
        linkability_rate=linkability_rate,
        avg_anonymity_set=avg_anon,
        min_anonymity_set=min_anon,
        advert_pub_keys_exposed=len(advert_pubs),
        src_hashes_observed=len(src_hashes),
        dest_hashes_observed=len(dest_hashes),
        max_relay_forwards=max_forwards,
        pni_table_cap=pni_table_cap,
        eviction_risk=max_forwards > pni_table_cap,
    )


def format_privacy_report(report: PrivacyReport, label: str = "") -> str:
    """Format a PrivacyReport as a human-readable string."""
    lines = []
    hdr = f"  Privacy Analysis: {label}" if label else "  Privacy Analysis"
    lines.append("")
    lines.append("=" * 72)
    lines.append(hdr)
    lines.append("=" * 72)

    # --- Path Hash Entropy ---
    lines.append("")
    lines.append("  1. Path Hash Entropy (passive eavesdropper)")
    lines.append(f"     Avg entropy ratio:  {report.avg_entropy_ratio:.3f}  "
                 f"(1.0 = fully unlinkable, near 0 = deterministic)")
    lines.append(f"     Min entropy ratio:  {report.min_entropy_ratio:.3f}  "
                 f"(worst-case relay)")
    lines.append("")
    lines.append(f"     {'Relay':<10} {'Forwards':>8} {'Unique':>8} {'Entropy':>8}")
    lines.append(f"     {'-'*10} {'-'*8} {'-'*8} {'-'*8}")
    for r in report.relay_privacy:
        lines.append(f"     {r.name:<10} {r.total_forwards:>8} "
                     f"{r.unique_hashes:>8} {r.entropy_ratio:>8.3f}")

    # --- Cross-Path Linkability ---
    lines.append("")
    lines.append("  2. Cross-Path Linkability (colluding endpoints)")
    lines.append(f"     Linkable position-hash pairs: {report.linkable_pairs} / "
                 f"{report.total_comparisons}")
    lines.append(f"     Linkability rate: {report.linkability_rate:.4f}  "
                 f"(0.0 = fully private, 1.0 = fully linkable)")

    # --- Relay Anonymity Set ---
    lines.append("")
    lines.append("  3. Relay Anonymity Set (global observer)")
    lines.append(f"     Avg anonymity set size: {report.avg_anonymity_set:.2f}  "
                 f"(higher = better)")
    lines.append(f"     Min anonymity set size: {report.min_anonymity_set}  "
                 f"(1 = fully identified)")

    # --- Identity Exposure ---
    lines.append("")
    lines.append("  4. Identity Exposure (adverts & payload headers)")
    lines.append(f"     Distinct pub_keys in adverts:  {report.advert_pub_keys_exposed}")
    lines.append(f"     Distinct src_hash values:      {report.src_hashes_observed}")
    lines.append(f"     Distinct dest_hash values:     {report.dest_hashes_observed}")

    # --- PNI Table Stress ---
    lines.append("")
    lines.append("  5. PNI Table Stress")
    lines.append(f"     Max forwards through a relay:  {report.max_relay_forwards}")
    lines.append(f"     PNI table capacity:            {report.pni_table_cap}")
    risk_str = "YES — eviction likely!" if report.eviction_risk else "no"
    lines.append(f"     Eviction risk:                 {risk_str}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)
