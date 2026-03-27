"""trace_viewer.py — Tab 3: Trace timeline with continuous time scrubbing."""

from __future__ import annotations

import bisect
import json as _json
from types import SimpleNamespace

from nicegui import ui

from .map_helpers import (
    ROLE_COLOUR, EDGE_COLOUR, SENDER_COLOUR, RECEIVER_COLOUR,
    RECEIVED_COLOUR, COLLISION_COLOUR,
    short_name, node_role, node_positions, compute_center_zoom,
)
from . import results as _results
from .state import AppState
from .trace_loader import (
    flatten_events, compute_trace_stats, broadcast_steps,
    compute_node_trace_stats, compute_edge_trace_stats,
)

# Events within +/- this window of current_time are "active"
WINDOW_SECS = 0.5

# Route type int → human name (from orchestrator/packet.py constants)
_ROUTE_NAMES = {
    0: "T_FLOOD", 1: "FLOOD", 2: "DIRECT", 3: "T_DIRECT",
}

# Selection highlight
_SELECTED_BORDER = "#ffffff"


# ---------------------------------------------------------------------------
# Public entry points (called by app.py)
# ---------------------------------------------------------------------------

def render_sidebar(state: AppState) -> None:
    @ui.refreshable
    def sidebar():
        _sidebar_content(state)
    sidebar()
    state._refresh_trace_sidebar = sidebar.refresh


def render_main(state: AppState) -> None:
    @ui.refreshable
    def main():
        _main_content(state)
    main()
    state._refresh_trace_main = main.refresh


# ---------------------------------------------------------------------------
# Selection management
# ---------------------------------------------------------------------------

def _clear_selection(state: AppState) -> None:
    """Clear all selections and refresh detail panel."""
    state._selected_pkt_idx = None
    state._selected_node_name = None
    state._selected_edge_key = None
    _refresh_detail(state)
    _update_map_overlay(state)


def _select_packet(state: AppState, pkt_idx: int) -> None:
    """Select a packet, jump timeline, pause playback."""
    packets = state.trace.get("packets", []) if state.trace else []
    if not (0 <= pkt_idx < len(packets)):
        return
    state._selected_pkt_idx = pkt_idx
    state._selected_node_name = None
    state._selected_edge_key = None

    tui = getattr(state, "_trace_ui", None)
    if tui is not None:
        if state.playing:
            _toggle_play(state)
        t = packets[pkt_idx]["first_seen_at"]
        state.current_time = t
        tui.slider.value = t

    _refresh_detail(state)


def _select_node(state: AppState, name: str) -> None:
    """Select a node for detail inspection."""
    state._selected_pkt_idx = None
    state._selected_node_name = name
    state._selected_edge_key = None
    _refresh_detail(state)
    _update_map_overlay(state)


def _select_edge(state: AppState, key: tuple) -> None:
    """Select an edge for detail inspection."""
    state._selected_pkt_idx = None
    state._selected_node_name = None
    state._selected_edge_key = key
    _refresh_detail(state)


def _refresh_detail(state: AppState) -> None:
    """Safely call the detail panel refresh if available."""
    fn = getattr(state, "_refresh_detail_panel", None)
    if fn is not None:
        fn()


# ---------------------------------------------------------------------------
# Detail panel renderers
# ---------------------------------------------------------------------------

def _render_detail_panel(state: AppState) -> None:
    """Render the appropriate detail panel based on current selection."""
    if state._selected_pkt_idx is not None:
        _render_packet_detail(state, state._selected_pkt_idx)
    elif state._selected_node_name is not None:
        _render_node_detail(state, state._selected_node_name)
    elif state._selected_edge_key is not None:
        _render_edge_detail(state, state._selected_edge_key)


def _detail_close_btn(state: AppState) -> None:
    """Render a close (X) button for the detail panel."""
    ui.button(
        icon="close", on_click=lambda: _clear_selection(state),
    ).props("flat dense round size=sm").classes("absolute-top-right")


