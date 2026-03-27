"""app.py — NiceGUI application factory for the MeshCore workbench."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from nicegui import ui

from orchestrator.config import load_topology
from . import topology_editor, sim_panel, trace_viewer
from .map_helpers import (
    bind_popups, compute_center_zoom, has_geo, node_positions, render_topology,
)
from .results import list_runs
from .state import AppState
from .trace_loader import load_trace

# Height for the main content area below tabs only (no header)
# Quasar tabs with icons ≈ 72px; use 75px for safety
_CONTENT_H = "calc(100vh - 75px)"


def create_app(
    topology_path: Optional[str] = None,
    trace_path: Optional[str] = None,
    output_dir: str = "output",
) -> None:
    """Register the NiceGUI page.  Call ui.run() afterwards to start serving."""

    @ui.page("/")
    def index():
        state = AppState()
        state._output_dir = output_dir

        # Load topology if provided
        if topology_path:
            state.topology = load_topology(topology_path)
            state.topology_path = topology_path

        # Load trace if provided
        if trace_path:
            state.trace = load_trace(trace_path)

        # Scan existing persistent runs
        state._available_runs = list_runs(state._output_dir)

        # -- Page chrome --
        topo_name = Path(state.topology_path).name if state.topology_path else ""
        ui.page_title(f"MeshCore Workbench — {topo_name}" if topo_name else "MeshCore Workbench")

        # -- Tab bar (no header — maximize vertical space) --
        with ui.tabs().classes("w-full") as tabs:
            tab_topo = ui.tab("Topology", icon="map")
            tab_sim = ui.tab("Simulation", icon="play_circle")
            tab_trace = ui.tab("Trace Viewer", icon="timeline")
            if topo_name:
                ui.space()
                ui.label(topo_name).classes("text-caption text-grey self-center")

        # -- Tab panels (no padding — we manage layout ourselves) --
        with ui.tab_panels(tabs, value=tab_topo).classes("w-full p-0").style(
            f"height: {_CONTENT_H}"
        ):
            _build_topology_tab(tab_topo, state)
            _build_simulation_tab(tab_sim, state)
            _build_trace_tab(tab_trace, state)


def _build_topology_tab(tab: ui.tab, state: AppState) -> None:
    with ui.tab_panel(tab).classes("p-0 w-full h-full"):
        with ui.row().classes("w-full h-full no-wrap gap-0"):
            # -- Sidebar (fixed 300px) --
            with ui.scroll_area().style(
                "width: 480px; min-width: 480px"
            ).classes("h-full p-2 border-r"):
                topology_editor.render_sidebar(state)

            # -- Map (fills remaining space) --
            with ui.element("div").classes("h-full").style(
                "flex: 1; min-width: 0"
            ):
                if state.topology:
                    positions = node_positions(state.topology)
                    state.positions = dict(positions)
                    center, zoom = compute_center_zoom(positions)
                    m = ui.leaflet(
                        center=center, zoom=zoom
                    ).classes("w-full h-full")
                    result = render_topology(m, state.topology, positions)
                    state.leaflet_map = m
                    state.markers = result["markers"]
                    state.edge_layers = result["edges"]
                    bind_popups(m, state.topology, state.markers, state.edge_layers)
                else:
                    with ui.column().classes(
                        "w-full h-full items-center justify-center"
                    ):
                        ui.icon("map").classes("text-6xl text-grey-4")
                        ui.label(
                            "Load a topology to get started"
                        ).classes("text-grey")


def _build_simulation_tab(tab: ui.tab, state: AppState) -> None:
    with ui.tab_panel(tab).classes("p-0 w-full h-full"):
        with ui.row().classes("w-full h-full no-wrap gap-0"):
            with ui.scroll_area().style(
                "width: 480px; min-width: 480px"
            ).classes("h-full p-2 border-r"):
                sim_panel.render_sidebar(state)
            with ui.element("div").classes("h-full").style(
                "flex: 1; min-width: 0"
            ):
                sim_panel.render_main(state)


def _build_trace_tab(tab: ui.tab, state: AppState) -> None:
    with ui.tab_panel(tab).classes("p-0 w-full h-full"):
        with ui.row().classes("w-full h-full no-wrap gap-0"):
            with ui.scroll_area().style(
                "width: 480px; min-width: 480px"
            ).classes("h-full p-2 border-r"):
                trace_viewer.render_sidebar(state)
            # position:relative lets trace_viewer use absolute positioning
            # to bypass the @ui.refreshable wrapper div height issue
            with ui.element("div").classes("h-full").style(
                "flex: 1; min-width: 0; position: relative"
            ):
                trace_viewer.render_main(state)
