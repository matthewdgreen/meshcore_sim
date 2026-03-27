"""topology_editor.py — Tab 1: Interactive topology editor."""

from __future__ import annotations

import json
from typing import Optional

from nicegui import ui

from orchestrator.config import (
    DirectionalOverrides, EdgeConfig, NodeConfig, TopologyConfig,
    topology_to_dict,
)
from .map_helpers import (
    ROLE_COLOUR, add_edge_layer, add_marker, node_role, short_name, has_geo,
)
from .state import AppState


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

def _node_edit_dialog(
    state: AppState,
    node: NodeConfig,
    is_new: bool,
    on_save: callable,
    on_delete: Optional[callable] = None,
) -> None:
    """Open a dialog to edit (or create) a node."""
    with ui.dialog() as dlg, ui.card().classes("w-96"):
        ui.label("New Node" if is_new else f"Edit: {short_name(node.name)}").classes(
            "text-h6"
        )

        name_input = ui.input("Name", value=node.name).classes("w-full")
        if not is_new:
            name_input.props("readonly")

        relay_cb = ui.checkbox("Relay", value=node.relay)
        room_cb = ui.checkbox("Room Server", value=node.room_server)

        with ui.row().classes("w-full gap-2"):
            lat_input = ui.number(
                "Latitude", value=node.lat, format="%.6f"
            ).classes("flex-1")
            lon_input = ui.number(
                "Longitude", value=node.lon, format="%.6f"
            ).classes("flex-1")

        prv_input = ui.input(
            "Private key (hex)", value=node.prv_key or ""
        ).classes("w-full")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            if on_delete and not is_new:
                ui.button(
                    "Delete", color="negative",
                    on_click=lambda: (on_delete(node), dlg.close()),
                ).props("flat")
                ui.space()

            ui.button("Cancel", on_click=dlg.close).props("flat")
            def do_save():
                node.name = name_input.value.strip()
                node.relay = relay_cb.value
                node.room_server = room_cb.value
                node.lat = lat_input.value
                node.lon = lon_input.value
                node.prv_key = prv_input.value.strip() or None
                on_save(node, is_new)
                dlg.close()

            ui.button("Save", color="primary", on_click=do_save)

    dlg.open()