def _render_packet_detail(state: AppState, pkt_idx: int) -> None:
    """Render packet detail panel."""
    packets = state.trace.get("packets", []) if state.trace else []
    if not (0 <= pkt_idx < len(packets)):
        return
    pkt = packets[pkt_idx]
    tui = getattr(state, "_trace_ui", None)
    t_min = tui.t_min if tui else 0.0

    with ui.card().classes("w-full relative"):
        _detail_close_btn(state)
        ui.label(f"Packet #{pkt_idx + 1}").classes("text-subtitle2")

        # Metadata
        route = "flood" if pkt.get("is_flood") else "direct"
        ui.label(f"Type: {pkt['payload_type_name']}").classes("text-body2")
        ui.label(f"Route: {route}").classes("text-body2")
        ui.label(f"Sender: {pkt['first_sender']}").classes("text-body2")
        ui.label(f"Witnesses: {pkt.get('witness_count', 0)}").classes("text-body2")
        avg_sz = pkt.get("avg_size_bytes")
        if avg_sz:
            ui.label(f"Avg size: {avg_sz:.0f} B").classes("text-body2")
        n_col = len(pkt.get("collisions", []))
        if n_col:
            ui.label(f"Collisions: {n_col}").classes("text-body2").style(
                f"color: {COLLISION_COLOUR}"
            )

        # Hop chain table
        steps = broadcast_steps(pkt)
        if steps:
            ui.label("Hop chain").classes("text-caption text-grey q-mt-sm")
            hop_rows = []
            for step_i, step in enumerate(steps):
                for hop in step:
                    rt = _ROUTE_NAMES.get(hop.get("route_type", -1), "?")
                    dt = hop["t"] - pkt["first_seen_at"]
                    hop_rows.append({
                        "id": len(hop_rows),
                        "step": step_i + 1,
                        "tx": short_name(hop["sender"]),
                        "rx": short_name(hop["receiver"]),
                        "route": rt,
                        "dt": f"+{dt:.3f}s",
                    })
            ui.table(
                columns=[
                    {"name": "step", "label": "#", "field": "step", "align": "left"},
                    {"name": "tx", "label": "TX", "field": "tx", "align": "left"},
                    {"name": "rx", "label": "RX", "field": "rx", "align": "left"},
                    {"name": "route", "label": "Route", "field": "route", "align": "left"},
                    {"name": "dt", "label": "dt", "field": "dt", "align": "right"},
                ],
                rows=hop_rows,
                row_key="id",
            ).classes("w-full").style("font-size: 0.7em")

        # Collision list
        collisions = pkt.get("collisions", [])
        if collisions:
            ui.label("Collisions").classes(
                "text-caption text-grey q-mt-sm"
            ).style(f"color: {COLLISION_COLOUR}")
            for col in collisions:
                dt = col["t"] - pkt["first_seen_at"]
                ui.label(
                    f"{short_name(col['sender'])} \u2192 "
                    f"{short_name(col['receiver'])}  +{dt:.3f}s"
                ).classes("text-body2").style(f"color: {COLLISION_COLOUR}")

        # Clickable receiver list
        receivers = pkt.get("unique_receivers", [])
        if receivers:
            ui.label("Receivers").classes("text-caption text-grey q-mt-sm")
            with ui.row().classes("gap-1 flex-wrap"):
                for r in receivers:
                    ui.button(
                        short_name(r),
                        on_click=lambda _, n=r: _select_node(state, n),
                    ).props("flat dense size=sm").classes("text-caption")


def _render_node_detail(state: AppState, name: str) -> None:
    """Render node detail panel."""
    topo = state.topology
    node_map = {n.name: n for n in topo.nodes} if topo else {}
    node = node_map.get(name)
    nstats = (state._node_trace_stats or {}).get(name, {})

    with ui.card().classes("w-full relative"):
        _detail_close_btn(state)
        ui.label(f"Node: {short_name(name)}").classes("text-subtitle2")

        if node:
            role = node_role(node)
            color = ROLE_COLOUR.get(role, ROLE_COLOUR["endpoint"])
            with ui.row().classes("gap-2 items-center"):
                _legend_dot(color)
                ui.label(role.replace("_", " ")).classes("text-body2")

        ui.label(f"TX: {nstats.get('tx_count', 0)}").classes("text-body2")
        ui.label(f"RX: {nstats.get('rx_count', 0)}").classes("text-body2")
        col_count = nstats.get("collisions_involved", 0)
        if col_count:
            ui.label(f"Collisions: {col_count}").classes("text-body2").style(
                f"color: {COLLISION_COLOUR}"
            )

        # Originated packets
        originated = nstats.get("packets_originated", [])
        if originated:
            ui.label(f"Originated ({len(originated)})").classes(
                "text-caption text-grey q-mt-sm"
            )
            with ui.row().classes("gap-1 flex-wrap"):
                for idx in originated[:20]:
                    ui.button(
                        f"#{idx + 1}",
                        on_click=lambda _, i=idx: _select_packet(state, i),
                    ).props("flat dense size=sm").classes("text-caption")
                if len(originated) > 20:
                    ui.label(f"+{len(originated) - 20} more").classes(
                        "text-caption text-grey"
                    )

        # Transited packets
        transited = nstats.get("packets_transited", [])
        if transited:
            ui.label(f"Transited ({len(transited)})").classes(
                "text-caption text-grey q-mt-sm"
            )
            with ui.row().classes("gap-1 flex-wrap"):
                for idx in transited[:20]:
                    ui.button(
                        f"#{idx + 1}",
                        on_click=lambda _, i=idx: _select_packet(state, i),
                    ).props("flat dense size=sm").classes("text-caption")
                if len(transited) > 20:
                    ui.label(f"+{len(transited) - 20} more").classes(
                        "text-caption text-grey"
                    )


def _render_edge_detail(state: AppState, key: tuple) -> None:
    """Render edge detail panel."""
    a, b = key
    topo = state.topology

    with ui.card().classes("w-full relative"):
        _detail_close_btn(state)
        ui.label(f"Edge: {short_name(a)} \u2194 {short_name(b)}").classes(
            "text-subtitle2"
        )

        # Topology properties
        if topo:
            for edge in topo.edges:
                ek = (edge.a, edge.b) if edge.a <= edge.b else (edge.b, edge.a)
                if ek == key:
                    loss_pct = f"{edge.loss * 100:.1f}%" if edge.loss > 0 else "0%"
                    ui.label(f"Loss: {loss_pct}").classes("text-body2")
                    ui.label(f"Latency: {edge.latency_ms:.1f} ms").classes("text-body2")
                    ui.label(f"SNR: {edge.snr:.1f} dB").classes("text-body2")
                    ui.label(f"RSSI: {edge.rssi:.1f} dBm").classes("text-body2")
                    break

        # Trace stats
        estats = (state._edge_trace_stats or {}).get(key, {})
        if estats:
            ui.separator()
            ui.label(f"Hops: {estats.get('hop_count', 0)}").classes("text-body2")
            col_count = estats.get("collision_count", 0)
            if col_count:
                ui.label(f"Collisions: {col_count}").classes("text-body2").style(
                    f"color: {COLLISION_COLOUR}"
                )
            pkts = estats.get("packets", [])
            if pkts:
                ui.label(f"Packets ({len(pkts)})").classes(
                    "text-caption text-grey q-mt-sm"
                )
                with ui.row().classes("gap-1 flex-wrap"):
                    for idx in pkts[:20]:
                        ui.button(
                            f"#{idx + 1}",
                            on_click=lambda _, i=idx: _select_packet(state, i),
                        ).props("flat dense size=sm").classes("text-caption")
                    if len(pkts) > 20:
                        ui.label(f"+{len(pkts) - 20} more").classes(
                            "text-caption text-grey"
                        )


