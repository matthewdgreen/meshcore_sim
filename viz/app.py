"""
app.py — Dash application factory for MeshCore topology visualisation.

Phase 1: static topology viewer.
  - Geo-aware layout when every node carries a non-zero lat/lon: renders on an
    OpenStreetMap tile layer (no API key required).
  - Force-directed layout (dash-cytoscape "cose") for synthetic topologies
    that have no geographic coordinates.

Phase 2: packet trace overlay (pass trace_path to create_app).
  - Nodes coloured by witness count (how many packets each node received).
  - Packet slider to step through every recorded packet.
  - Active senders highlighted in orange, receivers in green.
  - Trace summary stats (packet count, flood %, avg witness count) in sidebar.

Usage:
    from viz.app import create_app
    app = create_app(
        pathlib.Path("topologies/grid_10x10.json"),
        trace_path=pathlib.Path("trace.json"),   # optional
    )
    app.run(port=8050)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import dash
import dash_cytoscape as cyto
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

# Register the extended cytoscape layout algorithms (cose-bilkent, etc.)
cyto.load_extra_layouts()

# ── Colour palette ────────────────────────────────────────────────────────────

_ROLE_COLOUR: dict[str, str] = {
    "relay":       "#3a86ff",   # blue
    "room_server": "#ffbe0b",   # amber
    "endpoint":    "#8d99ae",   # grey
}
_EDGE_COLOUR     = "#adb5bd"
_EDGE_COLOUR_GEO = "rgba(173,181,189,0.6)"
_SENDER_COLOUR   = "#f77f00"   # orange — active packet senders
_RECEIVER_COLOUR = "#2dc653"   # green  — active packet receivers (current step)
_RECEIVED_COLOUR = "#74b3ce"   # soft blue  — received this packet (other steps)

# ── Helpers ───────────────────────────────────────────────────────────────────

_LABEL_LEN = 8   # characters shown on map/graph; full ID visible on hover


def _short(name: str) -> str:
    """First _LABEL_LEN chars of name, with ellipsis if truncated."""
    return name[:_LABEL_LEN] + "…" if len(name) > _LABEL_LEN else name


def _load(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _node_role(n: dict) -> str:
    if n.get("room_server"):
        return "room_server"
    if n.get("relay"):
        return "relay"
    return "endpoint"


def _has_geo(nodes: list[dict]) -> bool:
    """True when every node has a non-zero lat/lon pair."""
    if not nodes:
        return False
    for n in nodes:
        lat = n.get("lat")
        lon = n.get("lon")
        if lat is None or lon is None:
            return False
        if float(lat) == 0.0 and float(lon) == 0.0:
            return False
    return True


# ── Witness-count helpers (Phase 2) ──────────────────────────────────────────

def _witness_counts(trace: dict) -> dict[str, int]:
    """Map node_name → number of distinct packets that node received."""
    counts: dict[str, int] = {}
    for pkt in trace.get("packets", []):
        for node in pkt.get("unique_receivers", []):
            counts[node] = counts.get(node, 0) + 1
    return counts


def _witness_colour(count: int, max_count: int) -> str:
    """Linear interpolation: #e9ecef (0 witnesses) → #d62828 (max_count)."""
    if max_count == 0 or count == 0:
        return "#e9ecef"
    t = min(1.0, count / max_count)
    r = int(233 + t * (214 - 233))
    g = int(236 + t * ( 40 - 236))
    b = int(239 + t * ( 40 - 239))
    return f"#{r:02x}{g:02x}{b:02x}"


# ── Geo-map figure (Plotly scattermapbox) ─────────────────────────────────────

