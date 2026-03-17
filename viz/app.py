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
from dash import Input, Output, dcc, html

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
_RECEIVER_COLOUR = "#2dc653"   # green  — active packet receivers

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
            marker=dict(size=15, color=colour, opacity=0.7),
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


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _sidebar(
    topology_path: Path,
    nodes: list[dict],
    edges: list[dict],
    geo: bool,
    trace: Optional[dict] = None,
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
            slider: Any = dcc.Slider(
                id="packet-slider",
                min=0,
                max=n_pkts - 1,
                step=1,
                value=0,
                marks=None,
                tooltip={"placement": "bottom", "always_visible": False},
            )
        else:
            slider = html.P(
                "No packets in trace.",
                style={"fontSize": "12px", "color": "#6c757d"},
            )

        trace_section = [
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.Div(
                "Witness heatmap",
                style={"fontSize": "11px", "color": "#6c757d", "marginBottom": "4px"},
            ),
            scale_bar,
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.P(f"Packets: {n_pkts}", style={"margin": "2px 0", "fontSize": "13px"}),
            html.P(f"Flood:   {flood_pct:.0f}%", style={"margin": "2px 0", "fontSize": "13px"}),
            html.P(f"Avg witnesses: {mean_w:.1f}", style={"margin": "2px 0", "fontSize": "13px"}),
            html.Hr(style={"margin": "10px 0", "borderColor": "#dee2e6"}),
            html.Div(
                "Step through packets:",
                style={"fontSize": "12px", "color": "#6c757d", "marginBottom": "4px"},
            ),
            html.Div(
                [
                    html.Span("■ sender  ", style={"color": _SENDER_COLOUR, "fontSize": "11px"}),
                    html.Span("■ receiver", style={"color": _RECEIVER_COLOUR, "fontSize": "11px"}),
                ],
                style={"marginBottom": "6px"},
            ),
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

    app = dash.Dash(__name__, title=f"{topology_path.stem} — MeshCore viz")

    sidebar = _sidebar(topology_path, nodes, edges, geo, trace=trace)

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

    app.layout = html.Div(
        [sidebar, main_panel],
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
                Input("packet-slider", "value"),
            )
            def _on_packet_geo(idx: int) -> tuple:
                idx = idx or 0
                pkt = packets[idx]
                fig = _geo_figure(
                    nodes, edges, w_counts, max_w,
                    highlight_senders=pkt["unique_senders"],
                    highlight_receivers=pkt["unique_receivers"],
                )
                return fig, _packet_info_children(pkt, idx, len(packets))

        else:
            @app.callback(
                Output("cyto-graph", "stylesheet"),
                Output("packet-info", "children"),
                Input("packet-slider", "value"),
            )
            def _on_packet_cyto(idx: int) -> tuple:
                idx = idx or 0
                pkt = packets[idx]
                stylesheet = list(_CYTO_STYLESHEET)
                for s in pkt["unique_senders"]:
                    stylesheet.append({
                        "selector": f'[id = "{s}"]',
                        "style": {"border-width": "3px", "border-color": _SENDER_COLOUR},
                    })
                for r in pkt["unique_receivers"]:
                    stylesheet.append({
                        "selector": f'[id = "{r}"]',
                        "style": {"border-width": "3px", "border-color": _RECEIVER_COLOUR},
                    })
                return stylesheet, _packet_info_children(pkt, idx, len(packets))

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
