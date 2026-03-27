"""map_helpers.py — Leaflet map rendering for topology nodes and edges."""

from __future__ import annotations

import math
from typing import Optional

from nicegui import ui

from orchestrator.config import NodeConfig, EdgeConfig, TopologyConfig


# -- Colour palette (matches viz/app.py) ------------------------------------

ROLE_COLOUR = {
    "relay":       "#3a86ff",   # blue
    "room_server": "#ffbe0b",   # amber
    "endpoint":    "#8d99ae",   # grey
}
EDGE_COLOUR = "rgba(173,181,189,0.6)"
SENDER_COLOUR   = "#f77f00"   # orange
RECEIVER_COLOUR = "#2dc653"   # green
RECEIVED_COLOUR = "#74b3ce"   # soft blue
COLLISION_COLOUR = "#e63946"  # red

LABEL_LEN = 8


def short_name(name: str) -> str:
    return (name[:LABEL_LEN] + "\u2026") if len(name) > LABEL_LEN else name


def node_role(n: NodeConfig) -> str:
    if n.room_server:
        return "room_server"
    if n.relay:
        return "relay"
    return "endpoint"


# -- Geo detection -----------------------------------------------------------

def has_geo(nodes: list[NodeConfig]) -> bool:
    """True when every node has a non-zero lat/lon pair."""
    if not nodes:
        return False
    for n in nodes:
        if n.lat is None or n.lon is None:
            return False
        if n.lat == 0.0 and n.lon == 0.0:
            return False
    return True


# -- Synthetic layout for non-geo topologies ---------------------------------

def circular_positions(nodes: list[NodeConfig]) -> dict[str, tuple[float, float]]:
    """Place nodes evenly on a small circle centred at (0, 0).

    Returns {name: (lat, lon)} using small synthetic coords that Leaflet
    can render without a tile layer (or with one centred at 0,0).
    """
    n = len(nodes)
    if n == 0:
        return {}
    radius = 0.005 * max(n, 3)  # degrees — small enough for Leaflet
    positions = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / n - math.pi / 2  # start at top
        positions[node.name] = (
            radius * math.sin(angle),
            radius * math.cos(angle),
        )
    return positions


# -- Map rendering -----------------------------------------------------------

def compute_center_zoom(
    positions: dict[str, tuple[float, float]],
) -> tuple[tuple[float, float], int]:
    """Compute map center and a reasonable zoom level from node positions."""
    if not positions:
        return (0.0, 0.0), 2

    lats = [p[0] for p in positions.values()]
    lons = [p[1] for p in positions.values()]
    center = (sum(lats) / len(lats), sum(lons) / len(lons))

    lat_span = max(lats) - min(lats)
    lon_span = max(lons) - min(lons)
    span = max(lat_span, lon_span, 1e-6)

    # rough heuristic: leaflet zoom where 360/2^z ≈ span*2
    zoom = max(1, min(18, int(math.log2(360 / (span * 2.5)))))
    return center, zoom


def node_positions(topo: TopologyConfig) -> dict[str, tuple[float, float]]:
    """Return {name: (lat, lon)} for all nodes, using geo or circular layout."""
    if has_geo(topo.nodes):
        return {n.name: (n.lat, n.lon) for n in topo.nodes}  # type: ignore[arg-type]
    return circular_positions(topo.nodes)


def _marker_icon_html(colour: str, label: str) -> str:
    """Return HTML for a coloured circle marker with a tooltip-style label."""
    return (
        f'<div style="background:{colour};width:14px;height:14px;'
        f'border-radius:50%;border:2px solid white;'
        f'box-shadow:0 0 4px rgba(0,0,0,0.4);"></div>'
    )