def _edge_edit_dialog(
    state: AppState,
    edge: EdgeConfig,
    is_new: bool,
    on_save: callable,
    on_delete: Optional[callable] = None,
) -> None:
    """Open a dialog to edit (or create) an edge."""
    node_names = [n.name for n in state.topology.nodes]

    with ui.dialog() as dlg, ui.card().classes("w-96"):
        ui.label("New Edge" if is_new else "Edit Edge").classes("text-h6")

        if is_new:
            a_select = ui.select(
                node_names, value=edge.a, label="Node A"
            ).classes("w-full")
            b_select = ui.select(
                node_names, value=edge.b, label="Node B"
            ).classes("w-full")
        else:
            ui.label(
                f"{short_name(edge.a)} \u2194 {short_name(edge.b)}"
            ).classes("text-subtitle1")

        loss_input = ui.number(
            "Loss (0\u20131)", value=edge.loss, min=0, max=1,
            step=0.01, format="%.2f",
        ).classes("w-full")
        latency_input = ui.number(
            "Latency (ms)", value=edge.latency_ms, min=0, format="%.1f",
        ).classes("w-full")

        with ui.row().classes("w-full gap-2"):
            snr_input = ui.number(
                "SNR (dB)", value=edge.snr, format="%.1f",
            ).classes("flex-1")
            rssi_input = ui.number(
                "RSSI (dBm)", value=edge.rssi, format="%.1f",
            ).classes("flex-1")

        # -- Directional overrides (expandable) --
        with ui.expansion("Directional Overrides").classes("w-full"):
            ui.label(f"A \u2192 B").classes("text-caption font-bold")
            atob = edge.a_to_b or DirectionalOverrides()
            atob_loss = ui.number("Loss", value=atob.loss, format="%.2f").classes("w-full")
            atob_lat = ui.number("Latency ms", value=atob.latency_ms, format="%.1f").classes("w-full")
            atob_snr = ui.number("SNR dB", value=atob.snr, format="%.1f").classes("w-full")
            atob_rssi = ui.number("RSSI dBm", value=atob.rssi, format="%.1f").classes("w-full")

            ui.label(f"B \u2192 A").classes("text-caption font-bold mt-2")
            btoa = edge.b_to_a or DirectionalOverrides()
            btoa_loss = ui.number("Loss", value=btoa.loss, format="%.2f").classes("w-full")
            btoa_lat = ui.number("Latency ms", value=btoa.latency_ms, format="%.1f").classes("w-full")
            btoa_snr = ui.number("SNR dB", value=btoa.snr, format="%.1f").classes("w-full")
            btoa_rssi = ui.number("RSSI dBm", value=btoa.rssi, format="%.1f").classes("w-full")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            if on_delete and not is_new:
                ui.button(
                    "Delete", color="negative",
                    on_click=lambda: (on_delete(edge), dlg.close()),
                ).props("flat")
                ui.space()

            ui.button("Cancel", on_click=dlg.close).props("flat")

            def do_save():
                if is_new:
                    edge.a = a_select.value
                    edge.b = b_select.value
                edge.loss = loss_input.value or 0.0
                edge.latency_ms = latency_input.value or 0.0
                edge.snr = snr_input.value if snr_input.value is not None else 6.0
                edge.rssi = rssi_input.value if rssi_input.value is not None else -90.0

                # Directional overrides (None if all fields empty)
                def _build_dir(l, la, s, r):
                    if all(v is None for v in (l.value, la.value, s.value, r.value)):
                        return None
                    return DirectionalOverrides(
                        loss=l.value, latency_ms=la.value,
                        snr=s.value, rssi=r.value,
                    )

                edge.a_to_b = _build_dir(atob_loss, atob_lat, atob_snr, atob_rssi)
                edge.b_to_a = _build_dir(btoa_loss, btoa_lat, btoa_snr, btoa_rssi)
                on_save(edge, is_new)
                dlg.close()

            ui.button("Save", color="primary", on_click=do_save)

    dlg.open()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar(state: AppState) -> None:
    """Render the interactive topology editor sidebar."""
    topo = state.topology
    if topo is None:
        ui.label("No topology loaded.").classes("text-grey")
        return

    # -- Shared mutable state for filter and placement mode --
    filter_state = {'text': ''}
    place_mode = {'active': False, 'btn': None}

    # -- Summary --
    geo = has_geo(topo.nodes)
    with ui.row().classes("items-center w-full"):
        ui.label("Topology").classes("text-h6")
        if state.dirty:
            ui.badge("unsaved", color="warning").props("dense")

    if state.topology_path:
        ui.label(state.topology_path).classes("text-caption text-grey")
    ui.label(
        f"{len(topo.nodes)} nodes, {len(topo.edges)} edges"
        f"  {'(geo)' if geo else '(synthetic layout)'}"
    ).classes("text-body2")

    ui.separator()

    # -- Radio config --
    if topo.radio:
        ui.label("Radio").classes("text-subtitle2")
        r = topo.radio
        bw_khz = r.bw_hz / 1000
        ui.label(f"SF{r.sf}  BW {bw_khz:.0f} kHz  CR 4/{4 + r.cr}").classes(
            "text-body2"
        )
        ui.separator()

    # -- Action buttons --
    with ui.row().classes("w-full gap-1"):
        ui.button(
            "Add Node", icon="add_circle", color="primary",
            on_click=lambda: _handle_add_node(state, node_list, edge_list),
        ).props("dense flat size=sm")
        place_btn = ui.button(
            "Place on Map", icon="add_location", color="secondary",
            on_click=lambda: _toggle_place_mode(state, place_mode, node_list, edge_list),
        ).props("dense flat size=sm")
        place_mode['btn'] = place_btn
        ui.button(
            "Add Edge", icon="link", color="primary",
            on_click=lambda: _handle_add_edge(state, node_list, edge_list),
        ).props("dense flat size=sm")
        ui.space()
        ui.button(
            "Save", icon="save", color="positive",
            on_click=lambda: _handle_save(state),
        ).props("dense flat size=sm")

    ui.separator()

    # -- Filter input --
    def on_filter_change(e):
        val = e.value if hasattr(e, 'value') else (e.args if hasattr(e, 'args') else '')
        filter_state['text'] = val.strip().lower() if val else ''
        node_list.refresh()
        edge_list.refresh()

    ui.input(
        placeholder="Filter nodes/edges...",
        on_change=on_filter_change,
    ).props('dense outlined clearable').classes("w-full").style("font-size:0.85em")

    # -- Node list (refreshable) --
    @ui.refreshable
    def node_list():
        # Pre-compute edge counts
        edge_counts: dict[str, int] = {}
        for edge in topo.edges:
            edge_counts[edge.a] = edge_counts.get(edge.a, 0) + 1
            edge_counts[edge.b] = edge_counts.get(edge.b, 0) + 1

        ft = filter_state['text']
        filtered = [n for n in topo.nodes if ft in n.name.lower()] if ft else topo.nodes
        ui.label(f"Nodes ({len(filtered)})").classes("text-subtitle2")
        for node in filtered:
            _node_row(state, node, node_list, edge_list,
                      edge_count=edge_counts.get(node.name, 0),
                      geo=geo, filter_state=filter_state)

    # -- Edge list (refreshable) --
    @ui.refreshable
    def edge_list():
        ft = filter_state['text']
        if ft:
            filtered = [e for e in topo.edges
                        if ft in e.a.lower() or ft in e.b.lower()]
        else:
            filtered = topo.edges
        ui.label(f"Edges ({len(filtered)})").classes("text-subtitle2")
        for edge in filtered:
            _edge_row(state, edge, node_list, edge_list)

    node_list()
    ui.separator()
    edge_list()