def _geo_figure(
    nodes: list[dict],
    edges: list[dict],
    witness_counts: Optional[dict[str, int]] = None,
    max_count: int = 0,
    packet_witnesses: Optional[set] = None,
    highlight_senders: Optional[list[str]] = None,
    highlight_receivers: Optional[list[str]] = None,
) -> go.Figure:
    node_by_name = {n["name"]: n for n in nodes}

    # --- edge lines ---
    edge_lats: list[Any] = []
    edge_lons: list[Any] = []
    for e in edges:
        na = node_by_name.get(e["a"])
        nb = node_by_name.get(e["b"])
        if na is None or nb is None:
            continue
        edge_lats += [float(na["lat"]), float(nb["lat"]), None]
        edge_lons += [float(na["lon"]), float(nb["lon"]), None]

    edge_trace = go.Scattermapbox(
        lat=edge_lats,
        lon=edge_lons,
        mode="lines",
        line=dict(width=1, color=_EDGE_COLOUR_GEO),
        hoverinfo="none",
        showlegend=False,
    )

    # --- node traces ---
    if witness_counts is not None:
        # Single trace with colour array — shows witness-count heatmap
        lats   = [float(n["lat"]) for n in nodes]
        lons   = [float(n["lon"]) for n in nodes]
        colors = [witness_counts.get(n["name"], 0) for n in nodes]
        texts  = [
            f"<b>{_short(n['name'])}</b><br>"
            f"{n['name']}<br>"
            f"role: {_node_role(n)}<br>"
            f"witnesses: {witness_counts.get(n['name'], 0)}<br>"
            f"lat: {n['lat']:.5f}  lon: {n['lon']:.5f}"
            for n in nodes
        ]
        node_traces: list[Any] = [go.Scattermapbox(
            lat=lats,
            lon=lons,
            mode="markers",
            marker=dict(
                size=8,
                color=colors,
                colorscale="Reds",
                cmin=0,
                cmax=max(max_count, 1),
                colorbar=dict(
                    title=dict(text="Witnesses", side="right"),
                    thickness=12,
                    len=0.5,
                    x=1.0,
                ),
            ),
            text=texts,
            hoverinfo="text",
            showlegend=False,
        )]
    elif packet_witnesses is not None:
        # Per-packet mode: soft blue = received this packet, grey = did not
        lats   = [float(n["lat"]) for n in nodes]
        lons   = [float(n["lon"]) for n in nodes]
        colors = [
            _RECEIVED_COLOUR if n["name"] in packet_witnesses else "#e9ecef"
            for n in nodes
        ]
        texts  = [
            f"<b>{_short(n['name'])}</b><br>"
            f"{n['name']}<br>"
            f"role: {_node_role(n)}<br>"
            f"received: {'yes' if n['name'] in packet_witnesses else 'no'}<br>"
            f"lat: {n['lat']:.5f}  lon: {n['lon']:.5f}"
            for n in nodes
        ]
        node_traces = [go.Scattermapbox(
            lat=lats,
            lon=lons,
            mode="markers",
            marker=dict(size=8, color=colors),
            text=texts,
            hoverinfo="text",
            showlegend=False,
        )]
    else:
        # Role-coloured traces (Phase 1 / no trace loaded)
        role_buckets: dict[str, dict[str, list]] = {}
        for n in nodes:
            role = _node_role(n)
            if role not in role_buckets:
                role_buckets[role] = {"lats": [], "lons": [], "texts": []}
            b = role_buckets[role]
            b["lats"].append(float(n["lat"]))
            b["lons"].append(float(n["lon"]))
            b["texts"].append(
                f"<b>{_short(n['name'])}</b><br>"
                f"{n['name']}<br>"
                f"role: {role}<br>"
                f"lat: {n['lat']:.5f}  lon: {n['lon']:.5f}"
            )
        node_traces = [
            go.Scattermapbox(
                lat=b["lats"],
                lon=b["lons"],
                mode="markers",
                marker=dict(size=8, color=_ROLE_COLOUR.get(role, "#8d99ae")),
                text=b["texts"],
                hoverinfo="text",
                name=role,
            )
            for role, b in role_buckets.items()
        ]

    # --- packet highlight overlays (always emitted so trace count stays constant) ---
    # Constant trace count is important: with uirevision, Plotly matches traces by
    # position, so the viewport (zoom/pan) is preserved across slider updates.
    highlight_traces: list[Any] = []
    for names, colour, label in [
        (highlight_senders   or [], _SENDER_COLOUR,   "senders"),
        (highlight_receivers or [], _RECEIVER_COLOUR, "receivers"),
    ]:
        hl_lats = [float(node_by_name[s]["lat"]) for s in names if s in node_by_name]
        hl_lons = [float(node_by_name[s]["lon"]) for s in names if s in node_by_name]
        highlight_traces.append(go.Scattermapbox(
            lat=hl_lats,
            lon=hl_lons,
            mode="markers",
            marker=dict(size=20, color=colour, opacity=0.85),
            hoverinfo="none",
            showlegend=False,
            name=label,
        ))

    centre_lat = sum(float(n["lat"]) for n in nodes) / len(nodes)
    centre_lon = sum(float(n["lon"]) for n in nodes) / len(nodes)

    fig = go.Figure(data=[edge_trace] + node_traces + highlight_traces)
    fig.update_layout(
        mapbox=dict(
            style="open-street-map",
            center=dict(lat=centre_lat, lon=centre_lon),
            zoom=10,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ccc",
            borderwidth=1,
            x=0.01,
            y=0.99,
        ),
        uirevision="geo",
    )
    return fig