# ---------------------------------------------------------------------------
# Run selector
# ---------------------------------------------------------------------------

def _render_run_selector(state: AppState) -> None:
    """Dropdown to pick a saved simulation run for the current topology."""
    runs = state._available_runs
    has_trace = state.trace is not None

    # Filter to runs matching the loaded topology
    topo_stem = None
    if state.topology_path:
        from pathlib import Path
        topo_stem = Path(state.topology_path).stem
    if topo_stem:
        runs = [r for r in runs if r["topology_name"] == topo_stem]

    if not runs and not has_trace:
        return  # nothing to show

    # Build options dict: {run_dir: label}
    options: dict[str, str] = {}

    # If a trace was loaded that isn't from a saved run, add a CLI entry
    if has_trace and state._current_run_dir is None:
        options["__cli__"] = "(CLI) loaded trace"

    for r in runs:
        # Show shorter label since all are same topology
        options[r["run_dir"]] = r["label"]

    if not options:
        return

    # Determine current value
    current = state._current_run_dir
    if current is None and has_trace:
        current = "__cli__"

    def _on_change(e):
        val = e.value
        if val == "__cli__":
            return  # already showing CLI trace
        if val and val != state._current_run_dir:
            _load_saved_run(state, val)

    ui.select(
        options=options,
        value=current,
        label="Simulation run",
        on_change=_on_change,
    ).classes("w-full")


def _load_saved_run(state: AppState, run_dir: str) -> None:
    """Load a saved run's trace and refresh the viewer."""
    trace = _results.load_run(run_dir)
    if trace is None:
        ui.notify("Failed to load trace from run", type="negative")
        return

    state.trace = trace
    state._current_run_dir = run_dir

    if state._refresh_trace_main:
        state._refresh_trace_main()
    if state._refresh_trace_sidebar:
        state._refresh_trace_sidebar()


# ---------------------------------------------------------------------------
# Sidebar content
# ---------------------------------------------------------------------------

def _legend_dot(color: str, size: int = 10) -> None:
    """Render a small coloured circle inline."""
    ui.element("span").style(
        f"width:{size}px;height:{size}px;border-radius:50%;"
        f"background:{color};display:inline-block;flex-shrink:0;"
        f"border:1.5px solid rgba(255,255,255,0.8);"
        f"box-shadow:0 0 2px rgba(0,0,0,0.3)"
    )


def _legend_line(color: str, dashed: bool = False) -> None:
    """Render a short line segment inline."""
    style = f"dashed" if dashed else "solid"
    ui.element("span").style(
        f"width:20px;height:0;border-top:3px {style} {color};"
        f"display:inline-block;flex-shrink:0;vertical-align:middle"
    )


def _stat_row(label: str, value: str, color: str | None = None) -> None:
    """Render a compact label: value row."""
    with ui.row().classes("w-full justify-between items-baseline gap-1"):
        ui.label(label).classes("text-body2 text-grey-8")
        lbl = ui.label(value).classes("text-body2 text-weight-medium")
        if color:
            lbl.style(f"color: {color}")