def _node_row(state, node, node_list_refresh, edge_list_refresh,
              edge_count=0, geo=False, filter_state=None):
    """Render a single clickable node row in the sidebar."""
    role = node_role(node)
    colour = ROLE_COLOUR.get(role, ROLE_COLOUR["endpoint"])

    with ui.column().classes(
        "gap-0 py-0.5 cursor-pointer w-full rounded hover:bg-grey-2 px-1"
    ).on(
        "click",
        lambda n=node: _node_edit_dialog(
            state, n, is_new=False,
            on_save=lambda nd, _: _on_node_edited(state, node_list_refresh),
            on_delete=lambda nd: _on_node_deleted(state, nd, node_list_refresh, edge_list_refresh),
        ),
    ):
        with ui.row().classes("items-center gap-1 w-full"):
            ui.icon("circle").style(f"color:{colour};font-size:10px")
            ui.label(short_name(node.name)).classes("text-body2")
            if role != "endpoint":
                ui.badge(role, color="primary").props("dense outline")
            if edge_count > 0:
                ui.badge(str(edge_count), color="grey").props("dense outline").classes("text-xs")
        # Second line: coordinates for geo topologies
        if geo and node.lat is not None and node.lon is not None:
            ui.label(
                f"({node.lat:.4f}, {node.lon:.4f})"
            ).classes("text-caption text-grey").style(
                "margin-top:-2px; padding-left:18px"
            )