def render_topology(
    leaflet_map: ui.leaflet,
    topo: TopologyConfig,
    positions: Optional[dict[str, tuple[float, float]]] = None,
) -> dict:
    """Draw nodes and edges on the Leaflet map.

    Returns a dict with:
      markers: {node_name: Marker}
      edges:   [(edge, GenericLayer)]
    """
    if positions is None:
        positions = node_positions(topo)

    markers = {}
    edge_layers = []

    node_map = {n.name: n for n in topo.nodes}

    with leaflet_map:
        # Draw edges first (below markers)
        for edge in topo.edges:
            pos_a = positions.get(edge.a)
            pos_b = positions.get(edge.b)
            if pos_a is None or pos_b is None:
                continue
            layer = leaflet_map.generic_layer(
                name='polyline',
                args=[
                    [list(pos_a), list(pos_b)],
                    {"color": EDGE_COLOUR, "weight": 2, "opacity": 0.7},
                ],
            )
            edge_layers.append((edge, layer))

        # Draw node markers (draggable for editing)
        for node in topo.nodes:
            pos = positions.get(node.name)
            if pos is None:
                continue
            marker = leaflet_map.marker(latlng=pos).draggable()
            markers[node.name] = marker

    return {"markers": markers, "edges": edge_layers}


# -- Popup / tooltip binding --------------------------------------------------

def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#39;")


def _node_popup_html(node: NodeConfig, edges: list | None = None) -> str:
    """Build HTML for a marker popup showing node properties and edges."""
    role = node_role(node)
    colour = ROLE_COLOUR.get(role, ROLE_COLOUR["endpoint"])
    lines = [
        f'<b style="color:{colour}">{_escape_html(short_name(node.name))}</b>',
        f'<span style="color:#888">Role:</span> {role}',
    ]
    if node.lat is not None and node.lon is not None:
        lines.append(f'<span style="color:#888">Lat:</span> {node.lat:.6f}')
        lines.append(f'<span style="color:#888">Lon:</span> {node.lon:.6f}')
    if node.prv_key:
        lines.append(
            f'<span style="color:#888">Key:</span> '
            f'<code style="font-size:0.85em">{node.prv_key[:16]}\u2026</code>'
        )
    if edges:
        lines.append('<hr style="margin:4px 0;border-color:#555">')
        lines.append(f'<span style="color:#888">Edges ({len(edges)}):</span>')
        for edge in edges:
            peer = edge.b if edge.a == node.name else edge.a
            # SNR in direction from this node to peer
            if edge.a == node.name and edge.a_to_b and edge.a_to_b.snr is not None:
                snr = edge.a_to_b.snr
            elif edge.b == node.name and edge.b_to_a and edge.b_to_a.snr is not None:
                snr = edge.b_to_a.snr
            else:
                snr = edge.snr
            loss = edge.loss
            if edge.a == node.name and edge.a_to_b and edge.a_to_b.loss is not None:
                loss = edge.a_to_b.loss
            elif edge.b == node.name and edge.b_to_a and edge.b_to_a.loss is not None:
                loss = edge.b_to_a.loss
            parts = [f'SNR:{snr:.1f}']
            if loss > 0:
                parts.append(f'L:{loss*100:.0f}%')
            lines.append(
                f'&nbsp;\u2192 {_escape_html(short_name(peer))}'
                f' <span style="color:#aaa;font-size:0.9em">{" ".join(parts)}</span>'
            )
    return '<br>'.join(lines)


def _edge_popup_html(edge: EdgeConfig) -> str:
    """Build HTML for an edge polyline popup showing link properties."""
    loss_pct = f"{edge.loss * 100:.1f}%" if edge.loss > 0 else "0%"
    lines = [
        f'<b>{_escape_html(short_name(edge.a))} \u2194 '
        f'{_escape_html(short_name(edge.b))}</b>',
        f'<span style="color:#888">Loss:</span> {loss_pct}',
        f'<span style="color:#888">Latency:</span> {edge.latency_ms:.1f} ms',
        f'<span style="color:#888">SNR:</span> {edge.snr:.1f} dB',
        f'<span style="color:#888">RSSI:</span> {edge.rssi:.1f} dBm',
    ]
    for label, ovr in [("A\u2192B", edge.a_to_b), ("B\u2192A", edge.b_to_a)]:
        if ovr is None:
            continue
        parts = []
        if ovr.loss is not None:
            parts.append(f"loss={ovr.loss * 100:.1f}%")
        if ovr.latency_ms is not None:
            parts.append(f"lat={ovr.latency_ms:.1f}ms")
        if ovr.snr is not None:
            parts.append(f"snr={ovr.snr:.1f}")
        if ovr.rssi is not None:
            parts.append(f"rssi={ovr.rssi:.1f}")
        if parts:
            lines.append(f'<span style="color:#888">{label}:</span> {", ".join(parts)}')
    return '<br>'.join(lines)