def _render_summary_stats(stats: dict) -> None:
    """Render comprehensive statistics cards from compute_trace_stats()."""
    metrics = stats.get("metrics")
    timing = stats.get("timing")

    # -- Packet overview --
    with ui.card().classes("w-full"):
        ui.label("Packets").classes("text-subtitle2")
        _stat_row("Total", str(stats["n_packets"]))
        _stat_row("Flood-routed", f"{stats['n_flood']} ({stats['flood_pct']:.0f}%)")
        _stat_row("Direct-routed", str(stats.get("n_direct", 0)))
        _stat_row("Total hops", str(stats.get("total_hops", 0)))
        amp = stats.get("flood_amplification", 0)
        if amp > 0:
            _stat_row("Flood amplification", f"{amp:.1f}x")
        _stat_row("Avg witnesses", f"{stats['avg_witnesses']:.1f}")
        if stats["n_collisions"] > 0:
            _stat_row("Collisions", str(stats["n_collisions"]),
                       color=COLLISION_COLOUR)

    # -- Message delivery (from embedded metrics) --
    if metrics:
        delivery = metrics.get("delivery", {})
        attempted = delivery.get("attempted", 0)
        if attempted > 0:
            delivered = delivery.get("delivered", 0)
            rate = delivery.get("rate", 0) * 100
            with ui.card().classes("w-full"):
                ui.label("Message Delivery").classes("text-subtitle2")
                _stat_row("Delivered", f"{delivered}/{attempted}")
                rate_color = "#4caf50" if rate >= 80 else "#ff9800" if rate >= 50 else "#f44336"
                _stat_row("Delivery rate", f"{rate:.1f}%", color=rate_color)

                lat = metrics.get("latency", {})
                if lat:
                    _stat_row("Latency min", f"{lat['min_ms']:.0f} ms")
                    _stat_row("Latency p50", f"{lat['p50_ms']:.0f} ms")
                    _stat_row("Latency avg", f"{lat['avg_ms']:.0f} ms")
                    _stat_row("Latency p95", f"{lat['p95_ms']:.0f} ms")
                    _stat_row("Latency max", f"{lat['max_ms']:.0f} ms")

        # ACK outcomes
        ack = metrics.get("ack_outcomes", {})
        ack_total = sum(ack.get(k, 0) for k in ("confirmed", "retries", "failed"))
        if ack_total > 0:
            with ui.card().classes("w-full"):
                ui.label("ACK Outcomes").classes("text-subtitle2")
                _stat_row("Confirmed", str(ack.get("confirmed", 0)),
                           color="#4caf50")
                retries = ack.get("retries", 0)
                if retries:
                    _stat_row("Retries", str(retries), color="#ff9800")
                failed = ack.get("failed", 0)
                if failed:
                    _stat_row("Failed", str(failed), color="#f44336")

        # Channel messages
        ch = metrics.get("channel_messages", {})
        if ch.get("sent", 0) > 0 or ch.get("recv", 0) > 0:
            with ui.card().classes("w-full"):
                ui.label("Channel Messages").classes("text-subtitle2")
                _stat_row("Sent", str(ch.get("sent", 0)))
                _stat_row("Received", str(ch.get("recv", 0)))

        # Drops breakdown
        drops = metrics.get("drops", {})
        total_drops = sum(drops.values()) if drops else 0
        if total_drops > 0:
            with ui.card().classes("w-full"):
                ui.label("Drops").classes("text-subtitle2")
                for key, label in [
                    ("collisions", "RF collisions"),
                    ("halfduplex", "Half-duplex"),
                    ("link_loss", "Link loss"),
                    ("adversarial_drop", "Adversarial drop"),
                    ("adversarial_corrupt", "Adversarial corrupt"),
                    ("adversarial_replay", "Adversarial replay"),
                ]:
                    val = drops.get(key, 0)
                    if val > 0:
                        _stat_row(label, str(val), color=COLLISION_COLOUR)

        # Contact discovery
        contacts = metrics.get("contacts", {})
        if contacts:
            with ui.card().classes("w-full"):
                ui.label("Contact Discovery").classes("text-subtitle2")
                for name in sorted(contacts):
                    c = contacts[name]
                    disc = c["discovered"]
                    tot = c["total"]
                    pct = disc / tot * 100 if tot else 0
                    pct_color = "#4caf50" if pct >= 80 else "#ff9800" if pct >= 50 else "#f44336"
                    _stat_row(
                        short_name(name),
                        f"{disc}/{tot} ({pct:.0f}%)",
                        color=pct_color,
                    )

    # -- Timing (from embedded timing stats) --
    if timing:
        with ui.card().classes("w-full"):
            ui.label("Timing").classes("text-subtitle2")
            if "avg_airtime_ms" in timing:
                _stat_row("Avg airtime/hop",
                           f"{timing['avg_airtime_ms']:.0f} ms")
            if "relay_delay_avg_ms" in timing:
                _stat_row("Relay delay",
                           f"{timing['relay_delay_min_ms']:.0f} / "
                           f"{timing['relay_delay_avg_ms']:.0f} / "
                           f"{timing['relay_delay_max_ms']:.0f} ms")
            if "flood_prop_avg_ms" in timing:
                _stat_row("Flood propagation",
                           f"{timing['flood_prop_min_ms']:.0f} / "
                           f"{timing['flood_prop_avg_ms']:.0f} / "
                           f"{timing['flood_prop_max_ms']:.0f} ms")
            if "channel_utilization_pct" in timing:
                _stat_row("Channel utilization",
                           f"{timing['channel_utilization_pct']:.1f}%")

    # -- Per-type breakdown --
    by_type = stats.get("by_type", {})
    if len(by_type) > 1:
        with ui.card().classes("w-full"):
            ui.label("By Type").classes("text-subtitle2")
            rows = []
            for tname, info in sorted(by_type.items()):
                rows.append({
                    "type": tname[:12],
                    "count": info["count"],
                    "avg_w": f"{info['avg_witnesses']:.1f}",
                    "col": info["collisions"],
                })
            ui.table(
                columns=[
                    {"name": "type", "label": "Type", "field": "type",
                     "align": "left"},
                    {"name": "count", "label": "#", "field": "count",
                     "align": "right"},
                    {"name": "avg_w", "label": "Avg W", "field": "avg_w",
                     "align": "right"},
                    {"name": "col", "label": "Col", "field": "col",
                     "align": "right"},
                ],
                rows=rows,
                row_key="type",
            ).classes("w-full").style("font-size: 0.7em")