def _edge_row(state, edge, node_list_refresh, edge_list_refresh):
    """Render a single clickable edge row in the sidebar."""
    # Build compact parameter summary — only show non-default values
    params = []
    if edge.loss > 0:
        params.append(f"L:{edge.loss * 100:.0f}%")
    if edge.latency_ms > 0:
        params.append(f"{edge.latency_ms:.0f}ms")
    if edge.snr != 6.0:
        params.append(f"SNR:{edge.snr:.1f}")
    if edge.rssi != -90.0:
        params.append(f"RSSI:{edge.rssi:.0f}")
    has_dir = edge.a_to_b is not None or edge.b_to_a is not None

    with ui.column().classes(
        "gap-0 py-0.5 cursor-pointer w-full rounded hover:bg-grey-2 px-1"
    ).on(
        "click",
        lambda e=edge: _edge_edit_dialog(
            state, e, is_new=False,
            on_save=lambda ed, _: _on_edge_edited(state, ed, edge_list_refresh),
            on_delete=lambda ed: _on_edge_deleted(state, ed, edge_list_refresh),
        ),
    ):
        with ui.row().classes("items-center gap-1 w-full"):
            ui.label(
                f"{short_name(edge.a)} \u2194 {short_name(edge.b)}"
            ).classes("text-body2")
            if has_dir:
                ui.badge("\u2195", color="info").props("dense outline").classes("text-xs")
        if params:
            ui.label("  ".join(params)).classes(
                "text-caption text-grey"
            ).style("margin-top:-2px; padding-left:4px")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _mark_dirty(state: AppState) -> None:
    state.dirty = True


def _on_node_edited(state, node_list_refresh):
    """Called after a node's properties are saved in the dialog."""
    _mark_dirty(state)
    node_list_refresh.refresh()


def _on_node_deleted(state, node, node_list_refresh, edge_list_refresh):
    """Remove a node and its connected edges from the topology and map."""
    topo = state.topology
    name = node.name

    # Remove from topology
    topo.nodes = [n for n in topo.nodes if n.name != name]
    topo.edges = [e for e in topo.edges if e.a != name and e.b != name]

    # Remove marker from map
    marker = state.markers.pop(name, None)
    if marker and state.leaflet_map:
        state.leaflet_map.remove_layer(marker)

    # Remove connected edge layers from map
    new_edge_layers = []
    for edge, layer in state.edge_layers:
        if edge.a == name or edge.b == name:
            if state.leaflet_map:
                state.leaflet_map.remove_layer(layer)
        else:
            new_edge_layers.append((edge, layer))
    state.edge_layers = new_edge_layers

    state.positions.pop(name, None)
    _mark_dirty(state)
    node_list_refresh.refresh()
    edge_list_refresh.refresh()


def _on_edge_edited(state, edge, edge_list_refresh):
    """Called after an edge's properties are saved. Update the polyline if needed."""
    _mark_dirty(state)
    edge_list_refresh.refresh()


def _on_edge_deleted(state, edge, edge_list_refresh):
    """Remove an edge from the topology and map."""
    topo = state.topology
    topo.edges = [e for e in topo.edges if not (e.a == edge.a and e.b == edge.b)]

    # Remove from map
    new_edge_layers = []
    for e, layer in state.edge_layers:
        if e is edge:
            if state.leaflet_map:
                state.leaflet_map.remove_layer(layer)
        else:
            new_edge_layers.append((e, layer))
    state.edge_layers = new_edge_layers

    _mark_dirty(state)
    edge_list_refresh.refresh()


def _toggle_place_mode(state, place_mode, node_list_refresh, edge_list_refresh):
    """Toggle click-to-place-node mode on the map."""
    if not state.leaflet_map:
        ui.notify("No map available", type="warning")
        return

    if place_mode['active']:
        # Deactivate
        place_mode['active'] = False
        if place_mode['btn']:
            place_mode['btn'].props(remove="color=negative")
            place_mode['btn'].props(add="color=secondary")
        ui.notify("Placement mode off")
        return

    # Activate
    place_mode['active'] = True
    if place_mode['btn']:
        place_mode['btn'].props(remove="color=secondary")
        place_mode['btn'].props(add="color=negative")
    ui.notify("Click on the map to place a new node")

    def on_map_click(e):
        if not place_mode['active']:
            return
        # Deactivate after one placement
        place_mode['active'] = False
        if place_mode['btn']:
            place_mode['btn'].props(remove="color=negative")
            place_mode['btn'].props(add="color=secondary")

        latlng = e.args.get('latlng', {})
        lat = latlng.get('lat', 0.0)
        lng = latlng.get('lng', 0.0)

        new_node = NodeConfig(name="", lat=lat, lon=lng)

        def on_save(node, is_new):
            if not node.name:
                ui.notify("Node name is required", type="warning")
                return
            if any(n.name == node.name for n in state.topology.nodes):
                ui.notify(f"Node '{node.name}' already exists", type="warning")
                return

            state.topology.nodes.append(node)
            pos = (node.lat or 0.0, node.lon or 0.0)
            state.positions[node.name] = pos

            if state.leaflet_map:
                marker = add_marker(state.leaflet_map, node.name, pos)
                state.markers[node.name] = marker

            _mark_dirty(state)
            node_list_refresh.refresh()

        _node_edit_dialog(state, new_node, is_new=True, on_save=on_save)

    state.leaflet_map.on('map-click', on_map_click)