# ── Force-directed graph (dash-cytoscape) ─────────────────────────────────────

def _cyto_elements(
    nodes: list[dict],
    edges: list[dict],
    witness_counts: Optional[dict[str, int]] = None,
    max_count: int = 0,
) -> list[dict]:
    elements: list[dict] = []
    for n in nodes:
        role = _node_role(n)
        if witness_counts is not None:
            colour = _witness_colour(witness_counts.get(n["name"], 0), max_count)
        else:
            colour = _ROLE_COLOUR.get(role, "#8d99ae")
        elements.append({
            "data": {
                "id":      n["name"],
                "label":   _short(n["name"]),
                "role":    role,
                "colour":  colour,
                "witness": witness_counts.get(n["name"], 0) if witness_counts else 0,
            }
        })

    # Deduplicate undirected edges
    seen: set[frozenset] = set()
    for e in edges:
        key: frozenset = frozenset([e["a"], e["b"]])
        if key in seen:
            continue
        seen.add(key)
        elements.append({
            "data": {
                "source":     e["a"],
                "target":     e["b"],
                "loss":       e.get("loss", 0.0),
                "latency_ms": e.get("latency_ms", 0.0),
                "snr":        e.get("snr", 6.0),
                "rssi":       e.get("rssi", -90.0),
            }
        })
    return elements


_CYTO_STYLESHEET = [
    {
        "selector": "node",
        "style": {
            "label":            "data(label)",
            "background-color": "data(colour)",
            "font-size":        "9px",
            "color":            "#333",
            "text-valign":      "bottom",
            "text-margin-y":    "4px",
            "width":            "20px",
            "height":           "20px",
        },
    },
    {
        "selector": "edge",
        "style": {
            "line-color":  _EDGE_COLOUR,
            "width":       1,
            "curve-style": "bezier",
        },
    },
    {
        "selector": "node:selected",
        "style": {"border-width": "3px", "border-color": "#e63946"},
    },
    {
        "selector": "edge:selected",
        "style": {"line-color": "#e63946", "width": 2},
    },
]


# ── Packet info helper (Phase 2) ──────────────────────────────────────────────

def _packet_info_children(pkt: dict, idx: int, total: int) -> list:
    route = "FLOOD" if pkt["is_flood"] else "DIRECT"
    return [
        html.Div(
            f"Packet {idx + 1} / {total}",
            style={"fontWeight": "600", "marginBottom": "4px"},
        ),
        html.Div(f"Type:       {pkt['payload_type_name']}"),
        html.Div(f"Route:      {route}"),
        html.Div(f"Witnesses:  {pkt['witness_count']}"),
        html.Div(
            f"Sender:     {_short(pkt['first_sender'])}",
            title=pkt["first_sender"],
        ),
    ]


# ── Hop / broadcast-step helpers (Phase 2) ───────────────────────────────────

_ROUTE_TYPE: dict[int, str] = {0: "FLOOD", 1: "DIRECT", 3: "TRANSPORT_FLOOD"}


def _route_name(rt: int) -> str:
    return _ROUTE_TYPE.get(rt, f"route{rt}")