def _sidebar_content(state: AppState) -> None:
    ui.label("Trace Viewer").classes("text-h6")

    # -- Run selector dropdown --
    @ui.refreshable
    def run_selector():
        _render_run_selector(state)
    run_selector()
    state._refresh_run_selector = run_selector.refresh

    if state.trace is None:
        if not state._available_runs:
            ui.label("No trace loaded.").classes("text-grey")
            ui.label("Run a simulation first.").classes(
                "text-caption text-grey"
            )
        else:
            ui.label("Select a run above to view its trace.").classes(
                "text-caption text-grey"
            )
        return

    trace = state.trace
    packets = trace.get("packets", [])
    stats = compute_trace_stats(trace)

    # -- Summary stats --
    _render_summary_stats(stats)

    # -- Legend --
    ui.separator()
    ui.label("Legend").classes("text-subtitle2")

    # Node roles (base colors)
    with ui.column().classes("gap-1"):
        ui.label("Node roles").classes("text-caption text-grey")
        for role, color in ROLE_COLOUR.items():
            with ui.row().classes("gap-2 items-center"):
                _legend_dot(color)
                ui.label(role.replace("_", " ")).classes("text-caption")

    ui.space().style("height: 6px")

    # Event states (override colors)
    with ui.column().classes("gap-1"):
        ui.label("During events").classes("text-caption text-grey")
        for label, color, size, desc in [
            ("Transmitting", SENDER_COLOUR, 14, "node is sending"),
            ("Receiving", RECEIVER_COLOUR, 12, "node is receiving"),
            ("Witnessed", RECEIVED_COLOUR, 10, "received before t"),
        ]:
            with ui.row().classes("gap-2 items-center"):
                _legend_dot(color, size)
                ui.label(f"{label}").classes("text-caption")
                ui.label(f"({desc})").classes("text-caption text-grey-6")

    ui.space().style("height: 6px")

    # Edge styles
    with ui.column().classes("gap-1"):
        ui.label("Edges").classes("text-caption text-grey")
        with ui.row().classes("gap-2 items-center"):
            _legend_line(SENDER_COLOUR)
            ui.label("Active TX flow").classes("text-caption")
        with ui.row().classes("gap-2 items-center"):
            _legend_line(COLLISION_COLOUR, dashed=True)
            ui.label("Collision").classes("text-caption")

    if not packets:
        return

    # -- Detail panel (Phase 5) --
    ui.separator()

    @ui.refreshable
    def detail_panel():
        _render_detail_panel(state)

    detail_panel()
    state._refresh_detail_panel = detail_panel.refresh

    # -- Type filter --
    unique_types = sorted({p["payload_type_name"] for p in packets})
    if len(unique_types) > 1:
        ui.separator()
        state.type_filter = set(unique_types)
        type_select = ui.select(
            options=unique_types,
            value=unique_types,
            multiple=True,
            label="Filter types",
        ).classes("w-full")

        def _on_type_change(e):
            state.type_filter = set(e.value) if e.value else set(unique_types)
            _update_map_overlay(state)

        type_select.on_value_change(_on_type_change)

    # -- Packet table --
    ui.separator()
    ui.label("Packets").classes("text-subtitle2")

    t_offset = packets[0]["first_seen_at"] if packets else 0.0

    columns = [
        {"name": "idx", "label": "#", "field": "idx",
         "sortable": True, "align": "left"},
        {"name": "type", "label": "Type", "field": "type",
         "sortable": True, "align": "left"},
        {"name": "sender", "label": "Sender", "field": "sender",
         "sortable": True, "align": "left"},
        {"name": "time", "label": "Time", "field": "time",
         "sortable": True, "align": "right"},
        {"name": "w", "label": "W", "field": "w",
         "sortable": True, "align": "right"},
    ]
    rows = [
        {
            "idx": i + 1,
            "type": p["payload_type_name"][:8],
            "sender": short_name(p["first_sender"]),
            "time": f"{p['first_seen_at'] - t_offset:.1f}",
            "w": p["witness_count"],
        }
        for i, p in enumerate(packets)
    ]

    table = ui.table(
        columns=columns,
        rows=rows,
        row_key="idx",
        pagination={"rowsPerPage": 15},
    ).classes("w-full").style("font-size: 0.75em")

    def _on_row_click(e):
        row = e.args[1]
        pkt_idx = row.get("idx", 0) - 1
        _select_packet(state, pkt_idx)

    table.on("row-click", _on_row_click)


# ---------------------------------------------------------------------------
# Main content: map + timeline
# ---------------------------------------------------------------------------