def bind_popups(
    leaflet_map: ui.leaflet,
    topo: TopologyConfig,
    markers: dict,
    edge_layers: list,
) -> None:
    """Bind tooltips and popups to all markers and edge polylines.

    - Hover shows tooltip (short name / edge summary).
    - Left-click opens popup with full properties.
    - Right-click also opens popup.

    Uses a single JS injection after map init.  NiceGUI exposes
    ``getElement(id)`` globally, which returns the Vue component;
    its ``.map`` property is the Leaflet map instance.
    """
    import json as _json

    node_map = {n.name: n for n in topo.nodes}

    # Pre-build edge lookup: node_name → list of connected edges
    from collections import defaultdict
    edges_by_node: dict[str, list] = defaultdict(list)
    for edge in topo.edges:
        edges_by_node[edge.a].append(edge)
        edges_by_node[edge.b].append(edge)

    # Build {layer_id: {tooltip, popup}} for all markers and edges
    layer_data: dict = {}

    for name, marker in markers.items():
        node = node_map.get(name)
        if not node:
            continue
        layer_data[marker.id] = {
            "tooltip": short_name(name),
            "popup": _node_popup_html(node, edges_by_node.get(name)),
        }

    for edge, layer in edge_layers:
        layer_data[layer.id] = {
            "tooltip": f"{short_name(edge.a)} \u2194 {short_name(edge.b)}",
            "popup": _edge_popup_html(edge),
        }

    data_json = _json.dumps(layer_data)
    element_id = leaflet_map.id

    # Inject after the map is fully initialised so all layers exist in JS.
    # Use the element's client.run_javascript to avoid slot-stack errors.
    js = f'''
        (function() {{
            var data = {data_json};
            var comp = getElement({element_id});
            if (!comp || !comp.map) return;
            comp.$el.addEventListener("contextmenu", function(e) {{
                e.preventDefault();
            }});
            comp.map.eachLayer(function(layer) {{
                if (!layer.id) return;
                var info = data[layer.id];
                if (!info) return;
                if (info.tooltip) layer.bindTooltip(info.tooltip);
                if (info.popup) {{
                    layer.bindPopup(info.popup);
                    layer.on("contextmenu", function(e) {{
                        L.DomEvent.stopPropagation(e);
                        L.DomEvent.preventDefault(e);
                        this.openPopup();
                    }});
                }}
            }});
        }})();
    '''

    def _on_init(_):
        leaflet_map.client.run_javascript(js)

    leaflet_map.on('init', _on_init)


def add_marker(
    leaflet_map: ui.leaflet,
    name: str,
    latlng: tuple[float, float],
) -> object:
    """Add a single draggable marker to the map. Returns the Marker."""
    with leaflet_map:
        return leaflet_map.marker(latlng=latlng).draggable()


def add_edge_layer(
    leaflet_map: ui.leaflet,
    pos_a: tuple[float, float],
    pos_b: tuple[float, float],
) -> object:
    """Add a single polyline edge to the map. Returns the GenericLayer."""
    with leaflet_map:
        return leaflet_map.generic_layer(
            name='polyline',
            args=[
                [list(pos_a), list(pos_b)],
                {"color": EDGE_COLOUR, "weight": 2, "opacity": 0.7},
            ],
        )