def _broadcast_steps(pkt: dict) -> list[list[dict]]:
    """
    Group a packet's hops into broadcast steps ordered by tx_id.

    All hops that share a tx_id came from the same on-air transmission and
    are returned as one group.  Hops without a tx_id (old traces) each form
    their own singleton group so the slider still works.
    """
    seen: dict = {}
    for h in pkt.get("hops", []):
        key = h.get("tx_id")
        if key is None:
            key = id(h)   # fallback: treat as unique broadcast
        if key not in seen:
            seen[key] = []
        seen[key].append(h)
    return list(seen.values())


def _step_info_children(
    pkt: dict, step_idx: int, steps: list[list[dict]]
) -> list:
    """Sidebar content for the selected broadcast step (or summary when -1)."""
    n_steps = len(steps)
    if step_idx < 0 or step_idx >= n_steps:
        return [
            html.Div(
                f"All {n_steps} broadcast step(s) shown",
                style={"color": "#6c757d", "fontSize": "11px"},
            )
        ]
    step = steps[step_idx]
    sender    = step[0]["sender"]
    receivers = [h["receiver"] for h in step]
    dt        = step[0]["t"] - pkt["first_seen_at"]
    route     = _route_name(step[0]["route_type"])
    rx_short  = ", ".join(_short(r) for r in receivers)
    return [
        html.Div(
            f"Broadcast {step_idx + 1} / {n_steps}",
            style={"fontWeight": "600", "marginBottom": "2px"},
        ),
        html.Div(
            f"{_short(sender)} → {len(receivers)} node(s)",
            title=f"{sender} → {', '.join(receivers)}",
        ),
        html.Div(
            rx_short,
            style={"fontSize": "11px", "color": "#6c757d"},
            title=", ".join(receivers),
        ),
        html.Div(f"Route: {route}"),
        html.Div(f"t+{dt:.3f}s  paths: {step[0]['path_count']}"),
    ]


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _sidebar(
    topology_path: Path,
    nodes: list[dict],
    edges: list[dict],
    geo: bool,
    trace: Optional[dict] = None,
    w_counts: Optional[dict[str, int]] = None,
    trace_warning: Optional[str] = None,
    all_steps: Optional[list] = None,
) -> html.Div:
    role_counts: dict[str, int] = {}
    for n in nodes:
        r = _node_role(n)
        role_counts[r] = role_counts.get(r, 0) + 1

    stats = [
        html.P(f"Nodes: {len(nodes)}", style={"margin": "2px 0", "fontSize": "13px"}),
        html.P(f"Edges: {len(edges)}", style={"margin": "2px 0", "fontSize": "13px"}),
        html.P(
            "Layout: geo map" if geo else "Layout: force-directed",
            style={"margin": "2px 0", "fontSize": "12px", "color": "#6c757d"},
        ),
    ]

    # Role legend — only shown when no trace is loaded (heatmap replaces it)
    role_section: list = []
    if trace is None:
        role_section = [
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            *[
                html.Div(
                    [
                        html.Span(style={
                            "display": "inline-block",
                            "width": "11px", "height": "11px",
                            "borderRadius": "50%",
                            "background": colour,
                            "marginRight": "6px",
                            "verticalAlign": "middle",
                        }),
                        html.Span(
                            f"{role}  ({role_counts.get(role, 0)})",
                            style={"fontSize": "13px"},
                        ),
                    ],
                    style={"marginBottom": "5px"},
                )
                for role, colour in _ROLE_COLOUR.items()
                if role_counts.get(role, 0) > 0
            ],
        ]

    # Trace section — witness heatmap scale + stats + packet slider
    trace_section: list = []
    if trace is not None:
        packets = trace.get("packets", [])
        n_pkts  = len(packets)
        n_flood = sum(1 for p in packets if p["is_flood"])
        flood_pct = 100 * n_flood / n_pkts if n_pkts else 0.0
        mean_w    = (
            sum(p["witness_count"] for p in packets) / n_pkts
            if n_pkts else 0.0
        )

        # Colour-scale legend bar
        scale_bar = html.Div(
            [
                html.Span(
                    style={
                        "display": "inline-block",
                        "width": "16px", "height": "10px",
                        "background": _witness_colour(i, 4),
                        "borderRadius": "2px",
                        "marginRight": "2px",
                        "verticalAlign": "middle",
                    }
                )
                for i in range(5)
            ] + [
                html.Span(
                    "witnesses →",
                    style={"fontSize": "11px", "color": "#6c757d"},
                )
            ],
            style={"marginBottom": "6px"},
        )

        if n_pkts > 0:
            play_row: Any = html.Div(
                [
                    html.Div(
                        [
                            html.Button(
                                "▶",
                                id="play-btn",
                                n_clicks=0,
                                title="Play / Pause",
                                style={
                                    "padding": "2px 10px",
                                    "fontSize": "14px",
                                    "cursor": "pointer",
                                    "border": "1px solid #ced4da",
                                    "borderRadius": "4px",
                                    "background": "#fff",
                                    "lineHeight": "1.6",
                                },
                            ),
                            dcc.Dropdown(
                                id="play-speed",
                                options=[
                                    {"label": "0.5×", "value": 1000},
                                    {"label": "1×",   "value": 500},
                                    {"label": "2×",   "value": 250},
                                    {"label": "5×",   "value": 100},
                                ],
                                value=500,
                                clearable=False,
                                style={"flex": "1", "fontSize": "12px", "minWidth": "0"},
                            ),
                        ],
                        style={
                            "display": "flex",
                            "gap": "6px",
                            "alignItems": "center",
                            "marginBottom": "4px",
                        },
                    ),
                    dcc.Checklist(
                        id="hop-play-mode",
                        options=[{"label": " animate hops", "value": "hop"}],
                        value=[],
                        style={"fontSize": "12px", "color": "#495057"},
                    ),
                ],
                style={"marginBottom": "6px"},
            )
            slider: Any = dcc.Slider(
                id="packet-slider",
                min=0,
                max=n_pkts - 1,
                step=1,
                value=0,
                marks=None,
                tooltip={"placement": "bottom", "always_visible": False},
            )
            initial_hop_max = max(
                (len(s) - 1 for s in (all_steps or [])), default=0
            )
            hop_slider: Any = dcc.Slider(
                id="hop-slider",
                min=-1,
                max=max(initial_hop_max, 0),
                step=1,
                value=-1,
                marks=None,
                tooltip={"placement": "bottom", "always_visible": False},
            )
        else:
            play_row = html.Span()   # empty placeholder
            slider = html.P(
                "No packets in trace.",
                style={"fontSize": "12px", "color": "#6c757d"},
            )
            hop_slider = html.Span()

        trace_section = [
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            *(
                [html.Div(
                    trace_warning,
                    style={
                        "fontSize": "11px",
                        "color": "#842029",
                        "background": "#f8d7da",
                        "border": "1px solid #f5c2c7",
                        "borderRadius": "4px",
                        "padding": "6px 8px",
                        "marginBottom": "8px",
                        "lineHeight": "1.4",
                    },
                )]
                if trace_warning else []
            ),
            html.Div(
                "Witness heatmap",
                style={"fontSize": "11px", "color": "#6c757d", "marginBottom": "4px"},
            ),
            scale_bar,
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.P(f"Packets: {n_pkts}", style={"margin": "2px 0", "fontSize": "13px"}),
            html.P(f"Flood:   {flood_pct:.0f}%", style={"margin": "2px 0", "fontSize": "13px"}),
            html.P(f"Avg witnesses: {mean_w:.1f}", style={"margin": "2px 0", "fontSize": "13px"}),
            *(
                [html.P(
                    f"Node exposure: {min(w_counts.values())}–{max(w_counts.values())} pkts",
                    style={"margin": "2px 0", "fontSize": "12px", "color": "#6c757d"},
                )]
                if w_counts else []
            ),
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.Div(
                "Step through packets:",
                style={"fontSize": "12px", "color": "#6c757d", "marginBottom": "4px"},
            ),
            html.Div(
                [
                    html.Span("■ sender  ",  style={"color": _SENDER_COLOUR,   "fontSize": "11px"}),
                    html.Span("■ receiver ", style={"color": _RECEIVER_COLOUR, "fontSize": "11px"}),
                    html.Span("■ witnessed", style={"color": _RECEIVED_COLOUR, "fontSize": "11px"}),
                ],
                style={"marginBottom": "6px"},
            ),
            dcc.RadioItems(
                id="view-mode",
                options=[
                    {"label": " Global exposure heatmap", "value": "global"},
                    {"label": " Per-packet witness map",  "value": "packet"},
                ],
                value="global",
                style={"fontSize": "12px", "marginBottom": "6px"},
                labelStyle={"display": "block", "marginBottom": "2px"},
            ),
            play_row,
            slider,
            html.Div(
                id="packet-info",
                style={
                    "fontSize": "12px",
                    "color": "#495057",
                    "marginTop": "8px",
                    "lineHeight": "1.7",
                },
            ),
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.Div(
                "Step through broadcast events:",
                style={"fontSize": "12px", "color": "#6c757d", "marginBottom": "4px"},
            ),
            hop_slider,
            html.Div(
                id="hop-info",
                style={
                    "fontSize": "12px",
                    "color": "#495057",
                    "marginTop": "6px",
                    "lineHeight": "1.7",
                },
            ),
        ]

    return html.Div(
        [
            html.H3(
                topology_path.stem,
                style={
                    "fontSize": "14px",
                    "fontWeight": "600",
                    "marginBottom": "10px",
                    "wordBreak": "break-all",
                    "color": "#212529",
                },
            ),
            *stats,
            *role_section,
            *trace_section,
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.Div(
                id="hover-info",
                style={
                    "fontSize": "12px",
                    "color": "#495057",
                    "whiteSpace": "pre-wrap",
                    "lineHeight": "1.6",
                },
                children="Hover over a node or edge for details.",
            ),
        ],
        style={
            "width": "220px",
            "minWidth": "220px",
            "padding": "16px",
            "background": "#f8f9fa",
            "borderRight": "1px solid #dee2e6",
            "overflowY": "auto",
            "fontFamily": "system-ui, -apple-system, sans-serif",
            "boxSizing": "border-box",
        },
    )


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    topology_path: Path,
    trace_path: Optional[Path] = None,
) -> dash.Dash:
    """Build and return the Dash app for the given topology (and optional trace)."""
    raw    = _load(topology_path)
    nodes: list[dict] = raw.get("nodes", [])
    edges: list[dict] = raw.get("edges", [])
    geo    = _has_geo(nodes)
    trace: Optional[dict] = _load(trace_path) if trace_path is not None else None
    packets: list[dict]   = trace["packets"] if trace else []

    w_counts: Optional[dict[str, int]] = _witness_counts(trace) if trace else None
    max_w = max(w_counts.values(), default=0) if w_counts else 0
    all_steps: list[list[list[dict]]] = [_broadcast_steps(p) for p in packets]

    # Cross-check trace metadata against the loaded topology
    trace_warning: Optional[str] = None
    if trace:
        trace_topo = trace.get("topology")
        if trace_topo and trace_topo != topology_path.name:
            trace_warning = (
                f"⚠ Trace was recorded for '{trace_topo}' "
                f"but topology is '{topology_path.name}'"
            )
        elif trace.get("nodes"):
            topo_node_names = {n["name"] for n in nodes}
            trace_node_names = set(trace["nodes"])
            if not trace_node_names.intersection(topo_node_names):
                trace_warning = (
                    "⚠ No node names in this trace match the topology — "
                    "wrong trace file?"
                )

    app = dash.Dash(__name__, title=f"{topology_path.stem} — MeshCore viz")

    sidebar = _sidebar(
        topology_path, nodes, edges, geo,
        trace=trace, w_counts=w_counts, trace_warning=trace_warning,
        all_steps=all_steps,
    )

    if geo:
        main_panel = dcc.Graph(
            id="geo-graph",
            figure=_geo_figure(nodes, edges, w_counts, max_w),
            style={"flex": "1", "height": "100vh"},
            config={"scrollZoom": True},
        )
    else:
        main_panel = cyto.Cytoscape(
            id="cyto-graph",
            elements=_cyto_elements(nodes, edges, w_counts, max_w),
            layout={"name": "cose", "animate": False, "randomize": False},
            style={"flex": "1", "height": "100vh"},
            stylesheet=_CYTO_STYLESHEET,
            userZoomingEnabled=True,
            userPanningEnabled=True,
        )

    extra = (
        [dcc.Interval(id="play-interval", interval=500, disabled=True, n_intervals=0)]
        if trace and packets else []
    )
    app.layout = html.Div(
        [sidebar, main_panel] + extra,
        style={
            "display": "flex",
            "height": "100vh",
            "overflow": "hidden",
            "fontFamily": "system-ui, -apple-system, sans-serif",
        },
    )

    # ── Callbacks ──────────────────────────────────────────────────────────

    # Phase 2: packet step-through
    if trace and packets:
        if geo:
            @app.callback(
                Output("geo-graph", "figure"),
                Output("packet-info", "children"),
                Output("hop-info", "children"),
                Input("packet-slider", "value"),
                Input("hop-slider", "value"),
                Input("view-mode", "value"),
            )
            def _on_packet_geo(idx: int, hop_idx: int, view_mode: str) -> tuple:
                idx       = idx or 0
                view_mode = view_mode or "global"
                step_idx  = hop_idx if hop_idx is not None else -1
                pkt   = packets[idx]
                steps = all_steps[idx]
                if 0 <= step_idx < len(steps):
                    step      = steps[step_idx]
                    senders   = [step[0]["sender"]]
                    receivers = [h["receiver"] for h in step]
                else:
                    senders   = pkt["unique_senders"]
                    receivers = pkt["unique_receivers"]
                pkt_witnesses = (
                    set(pkt["unique_receivers"]) if view_mode == "packet" else None
                )
                fig = _geo_figure(
                    nodes, edges,
                    witness_counts=w_counts if view_mode == "global" else None,
                    max_count=max_w if view_mode == "global" else 0,
                    packet_witnesses=pkt_witnesses,
                    highlight_senders=senders,
                    highlight_receivers=receivers,
                )
                return (
                    fig,
                    _packet_info_children(pkt, idx, len(packets)),
                    _step_info_children(pkt, step_idx, steps),
                )

        else:
            @app.callback(
                Output("cyto-graph", "stylesheet"),
                Output("packet-info", "children"),
                Output("hop-info", "children"),
                Input("packet-slider", "value"),
                Input("hop-slider", "value"),
                Input("view-mode", "value"),
            )
            def _on_packet_cyto(idx: int, hop_idx: int, view_mode: str) -> tuple:
                idx       = idx or 0
                view_mode = view_mode or "global"
                step_idx  = hop_idx if hop_idx is not None else -1
                pkt   = packets[idx]
                steps = all_steps[idx]
                if 0 <= step_idx < len(steps):
                    step      = steps[step_idx]
                    senders   = [step[0]["sender"]]
                    receivers = [h["receiver"] for h in step]
                else:
                    senders   = pkt["unique_senders"]
                    receivers = pkt["unique_receivers"]
                stylesheet = list(_CYTO_STYLESHEET)
                # Per-packet mode: override each node's base colour before overlays.
                # Nodes that received this packet → muted green; others → grey.
                # Sender/receiver overlays (added below) win via CSS ordering.
                if view_mode == "packet":
                    pkt_witnesses = set(pkt["unique_receivers"])
                    for n in nodes:
                        colour = (
                            _RECEIVED_COLOUR if n["name"] in pkt_witnesses
                            else "#e9ecef"
                        )
                        stylesheet.append({
                            "selector": f'node[id = "{n["name"]}"]',
                            "style": {"background-color": colour},
                        })
                for s in senders:
                    stylesheet.append({
                        "selector": f'node[id = "{s}"]',
                        "style": {"background-color": _SENDER_COLOUR},
                    })
                for r in receivers:
                    stylesheet.append({
                        "selector": f'node[id = "{r}"]',
                        "style": {"background-color": _RECEIVER_COLOUR},
                    })
                return (
                    stylesheet,
                    _packet_info_children(pkt, idx, len(packets)),
                    _step_info_children(pkt, step_idx, steps),
                )

        # Advance on each interval tick.
        # In normal mode: step packets one-by-one (loop).
        # In hop mode: step hops within the current packet; when exhausted,
        # advance to the next packet and start at hop 0.
        @app.callback(
            Output("packet-slider", "value"),
            Output("hop-slider", "value", allow_duplicate=True),
            Input("play-interval", "n_intervals"),
            State("packet-slider", "value"),
            State("hop-slider", "value"),
            State("hop-slider", "max"),
            State("hop-play-mode", "value"),
            prevent_initial_call=True,
        )
        def _advance_play(
            _,
            pkt_idx: int,
            hop_idx: int,
            hop_max: int,
            hop_mode: list,
        ) -> tuple:
            pkt_idx = pkt_idx or 0
            hop_idx = hop_idx if hop_idx is not None else -1
            hop_max = hop_max if hop_max is not None else 0
            if hop_mode:
                # Hop-by-hop mode: step through hops, then next packet
                if hop_idx < hop_max:
                    return dash.no_update, hop_idx + 1
                else:
                    return (pkt_idx + 1) % len(packets), 0
            else:
                # Packet mode: advance packet, leave hop slider unchanged
                return (pkt_idx + 1) % len(packets), dash.no_update

        # Reset hop slider when the packet changes (user drag or play advance).
        # In hop mode: reset to hop 0 so animation starts from the first hop.
        # In normal mode: reset to -1 (show all hops).
        @app.callback(
            Output("hop-slider", "max"),
            Output("hop-slider", "value"),
            Input("packet-slider", "value"),
            State("hop-play-mode", "value"),
        )
        def _reset_hop(pkt_idx: int, hop_mode: list) -> tuple:
            pkt_idx  = pkt_idx or 0
            n_steps  = len(all_steps[pkt_idx])
            reset_val = 0 if hop_mode else -1
            return max(n_steps - 1, 0), reset_val

        # Play/pause button toggles interval
        @app.callback(
            Output("play-interval", "disabled"),
            Output("play-btn", "children"),
            Input("play-btn", "n_clicks"),
            State("play-interval", "disabled"),
            prevent_initial_call=True,
        )
        def _toggle_play(_, is_disabled: bool) -> tuple:
            playing = is_disabled   # about to start playing
            return not is_disabled, "⏸" if playing else "▶"

        # Speed dropdown changes interval period
        @app.callback(
            Output("play-interval", "interval"),
            Input("play-speed", "value"),
        )
        def _set_speed(ms: int) -> int:
            return ms or 500

    # Phase 1: hover detail for cytoscape
    if not geo:
        @app.callback(
            Output("hover-info", "children"),
            Input("cyto-graph", "mouseoverNodeData"),
            Input("cyto-graph", "mouseoverEdgeData"),
        )
        def _on_hover(
            node_data: dict | None,
            edge_data: dict | None,
        ) -> str:
            if node_data:
                nid = node_data["id"]
                witness_str = (
                    f"\nWitnesses: {node_data.get('witness', 0)}"
                    if trace else ""
                )
                return (
                    f"{_short(nid)}\n"
                    f"{nid}\n"
                    f"Role: {node_data.get('role', '?')}"
                    f"{witness_str}"
                )
            if edge_data:
                loss_pct = float(edge_data.get("loss", 0)) * 100
                src, tgt = edge_data["source"], edge_data["target"]
                return (
                    f"Edge\n"
                    f"  {_short(src)} ↔ {_short(tgt)}\n"
                    f"  {src}\n"
                    f"  {tgt}\n"
                    f"Loss:    {loss_pct:.1f}%\n"
                    f"Latency: {edge_data.get('latency_ms', 0):.1f} ms\n"
                    f"SNR:     {edge_data.get('snr', 0):.1f} dB\n"
                    f"RSSI:    {edge_data.get('rssi', 0):.0f} dBm"
                )
            return "Hover over a node or edge for details."

    return app