def _main_content(state: AppState) -> None:
    if state.trace is None:
        with ui.column().classes("w-full h-full items-center justify-center"):
            ui.icon("timeline").classes("text-6xl text-grey-4")
            ui.label("No trace loaded").classes("text-grey")
        return

    if state.topology is None:
        with ui.column().classes("w-full h-full items-center justify-center"):
            ui.label("No topology loaded").classes("text-grey")
        return

    # Build event timeline
    state.event_timeline = flatten_events(state.trace)
    if not state.event_timeline:
        with ui.column().classes("w-full h-full items-center justify-center"):
            ui.label("No events in trace").classes("text-grey")
        return

    t_min = state.event_timeline[0]["t"]
    t_max = state.event_timeline[-1]["t"]
    t_span = max(t_max - t_min, 0.1)
    state.current_time = t_min
    state._selected_pkt_idx = None
    state._selected_node_name = None
    state._selected_edge_key = None

    # Pre-compute per-node and per-edge trace stats
    state._node_trace_stats = compute_node_trace_stats(state.trace)
    state._edge_trace_stats = compute_edge_trace_stats(state.trace)

    # Pre-compute sorted times for bisect lookups
    event_times = [e["t"] for e in state.event_timeline]

    # -- Layout: absolute-positioned flex column with explicit viewport height --
    # Uses calc(100vh - 75px) to match _CONTENT_H from app.py, bypassing
    # the @ui.refreshable wrapper that breaks CSS height chains.
    with ui.column().classes("w-full gap-0").style(
        "position: absolute; top: 0; left: 0; right: 0;"
        "height: calc(100vh - 75px); overflow: hidden"
    ):
        # Map area (fills remaining space)
        with ui.element("div").classes("w-full").style(
            "flex: 1; min-height: 0"
        ):
            map_data = _build_trace_map(state)

        # Event info bar (shows what's happening / selected packet)
        event_label = ui.label("").classes("text-caption px-4").style(
            "height: 28px; line-height: 28px; flex-shrink: 0;"
            "background: #fff; border-top: 1px solid #e9ecef;"
            "color: #495057; font-family: monospace; font-size: 0.8em;"
            "white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
        )

        # Timeline bar
        with ui.row().classes("w-full items-center gap-2 px-4").style(
            "height: 52px; flex-shrink: 0;"
            "background: #f8f9fa; border-top: 1px solid #dee2e6"
        ):
            play_btn = ui.button(
                icon="play_arrow",
                on_click=lambda: _toggle_play(state),
            ).props("flat dense round")

            ui.select(
                options={
                    0.25: "0.25x", 0.5: "0.5x", 1.0: "1x",
                    2.0: "2x", 5.0: "5x", 10.0: "10x",
                },
                value=1.0,
            ).classes("w-20").props("dense").on_value_change(
                lambda e: setattr(state, "play_speed", float(e.value))
            )

            slider = ui.slider(
                min=t_min,
                max=t_max,
                value=t_min,
                step=max(t_span / 2000, 0.001),
            ).style("flex: 1; min-width: 100px")

            time_label = ui.label("t=0.0s").classes("text-caption").style(
                "min-width: 80px; text-align: right"
            )

    # Store UI refs
    tui = SimpleNamespace(
        map=map_data["map"],
        map_id=map_data["map"].id,
        marker_ids=map_data["marker_ids"],
        positions=map_data["positions"],
        event_times=event_times,
        t_min=t_min,
        t_max=t_max,
        slider=slider,
        time_label=time_label,
        event_label=event_label,
        play_btn=play_btn,
        timer=None,
    )
    state._trace_ui = tui

    # Playback timer (initially paused)
    tui.timer = ui.timer(0.05, lambda: _advance_time(state), active=False)

    # Slider change -> update overlay
    slider.on_value_change(lambda e: _on_slider_change(state, float(e.value)))

    # Trigger initial overlay after map init
    map_data["map"].on("init", lambda _: _update_map_overlay(state))


# ---------------------------------------------------------------------------
# Trace map: circle markers + edge polylines
# ---------------------------------------------------------------------------

def _build_trace_map(state: AppState) -> dict:
    """Create a Leaflet map with circle markers for trace overlay.

    Returns {"map": ui.leaflet, "marker_ids": {name: layer_id}, "positions": dict}.
    """
    topo = state.topology
    positions = node_positions(topo)
    center, zoom = compute_center_zoom(positions)

    m = ui.leaflet(center=center, zoom=zoom).classes("w-full h-full")

    marker_ids: dict[str, str] = {}

    with m:
        # Edges (base topology)
        for edge in topo.edges:
            pos_a = positions.get(edge.a)
            pos_b = positions.get(edge.b)
            if pos_a is None or pos_b is None:
                continue
            m.generic_layer(
                name="polyline",
                args=[
                    [list(pos_a), list(pos_b)],
                    {"color": EDGE_COLOUR, "weight": 2, "opacity": 0.5},
                ],
            )

        # Circle markers for nodes
        for node in topo.nodes:
            pos = positions.get(node.name)
            if pos is None:
                continue
            role = node_role(node)
            color = ROLE_COLOUR.get(role, ROLE_COLOUR["endpoint"])
            layer = m.generic_layer(
                name="circleMarker",
                args=[
                    list(pos),
                    {
                        "radius": 8,
                        "color": color,
                        "fillColor": color,
                        "fillOpacity": 0.8,
                        "weight": 2,
                    },
                ],
            )
            marker_ids[node.name] = layer.id

    # Bind rich tooltips via JS
    _bind_tooltips(m, topo, marker_ids)

    # Bind marker click → Python (Phase 5)
    _bind_marker_clicks(m, marker_ids, state)

    return {"map": m, "marker_ids": marker_ids, "positions": positions}


def _bind_tooltips(
    leaflet_map: ui.leaflet,
    topo,
    marker_ids: dict[str, str],
) -> None:
    """Bind node-name + role tooltips to circle markers."""
    tooltip_data = {}
    for node in topo.nodes:
        lid = marker_ids.get(node.name)
        if lid is None:
            continue
        role = node_role(node)
        tooltip_data[lid] = (
            f"<b>{short_name(node.name)}</b><br>"
            f"<span style='color:#888'>{role}</span>"
        )

    data_json = _json.dumps(tooltip_data)
    element_id = leaflet_map.id

    js = f"""
        (function() {{
            var data = {data_json};
            var comp = getElement({element_id});
            if (!comp || !comp.map) return;
            comp.map.eachLayer(function(layer) {{
                if (!layer.id) return;
                var tip = data[layer.id];
                if (tip) layer.bindTooltip(tip);
            }});
        }})();
    """

    def _on_init(_):
        leaflet_map.client.run_javascript(js)

    leaflet_map.on("init", _on_init)


