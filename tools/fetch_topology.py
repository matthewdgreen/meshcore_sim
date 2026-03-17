#!/usr/bin/env python3
"""
tools/fetch_topology.py — Fetch a live MeshCore network map and convert to
simulator topology JSON.

Data source: https://live.bostonme.sh or any self-hosted instance of
https://github.com/yellowcooln/meshcore-mqtt-live-map

Authentication (one of):
  --token TOKEN       PROD_TOKEN bearer token (ask network admin)
  --cookie VALUE      Value of the meshmap_auth cookie only
  --raw-cookie STRING Full Cookie header value copied verbatim from DevTools
                      (e.g. "cf_clearance=abc; meshmap_auth=xyz")

No external dependencies — stdlib only.

Examples
--------
# Check network size without credentials:
python3 tools/fetch_topology.py --stats

# Full Boston relay backbone (edges seen ≥ 5 times, relays only):
python3 tools/fetch_topology.py \\
    --token <TOKEN> \\
    --only-relays --min-edge-count 5 \\
    --output topologies/boston_relays.json --verbose

# All node types, looser edge filter, via browser cookie:
python3 tools/fetch_topology.py \\
    --cookie <meshmap_auth value> \\
    --min-edge-count 2 \\
    --output topologies/boston_full.json --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LOSS        = 0.05   # 5 % — typical outdoor LoRa link
_DEFAULT_LATENCY_MS  = 20.0   # ms — reasonable LoRa air-time
_DEFAULT_SNR         = 6.0    # dB
_DEFAULT_RSSI        = -90.0  # dBm

_ROLE_REPEATER    = "repeater"
_ROLE_COMPANION   = "companion"
_ROLE_ROOM_SERVER = "room_server"


# ---------------------------------------------------------------------------
# Geo helpers
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


def _coord_key(lat: float, lon: float) -> tuple:
    """Stable dict key for a coordinate (5 d.p. ≈ 1 m precision)."""
    return (round(lat, 5), round(lon, 5))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(
    url: str,
    token: Optional[str],
    cookie: Optional[str],
    raw_cookie: Optional[str] = None,
    debug: bool = False,
) -> dict:
    import urllib.parse

    headers: dict[str, str] = {
        # Mirror what the browser sends so the server accepts the request
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Accept": "*/*",
        "Referer": url.rsplit("/", 1)[0] + "/map",
    }
    if token:
        # The live-map server accepts the token as a query param AND x-access-token header
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={urllib.parse.quote(token)}"
        headers["x-access-token"] = token
    if raw_cookie:
        headers["Cookie"] = raw_cookie          # verbatim — user pastes full header
    elif cookie:
        headers["Cookie"] = f"meshmap_auth={cookie}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            pass
        if e.code == 401:
            hint = (
                "\nHint: the site may require multiple cookies (e.g. cf_clearance + meshmap_auth).\n"
                "Try --raw-cookie with the full Cookie header from DevTools:\n"
                "  1. Open DevTools → Network tab, reload the page\n"
                "  2. Find any request to /snapshot or /api/nodes\n"
                "  3. Right-click → Copy → Copy as cURL\n"
                "  4. Extract the -H 'cookie: ...' value and pass it to --raw-cookie"
            )
            if debug and body:
                hint += f"\n\nServer response body:\n{body[:500]}"
            raise SystemExit(f"401 Unauthorized at {url}{hint}")
        raise SystemExit(f"HTTP {e.code} fetching {url}: {e.reason}"
                         + (f"\n{body[:300]}" if debug and body else ""))
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error fetching {url}: {e.reason}")


def fetch_stats(host: str) -> dict:
    """GET /stats — public endpoint, no auth required."""
    return _fetch(f"https://{host}/stats", token=None, cookie=None)


def fetch_snapshot(
    host: str,
    token: Optional[str],
    cookie: Optional[str],
    raw_cookie: Optional[str] = None,
    debug: bool = False,
) -> dict:
    """GET /snapshot — requires bearer token or browser cookie(s)."""
    return _fetch(
        f"https://{host}/snapshot",
        token=token,
        cookie=cookie,
        raw_cookie=raw_cookie,
        debug=debug,
    )


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def build_topology(
    snapshot: dict,
    *,
    min_edge_count: int = 1,
    only_relays: bool = False,
    max_distance_km: float = 50.0,
    default_binary: str = "./node_agent/build/node_agent",
    warmup_secs: float = 15.0,
    duration_secs: float = 120.0,
    traffic_interval_secs: float = 10.0,
    advert_interval_secs: float = 60.0,
    seed: int = 42,
) -> tuple[dict, list[dict], list[dict]]:
    """
    Convert a /snapshot response to simulator topology JSON.

    Returns (topology_dict, nodes_with_meta, edges_with_meta).
    The _meta / _count fields in nodes/edges are stripped from the final JSON
    but returned for caller statistics.
    """
    devices       = snapshot.get("devices") or {}
    history_edges = snapshot.get("history_edges") or []

    # ------------------------------------------------------------------
    # 1. Build node list
    # ------------------------------------------------------------------
    nodes_raw: list[dict] = []       # includes _meta for stats
    coord_exact: dict[tuple, str] = {}  # (lat5, lon5) → device_id
    coord_index: list[tuple[float, float, str]] = []  # for nearest-neighbour

    for device_id, dev in devices.items():
        lat = float(dev.get("lat") or 0.0)
        lon = float(dev.get("lon") or 0.0)

        # Skip nodes without a real GPS fix
        if abs(lat) < 0.001 and abs(lon) < 0.001:
            continue

        role_str = (dev.get("role") or "").lower().replace("-", "_")

        if only_relays and role_str != _ROLE_REPEATER:
            continue

        is_relay       = (role_str == _ROLE_REPEATER)
        is_room_server = (role_str == _ROLE_ROOM_SERVER)

        node: dict = {"name": device_id, "relay": is_relay}
        if is_room_server:
            node["room_server"] = True

        # Store meta for verbose output — stripped before JSON serialisation
        node["_meta"] = {
            "display_name": dev.get("name") or device_id[:12],
            "lat": lat,
            "lon": lon,
            "role": role_str,
            "last_seen_ts": dev.get("last_seen_ts"),
            "rssi": dev.get("rssi"),
            "snr":  dev.get("snr"),
        }

        nodes_raw.append(node)
        coord_exact[_coord_key(lat, lon)] = device_id
        coord_index.append((lat, lon, device_id))

    node_ids: set[str] = {n["name"] for n in nodes_raw}

    # ------------------------------------------------------------------
    # 2. Nearest-neighbour resolver for history-edge endpoints
    #    History edges store lat/lon of the TX node, not a device_id.
    #    We resolve by exact coordinate match first, then nearest within
    #    100 m as a floating-point tolerance fallback.
    # ------------------------------------------------------------------

    def _resolve(lat: float, lon: float) -> Optional[str]:
        cand = coord_exact.get(_coord_key(lat, lon))
        if cand and cand in node_ids:
            return cand
        # Brute-force nearest neighbour — only reached on float-precision mismatch
        best_dist = float("inf")
        best_id: Optional[str] = None
        for dlat, dlon, did in coord_index:
            d = haversine_km(lat, lon, dlat, dlon)
            if d < best_dist:
                best_dist = d
                best_id = did
        return best_id if best_dist < 0.10 else None  # 100 m tolerance

    # Per-device signal quality for edge annotation
    node_rssi: dict[str, float] = {}
    node_snr:  dict[str, float] = {}
    for n in nodes_raw:
        meta = n["_meta"]
        if meta["rssi"] is not None:
            node_rssi[n["name"]] = float(meta["rssi"])
        if meta["snr"] is not None:
            node_snr[n["name"]]  = float(meta["snr"])

    # ------------------------------------------------------------------
    # 3. Build edge list from history edges
    # ------------------------------------------------------------------
    edges_raw: list[dict] = []
    seen_pairs: set[frozenset] = set()

    for hedge in history_edges:
        count = int(hedge.get("count") or 0)
        if count < min_edge_count:
            continue

        a_coord = hedge.get("a") or []
        b_coord = hedge.get("b") or []
        if len(a_coord) < 2 or len(b_coord) < 2:
            continue

        id_a = _resolve(float(a_coord[0]), float(a_coord[1]))
        id_b = _resolve(float(b_coord[0]), float(b_coord[1]))

        if id_a is None or id_b is None or id_a == id_b:
            continue
        if id_a not in node_ids or id_b not in node_ids:
            continue

        pair = frozenset([id_a, id_b])
        if pair in seen_pairs:
            continue  # keep first (highest count — list is unsorted; deduplicate)
        seen_pairs.add(pair)

        # Sanity-check physical distance
        dist = haversine_km(
            float(a_coord[0]), float(a_coord[1]),
            float(b_coord[0]), float(b_coord[1]),
        )
        if dist > max_distance_km:
            continue

        # Signal quality: average of each node's last-known values
        rssi = (node_rssi.get(id_a, _DEFAULT_RSSI)
                + node_rssi.get(id_b, _DEFAULT_RSSI)) / 2.0
        snr  = (node_snr.get(id_a,  _DEFAULT_SNR)
                + node_snr.get(id_b,  _DEFAULT_SNR))  / 2.0

        edges_raw.append({
            "a":          id_a,
            "b":          id_b,
            "loss":       _DEFAULT_LOSS,
            "latency_ms": _DEFAULT_LATENCY_MS,
            "snr":        round(snr, 1),
            "rssi":       round(rssi, 1),
            "_count":     count,    # stripped before output; kept for stats
            "_dist_km":   round(dist, 2),
        })

    # Sort by count descending so the strongest links appear first in JSON
    edges_raw.sort(key=lambda e: e["_count"], reverse=True)

    # ------------------------------------------------------------------
    # 4. Assemble final topology JSON (strip internal _ fields)
    # ------------------------------------------------------------------
    nodes_out = [{k: v for k, v in n.items() if not k.startswith("_")}
                 for n in nodes_raw]
    edges_out = [{k: v for k, v in e.items() if not k.startswith("_")}
                 for e in edges_raw]

    topology = {
        "nodes": nodes_out,
        "edges": edges_out,
        "simulation": {
            "warmup_secs":            warmup_secs,
            "duration_secs":          duration_secs,
            "traffic_interval_secs":  traffic_interval_secs,
            "advert_interval_secs":   advert_interval_secs,
            "default_binary":         default_binary,
            "seed":                   seed,
        },
    }

    return topology, nodes_raw, edges_raw


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_stats(host: str) -> None:
    data     = fetch_stats(host)
    stats    = data.get("stats", {})
    presence = data.get("mqtt_presence", {})
    print(f"  Host:              https://{host}")
    print(f"  Mapped devices:    {data.get('mapped_devices', '?')}")
    print(f"  Seen devices:      {data.get('seen_devices', '?')}")
    print(f"  Active routes:     {data.get('route_count', '?')}")
    print(f"  History edges:     {data.get('history_edge_count', '?')}")
    print(f"  MQTT online:       {presence.get('connected_total', '?')} "
          f"({presence.get('feeding_total', '?')} feeding)")
    last = stats.get("last_parsed_ts")
    if last:
        import datetime
        ts = datetime.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"  Last activity:     {ts}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a live MeshCore network map and write simulator topology JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--host", default="live.bostonme.sh",
        help="Hostname of the meshcore-mqtt-live-map instance "
             "(default: live.bostonme.sh)",
    )

    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--token", metavar="TOKEN",
        help="PROD_TOKEN bearer token for authenticated access",
    )
    auth.add_argument(
        "--cookie", metavar="VALUE",
        help="Value of the meshmap_auth cookie only (name=value pair added automatically)",
    )
    auth.add_argument(
        "--raw-cookie", metavar="STRING",
        help='Full Cookie header value verbatim, e.g. "cf_clearance=abc; meshmap_auth=xyz". '
             "Copy from DevTools → Network → any /snapshot request → Request Headers → cookie.",
    )

    parser.add_argument(
        "--stats", action="store_true",
        help="Print live network statistics (no auth required) and exit",
    )
    parser.add_argument(
        "--output", "-o", metavar="FILE",
        help="Write topology JSON to FILE (default: stdout)",
    )
    parser.add_argument(
        "--min-edge-count", type=int, default=1, metavar="N",
        help="Exclude edges observed fewer than N times (default: 1). "
             "Higher values give a denser, more reliable backbone.",
    )
    parser.add_argument(
        "--only-relays", action="store_true",
        help="Include only repeater nodes; skip companions and room servers",
    )
    parser.add_argument(
        "--max-distance-km", type=float, default=50.0, metavar="KM",
        help="Drop edges longer than KM kilometres (default: 50)",
    )
    parser.add_argument(
        "--warmup-secs", type=float, default=15.0, metavar="S",
        help="simulation.warmup_secs in output JSON (default: 15)",
    )
    parser.add_argument(
        "--duration-secs", type=float, default=120.0, metavar="S",
        help="simulation.duration_secs in output JSON (default: 120)",
    )
    parser.add_argument(
        "--binary", default="./node_agent/build/node_agent",
        dest="default_binary", metavar="PATH",
        help="simulation.default_binary in output JSON",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed in output JSON (default: 42)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print conversion statistics to stderr",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print HTTP response body on errors (useful for diagnosing auth failures)",
    )

    args = parser.parse_args()

    if args.stats:
        _print_stats(args.host)
        return

    if not args.token and not args.cookie and not args.raw_cookie:
        parser.error(
            "provide --token TOKEN, --cookie VALUE, or --raw-cookie STRING.\n"
            "       Use --stats to check network size without credentials."
        )

    if args.verbose:
        print(f"Fetching snapshot from https://{args.host}/snapshot …",
              file=sys.stderr)

    snapshot = fetch_snapshot(
        args.host, args.token, args.cookie,
        raw_cookie=args.raw_cookie,
        debug=args.debug,
    )
    topology, nodes_raw, edges_raw = build_topology(
        snapshot,
        min_edge_count=args.min_edge_count,
        only_relays=args.only_relays,
        max_distance_km=args.max_distance_km,
        default_binary=args.default_binary,
        warmup_secs=args.warmup_secs,
        duration_secs=args.duration_secs,
        seed=args.seed,
    )

    if args.verbose:
        total_devs  = len(snapshot.get("devices") or {})
        total_edges = len(snapshot.get("history_edges") or [])
        n_relay = sum(1 for n in nodes_raw if n.get("relay"))
        n_ep    = sum(1 for n in nodes_raw
                      if not n.get("relay") and not n.get("room_server"))
        n_rs    = sum(1 for n in nodes_raw if n.get("room_server"))
        print(f"  Snapshot: {total_devs} devices, {total_edges} history edges",
              file=sys.stderr)
        print(f"  Output:   {len(nodes_raw)} nodes "
              f"({n_relay} relays, {n_ep} endpoints, {n_rs} room-servers)",
              file=sys.stderr)
        print(f"            {len(edges_raw)} edges "
              f"(min_count={args.min_edge_count}, "
              f"max_dist={args.max_distance_km} km)",
              file=sys.stderr)
        if edges_raw:
            counts = [e["_count"] for e in edges_raw]
            dists  = [e["_dist_km"] for e in edges_raw]
            print(f"            edge count range: {min(counts)}–{max(counts)}",
                  file=sys.stderr)
            print(f"            distance range:   {min(dists)}–{max(dists)} km",
                  file=sys.stderr)

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
