#!/usr/bin/env python3
"""
tools/import_topology.py — Convert meshcore-optimizer topology JSON to
simulator topology JSON.

The meshcore-optimizer tool maps a live MeshCore network by probing nodes
via traces and neighbor tables.  Its output has gaps that make it unsuitable
for direct simulation: incomplete edges, no companion endpoints, stub nodes,
directed-only edges, no loss/latency, no radio/simulation section, and
emoji-laden names.

This tool fills those gaps:
  1. Filters stub nodes (no coordinates, short prefixes)
  2. Sanitises names (strips emoji / non-ASCII, truncates)
  3. Merges directed edges into undirected with directional overrides
  4. Estimates missing edges via statistics-driven gap-fill:
     - Fits propagation model (SNR vs distance) from measured edges
     - Generates inferred edges with log-normal shadow fading
     - Caps inferred edges per node to prevent star explosion
  5. Maps SNR to packet loss probability
  6. Adds companion endpoint nodes
  7. Adds radio + simulation sections

No external dependencies — stdlib only.

Examples
--------
# Basic conversion with 5 companion endpoints (default):
python3 tools/import_topology.py ../meshcore-optimizer/topology.json \\
    --output topologies/gdansk.json --verbose

# Disable gap-filling, custom radio params:
python3 tools/import_topology.py topology.json \\
    --no-fill-gaps --sf 12 --bw-hz 125000 -o output.json -v
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source confidence ranking: higher = more trustworthy
_SOURCE_RANK = {"neighbors": 3, "trace": 2, "inferred": 1}

# SNR-to-loss mapping table (lower bound → loss)
_SNR_LOSS_TABLE = [
    (6.0,   0.02),   # Excellent
    (0.0,   0.05),   # Good
    (-6.0,  0.10),   # Marginal
    (-10.0, 0.20),   # Poor
    (-12.0, 0.35),   # Near limit
]
_SNR_LOSS_FLOOR = 0.50  # SNR < -12 dB

# Log-distance path loss model parameters (suburban outdoor)
# These are fallbacks when < 5 measured edges are available for fitting.
_SNR_REF = 10.0     # dB at reference distance
_D_REF_KM = 1.0     # reference distance in km
_PATH_LOSS_N = 3.0   # path loss exponent
_SNR_FLOOR = -12.0   # LoRa SF10 demodulation floor (dB)
_MAX_GAP_KM = 30.0   # max practical LoRa range (km)

# Shadow fading defaults
_DEFAULT_SIGMA = 8.0     # dB — typical outdoor log-normal shadow fading
_MIN_EDGES_FOR_FIT = 5   # need at least this many measured edges to fit model
_MAX_GOOD_LINKS = 3  # cap SNR > 0 edges per node (measured + inferred); typical 2-4
_MAX_EDGES_PER_NODE = 30  # hard cap on total edges per node
_MAX_RANGE_FACTOR = 1.5  # auto max range = max measured distance * this

# Reverse-direction SNR penalty when only one direction is measured
_REVERSE_SNR_PENALTY = 5.0  # dB

# Companion link parameters (phone sitting next to repeater)
_COMPANION_SNR = 12.0
_COMPANION_RSSI = -60.0
_COMPANION_LOSS = 0.02
_COMPANION_LATENCY_MS = 5.0

# EU Narrow defaults — standard European MeshCore configuration.
# Edit the radio section in the output JSON to match your network.
_DEFAULT_SF = 8
_DEFAULT_BW_HZ = 62_500
_DEFAULT_CR = 4        # CR4/8
_DEFAULT_PREAMBLE = 8

# Companion name sequence
_COMPANION_NAMES = [
    "alice", "bob", "charlie", "dave", "eve", "frank",
    "grace", "heidi", "ivan", "judy", "karl", "linda",
    "mike", "nancy", "oscar", "peggy", "quinn", "ruth",
    "steve", "tina", "ursula", "victor", "wendy", "xavier",
    "yvonne", "zack",
]


# ---------------------------------------------------------------------------
# Geo helpers (duplicated from fetch_topology.py — stdlib only)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres (WGS-84 sphere approximation)."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Name sanitisation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"[^A-Za-z0-9_ -]")


def sanitize_name(raw_name: str, prefix: str) -> str:
    """Strip emoji/non-ASCII, truncate to 20 chars, fall back to prefix."""
    clean = _NAME_RE.sub("", raw_name).strip()
    # Collapse runs of spaces/underscores
    clean = re.sub(r"[\s_]+", "_", clean)
    # Strip leading/trailing underscores or hyphens
    clean = clean.strip("_-")
    if not clean:
        clean = prefix
    return clean[:20]


# ---------------------------------------------------------------------------
# SNR → loss mapping
# ---------------------------------------------------------------------------

def snr_to_loss(snr: float) -> float:
    """Map SNR (dB) to packet loss probability."""
    for threshold, loss in _SNR_LOSS_TABLE:
        if snr >= threshold:
            return loss
    return _SNR_LOSS_FLOOR


# ---------------------------------------------------------------------------
# Gap-fill: propagation model fitting and estimation
# ---------------------------------------------------------------------------

def estimate_snr(distance_km: float) -> float:
    """Log-distance path loss model: SNR_est = SNR_ref - 10*n*log10(d/d_ref)."""
    if distance_km <= 0:
        return _SNR_REF
    return _SNR_REF - 10.0 * _PATH_LOSS_N * math.log10(distance_km / _D_REF_KM)


def fit_propagation_model(
    measured_edges: list[dict],
    nodes: dict[str, dict],
    sigma_override: Optional[float] = None,
    verbose: bool = False,
) -> tuple:
    """Fit SNR = a + b * log10(dist_km) from measured edges.

    Returns (a, b, sigma, max_dist_km, mean_rssi).
    Falls back to hardcoded constants when < 5 measured edges with distance.
    """
    # Collect (log10_dist, snr, rssi) for edges with source=neighbors|trace
    points = []  # (log10_dist, snr)
    rssi_vals = []
    max_dist = 0.0

    # Build prefix→node lookup by name
    name_to_prefix = {n["name"]: p for p, n in nodes.items()}

    for e in measured_edges:
        a_name = e.get("a", "")
        b_name = e.get("b", "")
        a_pfx = name_to_prefix.get(a_name)
        b_pfx = name_to_prefix.get(b_name)
        if a_pfx is None or b_pfx is None:
            continue

        n1, n2 = nodes[a_pfx], nodes[b_pfx]
        dist = haversine_km(n1["lat"], n1["lon"], n2["lat"], n2["lon"])
        if dist <= 0.01:  # skip co-located nodes
            continue

        snr = float(e.get("snr", 0.0))
        points.append((math.log10(dist), snr))
        max_dist = max(max_dist, dist)

        rssi = e.get("rssi")
        if rssi is not None:
            rssi_vals.append(float(rssi))

    mean_rssi = sum(rssi_vals) / len(rssi_vals) if rssi_vals else -90.0

    if len(points) < _MIN_EDGES_FOR_FIT:
        # Not enough data — fall back to hardcoded model
        a = _SNR_REF
        b = -10.0 * _PATH_LOSS_N  # = -30.0
        sigma = _DEFAULT_SIGMA
        if max_dist == 0.0:
            max_dist = _MAX_GAP_KM / _MAX_RANGE_FACTOR
        if verbose:
            print(f"  Propagation model: FALLBACK (only {len(points)} measured "
                  f"edges with distance, need {_MIN_EDGES_FOR_FIT})",
                  file=sys.stderr)
            print(f"    SNR = {a:.1f} + {b:.1f}*log10(d), sigma={sigma:.1f} dB",
                  file=sys.stderr)
        return (a, b, sigma, max_dist, mean_rssi)

    # Linear regression: SNR = a + b * log10(dist)
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xx = sum(p[0] ** 2 for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)

    denom = n * sum_xx - sum_x ** 2
    if abs(denom) < 1e-12:
        # Degenerate case — all edges at same distance
        a = sum_y / n
        b = 0.0
    else:
        b = (n * sum_xy - sum_x * sum_y) / denom
        a = (sum_y - b * sum_x) / n

    # Sigma = stddev of residuals
    residuals = [p[1] - (a + b * p[0]) for p in points]
    variance = sum(r ** 2 for r in residuals) / n
    fitted_sigma = math.sqrt(variance)

    sigma = sigma_override if sigma_override is not None else fitted_sigma
    # Ensure sigma is at least 1 dB (prevents degenerate zero-variation)
    if sigma < 1.0:
        sigma = 1.0

    if verbose:
        print(f"  Propagation model: SNR = {a:.1f} + {b:.1f}*log10(d), "
              f"sigma={sigma:.1f} dB (fitted from {n} edges)",
              file=sys.stderr)
        print(f"    Max measured distance: {max_dist:.1f} km, "
              f"mean RSSI: {mean_rssi:.1f} dBm",
              file=sys.stderr)

    return (a, b, sigma, max_dist, mean_rssi)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def import_topology(
    optimizer_json: dict,
    *,
    companions: int = 5,
    fill_gaps: bool = True,
    max_gap_km: float = _MAX_GAP_KM,
    max_good_links: int = _MAX_GOOD_LINKS,
    gap_sigma: Optional[float] = None,
    sf: int = _DEFAULT_SF,
    bw_hz: int = _DEFAULT_BW_HZ,
    cr: int = _DEFAULT_CR,
    preamble_symbols: int = _DEFAULT_PREAMBLE,
    verbose: bool = False,
) -> dict:
    """Convert meshcore-optimizer JSON to simulator topology JSON."""

    raw_nodes = optimizer_json.get("nodes", {})
    raw_edges = optimizer_json.get("edges", [])

    # ------------------------------------------------------------------
    # 1. Parse & filter nodes
    # ------------------------------------------------------------------
    nodes = {}  # prefix → node dict
    total_raw = len(raw_nodes)
    skipped_coords = 0
    skipped_prefix = 0

    for prefix, ndata in raw_nodes.items():
        # Drop stub nodes: short prefix
        if len(prefix) < 8:
            skipped_prefix += 1
            continue

        lat = float(ndata.get("lat", 0.0))
        lon = float(ndata.get("lon", 0.0))

        # Drop stub nodes: no coordinates
        if lat == 0.0 and lon == 0.0:
            skipped_coords += 1
            continue

        name = sanitize_name(ndata.get("name", prefix), prefix)

        # Ensure unique names
        base_name = name
        counter = 2
        existing_names = {n["name"] for n in nodes.values()}
        while name in existing_names:
            name = f"{base_name[:17]}_{counter}"
            counter += 1

        nodes[prefix] = {
            "name": name,
            "prefix": prefix,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "relay": True,  # all real nodes are infrastructure repeaters
        }

    surviving_prefixes = set(nodes.keys())

    if verbose:
        print(f"  Nodes: {total_raw} raw → {len(nodes)} surviving "
              f"(dropped {skipped_prefix} short-prefix, "
              f"{skipped_coords} no-coords)", file=sys.stderr)

    # ------------------------------------------------------------------
    # 2. Merge directed edges into undirected with directional overrides
    # ------------------------------------------------------------------
    # Group by unordered pair
    pair_edges: dict[frozenset, list[dict]] = {}
    edges_dropped = 0

    for e in raw_edges:
        src = e.get("from", "")
        dst = e.get("to", "")

        # Drop edges where either endpoint was filtered out
        if src not in surviving_prefixes or dst not in surviving_prefixes:
            edges_dropped += 1
            continue

        pair = frozenset([src, dst])
        if pair not in pair_edges:
            pair_edges[pair] = []
        pair_edges[pair].append(e)

    # Build merged undirected edges
    merged_edges = []  # list of edge dicts
    edge_set = set()  # frozenset of (prefix_a, prefix_b) for gap detection

    for pair, directed_list in pair_edges.items():
        endpoints = sorted(pair)  # stable ordering
        a_prefix, b_prefix = endpoints[0], endpoints[1]

        # Separate A→B and B→A edges, picking best by source confidence
        a_to_b_edges = [e for e in directed_list
                        if e["from"] == a_prefix and e["to"] == b_prefix]
        b_to_a_edges = [e for e in directed_list
                        if e["from"] == b_prefix and e["to"] == a_prefix]

        def best_edge(candidates: list[dict]) -> Optional[dict]:
            if not candidates:
                return None
            return max(candidates,
                       key=lambda e: _SOURCE_RANK.get(e.get("source", ""), 0))

        best_ab = best_edge(a_to_b_edges)
        best_ba = best_edge(b_to_a_edges)

        if best_ab is None and best_ba is None:
            continue

        # Extract SNR values
        # A→B: SNR measured at B (what B sees when A transmits)
        # B→A: SNR measured at A (what A sees when B transmits)
        if best_ab is not None and best_ba is not None:
            snr_a_to_b = float(best_ab.get("snr_db", 0.0))
            snr_b_to_a = float(best_ba.get("snr_db", 0.0))
        elif best_ab is not None:
            snr_a_to_b = float(best_ab.get("snr_db", 0.0))
            snr_b_to_a = snr_a_to_b - _REVERSE_SNR_PENALTY
        else:
            snr_b_to_a = float(best_ba.get("snr_db", 0.0))
            snr_a_to_b = snr_b_to_a - _REVERSE_SNR_PENALTY

        loss_ab = snr_to_loss(snr_a_to_b)
        loss_ba = snr_to_loss(snr_b_to_a)

        edge = {
            "a": nodes[a_prefix]["name"],
            "b": nodes[b_prefix]["name"],
            "loss": loss_ab,
            "latency_ms": 20.0,
            "snr": round(snr_a_to_b, 2),
            "rssi": -90.0,
        }

        # Add directional overrides if the two directions differ
        if abs(snr_a_to_b - snr_b_to_a) > 0.01 or abs(loss_ab - loss_ba) > 0.001:
            edge["a_to_b"] = {
                "snr": round(snr_a_to_b, 2),
                "loss": loss_ab,
            }
            edge["b_to_a"] = {
                "snr": round(snr_b_to_a, 2),
                "loss": loss_ba,
            }

        merged_edges.append(edge)
        edge_set.add(frozenset([a_prefix, b_prefix]))

    if verbose:
        print(f"  Edges: {len(raw_edges)} directed → {len(merged_edges)} "
              f"undirected (dropped {edges_dropped} with filtered endpoints)",
              file=sys.stderr)

    # ------------------------------------------------------------------
    # 3. Estimate missing edges (smart gap-filling)
    # ------------------------------------------------------------------
    gap_filled = 0

    if fill_gaps:
        # Fit propagation model from measured edges
        model_a, model_b, sigma, max_measured_dist, mean_rssi = \
            fit_propagation_model(
                merged_edges, nodes,
                sigma_override=gap_sigma,
                verbose=verbose,
            )

        # Auto-derive max range from measured data (or use explicit CLI value)
        auto_max_km = max_measured_dist * _MAX_RANGE_FACTOR
        effective_max_km = min(max_gap_km, auto_max_km) if auto_max_km > 0 else max_gap_km

        # Seeded RNG for reproducible shadow fading
        rng = random.Random(42)

        # Count edges per node from measured data.
        # good_count: SNR > 0 edges (capped at max_good_links; typical 2-4)
        # total_count: all edges (hard cap at _MAX_EDGES_PER_NODE)
        good_count = {}   # prefix → count of SNR > 0 edges
        total_count = {}  # prefix → count of all edges
        name_to_prefix = {n["name"]: p for p, n in nodes.items()}
        for e in merged_edges:
            for name_key in ("a", "b"):
                pfx = name_to_prefix.get(e.get(name_key))
                if pfx:
                    total_count[pfx] = total_count.get(pfx, 0) + 1
                    if float(e.get("snr", 0.0)) > 0.0:
                        good_count[pfx] = good_count.get(pfx, 0) + 1

        prefix_list = sorted(nodes.keys())

        # For each node, find closest unmeasured candidates
        for p1 in prefix_list:
            n1 = nodes[p1]

            # Compute distances to all other nodes, sorted closest-first
            candidates = []
            for p2 in prefix_list:
                if p2 <= p1:  # avoid duplicates and self-edges
                    continue
                pair = frozenset([p1, p2])
                if pair in edge_set:
                    continue
                n2 = nodes[p2]
                dist = haversine_km(n1["lat"], n1["lon"],
                                    n2["lat"], n2["lon"])
                if dist > effective_max_km or dist <= 0.01:
                    continue
                candidates.append((dist, p2))

            candidates.sort()  # closest first

            for dist, p2 in candidates:
                # Hard cap on total edges per node
                t1 = total_count.get(p1, 0)
                t2 = total_count.get(p2, 0)
                if t1 >= _MAX_EDGES_PER_NODE or t2 >= _MAX_EDGES_PER_NODE:
                    continue

                # Estimate SNR with shadow fading
                snr_est = model_a + model_b * math.log10(dist) \
                    + rng.gauss(0.0, sigma)

                if snr_est <= _SNR_FLOOR:
                    continue

                # If this would be a "good" link (SNR > 0), check the cap
                if snr_est > 0.0:
                    g1 = good_count.get(p1, 0)
                    g2 = good_count.get(p2, 0)
                    if g1 >= max_good_links or g2 >= max_good_links:
                        continue

                loss = snr_to_loss(snr_est)

                # RSSI: use mean measured RSSI with proportional noise
                rssi_est = mean_rssi + rng.gauss(0.0, sigma / 2.0)

                merged_edges.append({
                    "a": nodes[p1]["name"],
                    "b": nodes[p2]["name"],
                    "loss": loss,
                    "latency_ms": 20.0,
                    "snr": round(snr_est, 2),
                    "rssi": round(rssi_est, 1),
                })
                edge_set.add(frozenset([p1, p2]))
                total_count[p1] = t1 + 1
                total_count[p2] = t2 + 1
                if snr_est > 0.0:
                    good_count[p1] = good_count.get(p1, 0) + 1
                    good_count[p2] = good_count.get(p2, 0) + 1
                gap_filled += 1

        if verbose:
            print(f"  Gap-fill: {gap_filled} estimated edges added "
                  f"(max {max_good_links} good links/node, "
                  f"max {effective_max_km:.1f} km, "
                  f"SNR floor {_SNR_FLOOR} dB)",
                  file=sys.stderr)

    # ------------------------------------------------------------------
    # 4. Build output node list (relays only, before companions)
    # ------------------------------------------------------------------
    nodes_out = []
    for prefix in sorted(nodes.keys()):
        n = nodes[prefix]
        nodes_out.append({
            "name": n["name"],
            "relay": True,
            "lat": n["lat"],
            "lon": n["lon"],
        })

    # ------------------------------------------------------------------
    # 5. Add companion endpoints
    # ------------------------------------------------------------------
    if companions > 0:
        # Random relay selection (seeded for reproducibility)
        if len(nodes_out) > 0:
            rng = random.Random(42)
            relay_list = [(n["name"], n["lat"], n["lon"]) for n in nodes_out]
            n_companions = min(companions, len(relay_list), len(_COMPANION_NAMES))
            selected = rng.sample(relay_list, n_companions)

            # Create companion nodes and edges
            for i, (relay_name, lat, lon) in enumerate(selected):
                comp_name = _COMPANION_NAMES[i]
                nodes_out.append({
                    "name": comp_name,
                    "relay": False,
                    "lat": lat,
                    "lon": lon,
                })
                merged_edges.append({
                    "a": comp_name,
                    "b": relay_name,
                    "snr": _COMPANION_SNR,
                    "rssi": _COMPANION_RSSI,
                    "loss": _COMPANION_LOSS,
                    "latency_ms": _COMPANION_LATENCY_MS,
                })

            if verbose:
                print(f"  Companions: {len(selected)} added "
                      f"({', '.join(_COMPANION_NAMES[i] for i in range(len(selected)))})",
                      file=sys.stderr)
                for i, (relay_name, _, _) in enumerate(selected):
                    print(f"    {_COMPANION_NAMES[i]} → {relay_name}",
                          file=sys.stderr)

    # ------------------------------------------------------------------
    # 6. Compute warmup and assemble topology
    # ------------------------------------------------------------------
    node_count = len(nodes_out)
    # Auto-compute warmup: stagger + propagation margin
    # Use the same formula as airtime.py: node_count * airtime * margin
    t_sym_ms = (2 ** sf) / (bw_hz / 1000.0)
    de = 1 if t_sym_ms >= 16.0 else 0
    numerator = 8 * 40 - 4 * sf + 28 + 16 - 0  # 40-byte advert, CRC, explicit header
    denominator = 4 * (sf - 2 * de)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)
    t_preamble_ms = (8 + 4.25) * t_sym_ms
    airtime_ms = t_preamble_ms + payload_symbols * t_sym_ms
    warmup_secs = max(10.0, node_count * airtime_ms / 1000.0 * 2.0)

    topology = {
        "radio": {
            "sf": sf,
            "bw_hz": bw_hz,
            "cr": cr,
            "preamble_symbols": preamble_symbols,
        },
        "nodes": nodes_out,
        "edges": merged_edges,
        "simulation": {
            "warmup_secs": round(warmup_secs, 1),
            "duration_secs": 300.0,
            "traffic_interval_secs": 15.0,
            "advert_interval_secs": 60.0,
            "default_binary": "./node_agent/build/node_agent",
            "seed": 42,
        },
    }

    if verbose:
        print(f"  Output: {len(nodes_out)} nodes, {len(merged_edges)} edges",
              file=sys.stderr)
        print(f"  Radio:  SF{sf} / BW{bw_hz // 1000} kHz / CR4/{cr + 4}",
              file=sys.stderr)
        print(f"  Warmup: {warmup_secs:.1f}s (auto-computed from {node_count} nodes)",
              file=sys.stderr)

    return topology


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert meshcore-optimizer topology JSON to simulator format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input", metavar="INPUT",
        help="Path to meshcore-optimizer topology.json",
    )
    parser.add_argument(
        "-o", "--output", metavar="FILE",
        help="Output path (default: stdout)",
    )
    parser.add_argument(
        "--companions", type=int, default=5, metavar="N",
        help="Number of companion endpoints to add (default: 5)",
    )
    parser.add_argument(
        "--no-fill-gaps", action="store_true",
        help="Don't add estimated edges for unobserved node pairs",
    )
    parser.add_argument(
        "--max-gap-km", type=float, default=_MAX_GAP_KM, metavar="KM",
        help=f"Max distance for gap-fill edges (default: {_MAX_GAP_KM})",
    )
    parser.add_argument(
        "--max-good-links", type=int,
        default=_MAX_GOOD_LINKS, metavar="N",
        help=f"Max SNR>0 edges per node, measured+inferred (default: {_MAX_GOOD_LINKS})",
    )
    parser.add_argument(
        "--gap-sigma", type=float, default=None, metavar="DB",
        help="Override fitted shadow fading sigma (dB); "
             "by default, sigma is fitted from measured edges",
    )

    radio = parser.add_argument_group(
        "radio parameters",
        "LoRa radio configuration written into the output JSON.",
    )
    radio.add_argument(
        "--sf", type=int, default=_DEFAULT_SF, metavar="N",
        help=f"Spreading factor 7-12 (default: {_DEFAULT_SF})",
    )
    radio.add_argument(
        "--bw-hz", type=int, default=_DEFAULT_BW_HZ, metavar="HZ",
        dest="bw_hz",
        help=f"Bandwidth in Hz (default: {_DEFAULT_BW_HZ})",
    )
    radio.add_argument(
        "--cr", type=int, default=_DEFAULT_CR, metavar="N",
        help=f"Coding-rate offset: 1=CR4/5, 2=CR4/6, 3=CR4/7, 4=CR4/8 (default: {_DEFAULT_CR})",
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print conversion statistics to stderr",
    )

    args = parser.parse_args()

    with open(args.input) as f:
        optimizer_json = json.load(f)

    topology = import_topology(
        optimizer_json,
        companions=args.companions,
        fill_gaps=not args.no_fill_gaps,
        max_gap_km=args.max_gap_km,
        max_good_links=args.max_good_links,
        gap_sigma=args.gap_sigma,
        sf=args.sf,
        bw_hz=args.bw_hz,
        cr=args.cr,
        verbose=args.verbose,
    )

    out = json.dumps(topology, indent=2)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(out)
            fh.write("\n")
        if args.verbose:
            print(f"  Written to {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