def _bind_marker_clicks(
    leaflet_map: ui.leaflet,
    marker_ids: dict[str, str],
    state: AppState,
) -> None:
    """Bind click handlers on circleMarkers that emit 'marker-click' to Python."""
    # Build {layer_id: node_name} for JS
    id_to_name = {lid: name for name, lid in marker_ids.items()}
    data_json = _json.dumps(id_to_name)
    element_id = leaflet_map.id

    js = f"""
        (function() {{
            var idToName = {data_json};
            var comp = getElement({element_id});
            if (!comp || !comp.map) return;
            comp.map.eachLayer(function(layer) {{
                if (!layer.id) return;
                var name = idToName[layer.id];
                if (name) {{
                    layer.on('click', function(e) {{
                        L.DomEvent.stopPropagation(e);
                        comp.$emit('marker-click', {{name: name}});
                    }});
                }}
            }});
        }})();
    """

    def _on_init(_):
        leaflet_map.client.run_javascript(js)

    leaflet_map.on("init", _on_init)

    # Python handler
    def _on_marker_click(e):
        name = e.args.get("name") if isinstance(e.args, dict) else None
        if name:
            _select_node(state, name)

    leaflet_map.on("marker-click", _on_marker_click)


# ---------------------------------------------------------------------------
# Playback controls
# ---------------------------------------------------------------------------

def _toggle_play(state: AppState) -> None:
    tui = getattr(state, "_trace_ui", None)
    if tui is None:
        return
    state.playing = not state.playing
    tui.timer.active = state.playing
    tui.play_btn.props(
        f'icon={"pause" if state.playing else "play_arrow"}'
    )


def _advance_time(state: AppState) -> None:
    """Timer callback: advance current time and update slider."""
    tui = getattr(state, "_trace_ui", None)
    if tui is None:
        return

    dt = 0.05 * state.play_speed
    state.current_time += dt

    if state.current_time > tui.t_max:
        state.current_time = tui.t_min  # loop

    # Updating slider triggers on_value_change -> overlay update
    tui.slider.value = state.current_time


def _on_slider_change(state: AppState, t: float) -> None:
    """Handle slider value change (user drag or timer advance)."""
    state.current_time = t
    # Clear selections when playing (keep them when jumping via row click)
    if state.playing:
        state._selected_pkt_idx = None
        state._selected_node_name = None
        state._selected_edge_key = None
    _update_time_label(state)
    _update_map_overlay(state)


def _update_time_label(state: AppState) -> None:
    tui = getattr(state, "_trace_ui", None)
    if tui is None:
        return
    rel_t = state.current_time - tui.t_min
    tui.time_label.set_text(f"t={rel_t:.1f}s")


# ---------------------------------------------------------------------------
# Map overlay update
# ---------------------------------------------------------------------------