def _handle_add_node(state, node_list_refresh, edge_list_refresh):
    """Open dialog to add a new node."""
    if not state.topology:
        return

    # Default position: center of current map view
    center = (0.0, 0.0)
    if state.positions:
        lats = [p[0] for p in state.positions.values()]
        lons = [p[1] for p in state.positions.values()]
        center = (sum(lats) / len(lats), sum(lons) / len(lons))

    new_node = NodeConfig(name="", lat=center[0], lon=center[1])

    def on_save(node, is_new):
        if not node.name:
            ui.notify("Node name is required", type="warning")
            return
        if any(n.name == node.name for n in state.topology.nodes):
            ui.notify(f"Node '{node.name}' already exists", type="warning")
            return

        state.topology.nodes.append(node)
        pos = (node.lat or 0.0, node.lon or 0.0)
        state.positions[node.name] = pos

        # Add marker to map
        if state.leaflet_map:
            marker = add_marker(state.leaflet_map, node.name, pos)
            state.markers[node.name] = marker

        _mark_dirty(state)
        node_list_refresh.refresh()

    _node_edit_dialog(state, new_node, is_new=True, on_save=on_save)


def _handle_add_edge(state, node_list_refresh, edge_list_refresh):
    """Open dialog to add a new edge."""
    if not state.topology or len(state.topology.nodes) < 2:
        ui.notify("Need at least 2 nodes to add an edge", type="warning")
        return

    names = [n.name for n in state.topology.nodes]
    new_edge = EdgeConfig(a=names[0], b=names[1])

    def on_save(edge, is_new):
        if edge.a == edge.b:
            ui.notify("Cannot connect a node to itself", type="warning")
            return
        # Check for duplicate
        for e in state.topology.edges:
            if (e.a == edge.a and e.b == edge.b) or (e.a == edge.b and e.b == edge.a):
                ui.notify("Edge already exists", type="warning")
                return

        state.topology.edges.append(edge)

        # Add polyline to map
        pos_a = state.positions.get(edge.a)
        pos_b = state.positions.get(edge.b)
        if pos_a and pos_b and state.leaflet_map:
            layer = add_edge_layer(state.leaflet_map, pos_a, pos_b)
            state.edge_layers.append((edge, layer))

        _mark_dirty(state)
        edge_list_refresh.refresh()

    _edge_edit_dialog(state, new_edge, is_new=True, on_save=on_save)


async def _handle_save(state: AppState) -> None:
    """Read marker positions from map, update topology, write JSON."""
    if not state.topology:
        return

    # Read current marker positions from the map
    if state.leaflet_map:
        for name, marker in state.markers.items():
            try:
                result = await state.leaflet_map.run_layer_method(
                    marker.id, 'getLatLng', timeout=2,
                )
                if result and isinstance(result, dict):
                    lat = result.get('lat', 0.0)
                    lng = result.get('lng', 0.0)
                    state.positions[name] = (lat, lng)
                    # Update the node's lat/lon in topology
                    for n in state.topology.nodes:
                        if n.name == name:
                            n.lat = lat
                            n.lon = lng
                            break
            except Exception:
                pass  # marker may have been removed

    # Serialize and write
    if not state.topology_path:
        ui.notify("No file path set — use Save As", type="warning")
        return

    data = topology_to_dict(state.topology)
    with open(state.topology_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    state.dirty = False
    ui.notify(f"Saved to {state.topology_path}", type="positive")