def _update_map_overlay(state: AppState) -> None:
    """Update circle marker styles, flow edges, collision overlays, event info."""
    tui = getattr(state, "_trace_ui", None)
    if tui is None:
        return

    t = state.current_time
    events = state.event_timeline
    event_times = tui.event_times
    packets = state.trace.get("packets", [])

    # Find active events in [t - window, t + window]
    left = bisect.bisect_left(event_times, t - WINDOW_SECS)
    right = bisect.bisect_right(event_times, t + WINDOW_SECS)
    active_raw = events[left:right]

    # Apply type filter
    allowed = state.type_filter
    if allowed is not None:
        active = [
            e for e in active_raw
            if packets[e["pkt_idx"]]["payload_type_name"] in allowed
        ]
    else:
        active = active_raw

    # --- Compute accumulated witnesses (all receivers with t <= current) ---
    # Use bisect: events[0:right] are all events up to t + window.
    # Filter to hops with t <= current_time.
    witnessed: set[str] = set()
    for ev in events[:right]:
        if ev["t"] > t:
            break
        if ev["type"] == "hop":
            if allowed is None or packets[ev["pkt_idx"]]["payload_type_name"] in allowed:
                witnessed.add(ev["receiver"])

    # --- Classify currently active nodes ---
    senders: set[str] = set()
    receivers: set[str] = set()
    collision_pairs: list[tuple[str, str]] = []
    flow_pairs: list[tuple[str, str]] = []
    active_descriptions: list[str] = []

    for ev in active:
        if ev["type"] == "hop":
            senders.add(ev["sender"])
            receivers.add(ev["receiver"])
            flow_pairs.append((ev["sender"], ev["receiver"]))
            pkt_type = packets[ev["pkt_idx"]]["payload_type_name"]
            desc = f"{short_name(ev['sender'])}\u2192{short_name(ev['receiver'])} [{pkt_type}]"
            if desc not in active_descriptions:
                active_descriptions.append(desc)
        elif ev["type"] == "collision":
            collision_pairs.append((ev["sender"], ev["receiver"]))
            desc = f"{short_name(ev['sender'])}\u2192{short_name(ev['receiver'])} [COLLISION]"
            if desc not in active_descriptions:
                active_descriptions.append(desc)

    # --- Update event info label ---
    # Priority: selected packet > active events > idle status
    sel_idx = getattr(state, "_selected_pkt_idx", None)
    _BAR_BASE = (
        "height: 28px; line-height: 28px; flex-shrink: 0;"
        "border-top: 1px solid #e9ecef; font-family: monospace;"
        "font-size: 0.8em; white-space: nowrap; overflow: hidden;"
        "text-overflow: ellipsis"
    )

    if sel_idx is not None and 0 <= sel_idx < len(packets):
        pkt = packets[sel_idx]
        pkt_t = pkt["first_seen_at"]
        rel_t = pkt_t - tui.t_min
        route = "flood" if pkt.get("is_flood") else "direct"
        text = (
            f"Packet #{sel_idx+1}  |  {pkt['payload_type_name']}  |  "
            f"from {short_name(pkt['first_sender'])}  |  "
            f"t={rel_t:.3f}s  |  {route}  |  "
            f"{pkt.get('witness_count', 0)} witnesses"
        )
        tui.event_label.set_text(text)
        tui.event_label.style(
            f"{_BAR_BASE} background: #e3f2fd; color: #1565c0"
        )
    elif active_descriptions:
        tui.event_label.set_text(
            f"{len(active)} active: " + "  |  ".join(active_descriptions[:4])
        )
        tui.event_label.style(
            f"{_BAR_BASE} background: #fff8e1; color: #495057"
        )
    else:
        tui.event_label.set_text(
            f"No activity  ({len(witnessed)} nodes witnessed so far)"
        )
        tui.event_label.style(
            f"{_BAR_BASE} background: #fff; color: #adb5bd"
        )

    # --- Build per-layer style dict ---
    topo = state.topology
    node_map = {n.name: n for n in topo.nodes}
    positions = tui.positions
    selected_node = state._selected_node_name

    styles: dict = {}
    for name, lid in tui.marker_ids.items():
        node = node_map.get(name)
        if node is None:
            continue

        if name in senders:
            color = SENDER_COLOUR
            radius = 14
            opacity = 0.95
        elif name in receivers:
            color = RECEIVER_COLOUR
            radius = 11
            opacity = 0.9
        elif name in witnessed:
            color = RECEIVED_COLOUR
            radius = 9
            opacity = 0.85
        else:
            role = node_role(node)
            color = ROLE_COLOUR.get(role, ROLE_COLOUR["endpoint"])
            radius = 8
            opacity = 0.8

        # Selection highlight: white border ring + larger radius
        border_color = color
        weight = 2
        if name == selected_node:
            border_color = _SELECTED_BORDER
            radius = max(radius, 12)
            weight = 4

        styles[lid] = {
            "color": border_color,
            "fillColor": color,
            "fillOpacity": opacity,
            "radius": radius,
            "weight": weight,
        }

    # --- Build flow edge coordinates (sender -> receiver) ---
    flow_coords = []
    seen_flows: set[tuple[str, str]] = set()
    for sender, receiver in flow_pairs:
        key = (sender, receiver)
        if key in seen_flows:
            continue
        seen_flows.add(key)
        pos_s = positions.get(sender)
        pos_r = positions.get(receiver)
        if pos_s and pos_r:
            flow_coords.append([list(pos_s), list(pos_r)])

    # --- Build collision polyline coordinates ---
    collision_coords = []
    for sender, receiver in collision_pairs:
        pos_s = positions.get(sender)
        pos_r = positions.get(receiver)
        if pos_s and pos_r:
            collision_coords.append([list(pos_s), list(pos_r)])

    # --- Single JS call to update everything ---
    styles_json = _json.dumps(styles)
    flow_json = _json.dumps(flow_coords)
    col_json = _json.dumps(collision_coords)
    map_id = tui.map_id

    js = f"""
        (function() {{
            var comp = getElement({map_id});
            if (!comp || !comp.map) return;
            var map = comp.map;

            // Update circle marker styles
            var styles = {styles_json};
            map.eachLayer(function(layer) {{
                if (!layer.id) return;
                var s = styles[layer.id];
                if (s && layer.setStyle) {{
                    layer.setStyle({{
                        color: s.color,
                        fillColor: s.fillColor,
                        fillOpacity: s.fillOpacity,
                        weight: s.weight || 2
                    }});
                    if (s.radius && layer.setRadius) layer.setRadius(s.radius);
                }}
            }});

            // Flow edges (orange TX direction lines)
            if (window._wb_flow) {{
                window._wb_flow.forEach(function(l) {{ map.removeLayer(l); }});
            }}
            window._wb_flow = [];
            var flows = {flow_json};
            for (var i = 0; i < flows.length; i++) {{
                var pl = L.polyline(flows[i], {{
                    color: '{SENDER_COLOUR}',
                    weight: 4,
                    opacity: 0.7,
                    dashArray: '8,6'
                }});
                pl.addTo(map);
                window._wb_flow.push(pl);
            }}

            // Collision edges (red dashed)
            if (window._wb_col) {{
                window._wb_col.forEach(function(l) {{ map.removeLayer(l); }});
            }}
            window._wb_col = [];
            var cols = {col_json};
            for (var i = 0; i < cols.length; i++) {{
                var pl = L.polyline(cols[i], {{
                    color: '{COLLISION_COLOUR}',
                    weight: 4,
                    dashArray: '4,4',
                    opacity: 0.9
                }});
                pl.addTo(map);
                window._wb_col.push(pl);
            }}
        }})();
    """

    tui.map.client.run_javascript(js)
