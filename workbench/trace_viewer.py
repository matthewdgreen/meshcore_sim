"""trace_viewer.py — Tab 3: Trace timeline with continuous time scrubbing."""

from __future__ import annotations

import bisect
import json as _json
from types import SimpleNamespace

from nicegui import ui

from .map_helpers import (
    ROLE_COLOUR, EDGE_COLOUR, SENDER_COLOUR, RECEIVER_COLOUR,
    RECEIVED_COLOUR, COLLISION_COLOUR, HALFDUPLEX_COLOUR,
    short_name, medium_name, node_role, node_positions, compute_center_zoom,
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


def _jump_to_tx_id(state: AppState, target_tx_id: int) -> None:
    """Find the packet containing a hop or collision with this tx_id and select it."""
    packets = state.trace.get("packets", []) if state.trace else []
    for pkt_idx, pkt in enumerate(packets):
        for hop in pkt.get("hops", []):
            if hop.get("tx_id") == target_tx_id:
                _select_packet(state, pkt_idx)
                return
        for col in pkt.get("collisions", []):
            if col.get("tx_id") == target_tx_id:
                _select_packet(state, pkt_idx)
                return


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
        n_hd = len(pkt.get("halfduplex", []))
        if n_hd:
            ui.label(f"Half-duplex drops: {n_hd}").classes("text-body2").style(
                f"color: {HALFDUPLEX_COLOUR}"
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
                interferer = col.get("interferer")
                overlap_s = col.get("overlap_s", 0)
                int_tx_id = col.get("interferer_tx_id")

                label_parts = [
                    f"{short_name(col['sender'])} \u2192 "
                    f"{short_name(col['receiver'])}  +{dt:.3f}s"
                ]
                if interferer:
                    overlap_ms = overlap_s * 1000
                    label_parts.append(
                        f"  [by {short_name(interferer)}, {overlap_ms:.0f}ms overlap]"
                    )

                with ui.row().classes("items-center gap-0"):
                    ui.label("".join(label_parts)).classes(
                        "text-body2"
                    ).style(f"color: {COLLISION_COLOUR}")
                    if int_tx_id is not None:
                        ui.button(
                            icon="arrow_forward",
                            on_click=lambda _, tid=int_tx_id: _jump_to_tx_id(state, tid),
                        ).props("flat dense round size=xs").tooltip(
                            "Jump to interferer packet"
                        )

        # Half-duplex list
        halfduplex_list = pkt.get("halfduplex", [])
        if halfduplex_list:
            ui.label("Half-duplex (receiver busy)").classes(
                "text-caption text-grey q-mt-sm"
            ).style(f"color: {HALFDUPLEX_COLOUR}")
            for hd in halfduplex_list:
                dt = hd["t"] - pkt["first_seen_at"]
                blocker_tid = hd.get("blocker_tx_id")

                label_parts = [
                    f"{short_name(hd['sender'])} \u2192 "
                    f"{short_name(hd['receiver'])}  +{dt:.3f}s"
                ]
                if blocker_tid is not None:
                    label_parts.append(f"  [blocked by tx#{blocker_tid}]")

                with ui.row().classes("items-center gap-0"):
                    ui.label("".join(label_parts)).classes(
                        "text-body2"
                    ).style(f"color: {HALFDUPLEX_COLOUR}")
                    if blocker_tid is not None:
                        ui.button(
                            icon="arrow_forward",
                            on_click=lambda _, tid=blocker_tid: _jump_to_tx_id(state, tid),
                        ).props("flat dense round size=xs").tooltip(
                            "Jump to blocking TX"
                        )

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
        hd_count = nstats.get("halfduplex_involved", 0)
        if hd_count:
            ui.label(f"Half-duplex drops: {hd_count}").classes("text-body2").style(
                f"color: {HALFDUPLEX_COLOUR}"
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
            hd_count = estats.get("halfduplex_count", 0)
            if hd_count:
                ui.label(f"Half-duplex drops: {hd_count}").classes("text-body2").style(
                    f"color: {HALFDUPLEX_COLOUR}"
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
        if stats.get("n_halfduplex", 0) > 0:
            _stat_row("Half-duplex", str(stats["n_halfduplex"]),
                       color=HALFDUPLEX_COLOUR)

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
                    ("snr_below_threshold", "SNR below threshold"),
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
        with ui.row().classes("gap-2 items-center"):
            _legend_line(HALFDUPLEX_COLOUR, dashed=True)
            ui.label("Half-duplex drop").classes("text-caption")

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

        # TX waterfall panel (collapsible)
        with ui.element("div").classes("w-full").style(
            "height: 0px; flex-shrink: 0; overflow: hidden;"
            "border-top: 1px solid #dee2e6; background: #fafafa;"
            "transition: height 0.2s ease"
        ) as waterfall_wrapper:
            waterfall_html = ui.html("").classes("w-full")

        # Timeline bar
        with ui.row().classes("w-full items-center gap-2 px-4").style(
            "height: 52px; flex-shrink: 0;"
            "background: #f8f9fa; border-top: 1px solid #dee2e6"
        ):
            play_btn = ui.button(
                icon="play_arrow",
                on_click=lambda: _toggle_play(state),
            ).props("flat dense round")

            waterfall_btn = ui.button(
                icon="waterfall_chart",
                on_click=lambda: _toggle_waterfall(state),
            ).props("flat dense round").tooltip("Toggle TX waterfall")

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
        waterfall_btn=waterfall_btn,
        waterfall_wrapper=waterfall_wrapper,
        waterfall_html=waterfall_html,
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


def _toggle_waterfall(state: AppState) -> None:
    """Toggle the TX waterfall panel visibility."""
    tui = getattr(state, "_trace_ui", None)
    if tui is None:
        return
    state._waterfall_visible = not state._waterfall_visible
    if state._waterfall_visible:
        # Initial height — will be adjusted by _update_waterfall() on each render.
        # Estimate: each receiver gets a header (18px) + ~2 sender rows (20px each).
        topo = state.topology
        n_nodes = len(topo.nodes) if topo else 3
        h = max(80, min(n_nodes * 58, 300))
        tui.waterfall_wrapper.style(
            f"height: {h}px; flex-shrink: 0; overflow-y: auto;"
            "border-top: 1px solid #dee2e6; background: #fafafa;"
            "transition: height 0.2s ease"
        )
    else:
        tui.waterfall_wrapper.style(
            "height: 0px; flex-shrink: 0; overflow: hidden;"
            "border-top: 1px solid #dee2e6; background: #fafafa;"
            "transition: height 0.2s ease"
        )
    _update_map_overlay(state)


# Sender colors for waterfall bars
_WF_COLORS = [
    "#1565c0", "#2e7d32", "#6a1b9a", "#e65100",
    "#00838f", "#ad1457", "#4e342e", "#1a237e",
    "#558b2f", "#bf360c", "#0277bd", "#880e4f",
]

# Broadcast step colors (shared between map overlay and waterfall)
_STEP_HUES = [
    "#1565c0", "#2e7d32", "#6a1b9a", "#e65100",
    "#00838f", "#ad1457", "#4e342e", "#1a237e",
]


def _update_waterfall(
    state: AppState,
    waterfall_data: list[dict],
    current_time: float,
    map_id: int = 0,
    positions: dict[str, tuple[float, float]] | None = None,
    tx_id_to_step: dict[int, tuple[int, str]] | None = None,
    rx_outcomes: dict[tuple[int, str], str] | None = None,
    tx_id_to_pkt: dict[int, tuple[int, str]] | None = None,
) -> None:
    """Render TX waterfall bars grouped by receiver into the waterfall html widget.

    Layout:
        ── receiver_A ──────────────────────
          sender_X  [===snr===]     [===snr===]
          sender_Y       [======snr======]
        ── receiver_B ──────────────────────
          sender_X    [====snr====]
          ...

    Each receiver gets a header separator, then one sub-row per sender
    that can reach it, so bars never overlap vertically.
    """
    tui = getattr(state, "_trace_ui", None)
    if tui is None or not state._waterfall_visible:
        return
    if not waterfall_data:
        tui.waterfall_html.set_content(
            '<div style="color:#adb5bd;padding:4px 12px;font-size:0.75em">'
            'No TX events in window</div>'
        )
        return

    # Assign a stable colour per sender
    sender_color: dict[str, str] = {}
    color_idx = 0
    for ev in waterfall_data:
        s = ev["sender"]
        if s not in sender_color:
            sender_color[s] = _WF_COLORS[color_idx % len(_WF_COLORS)]
            color_idx += 1

    # Group events by receiver, then by sender within each receiver.
    # Self-TX events (sender == receiver) go into a separate dict
    # to render on the header row.
    by_rx: dict[str, dict[str, list[dict]]] = {}
    self_tx: dict[str, list[dict]] = {}  # receiver → list of own TX events
    for ev in waterfall_data:
        rx = ev["receiver"]
        sx = ev["sender"]
        if ev.get("self_tx"):
            self_tx.setdefault(rx, []).append(ev)
        else:
            by_rx.setdefault(rx, {}).setdefault(sx, []).append(ev)
    rx_order = sorted(by_rx.keys())

    wf_window = 2.0
    t_lo = current_time - wf_window
    t_hi = current_time + wf_window
    total_w = t_hi - t_lo

    header_h = 18   # receiver separator line height
    row_h = 20      # sender sub-row height
    label_w = 140   # px reserved for sender label (wider for full names)

    # Calculate total height: for each receiver, 1 header + N sender rows
    total_h = 0
    for rx in rx_order:
        total_h += header_h + len(by_rx[rx]) * row_h
    total_h = max(total_h, header_h + row_h)

    parts = [
        f'<div style="position:relative;width:100%;height:{total_h}px;'
        f'font-family:monospace;font-size:0.7em">'
    ]

    # "Now" hairline
    now_pct = (current_time - t_lo) / total_w * 100
    parts.append(
        f'<div style="position:absolute;left:{now_pct:.2f}%;top:0;bottom:0;'
        f'width:1px;background:#f44336;z-index:10"></div>'
    )

    y = 0
    alt = 0  # alternating background toggle
    for rx in rx_order:
        senders = by_rx[rx]
        sender_order = sorted(senders.keys())

        # ── Receiver header separator ──
        parts.append(
            f'<div data-node="{rx}" style="position:absolute;left:0;top:{y}px;right:0;'
            f'height:{header_h}px;background:#e0e0e0;z-index:2;'
            f'display:flex;align-items:center;padding-left:4px;'
            f'font-weight:bold;font-size:0.9em;color:#333;cursor:pointer;'
            f'border-bottom:1px solid #bdbdbd">'
            f'{medium_name(rx)}</div>'
        )
        # Self-TX bars on the header row (this node's own transmissions)
        for ev in self_tx.get(rx, []):
            bar_left = (ev["t_start"] - t_lo) / total_w * 100
            bar_right = (ev["t_end"] - t_lo) / total_w * 100
            bar_w = max(bar_right - bar_left, 0.3)
            tx_id_val = ev["tx_id"]
            pkt_info = tx_id_to_pkt.get(tx_id_val) if tx_id_to_pkt else None
            pkt_num = pkt_info[0] if pkt_info else "?"
            pkt_type = pkt_info[1] if pkt_info else ""
            airtime_ms = (ev["t_end"] - ev["t_start"]) * 1000
            title = (
                f"{medium_name(rx)} TX"
                f" pkt#{pkt_num} {pkt_type} tx#{tx_id_val}"
                f" {airtime_ms:.0f}ms"
            )
            parts.append(
                f'<div class="wf-bar" style="position:absolute;'
                f'left:{bar_left:.2f}%;'
                f'top:{y + 2}px;width:{bar_w:.2f}%;height:{header_h - 4}px;'
                f'background:{SENDER_COLOUR};opacity:0.7;border-radius:2px;'
                f'z-index:3;overflow:hidden;cursor:pointer;'
                f'color:#fff;font-size:0.75em;line-height:{header_h - 4}px;'
                f'text-align:center;white-space:nowrap" '
                f'data-sender="{rx}" data-receiver="{rx}" '
                f'data-txid="{tx_id_val}" '
                f'title="{title}">'
                f'\u2191#{pkt_num}</div>'
            )
        y += header_h

        # Sender sub-rows
        for sx in sender_order:
            events = senders[sx]
            # Row background
            bg = "#f5f5f5" if alt % 2 == 0 else "#fafafa"
            parts.append(
                f'<div style="position:absolute;left:0;top:{y}px;right:0;'
                f'height:{row_h}px;background:{bg};z-index:0"></div>'
            )
            # Sender label
            color = sender_color[sx]
            parts.append(
                f'<div data-node="{sx}" style="position:absolute;left:4px;top:{y}px;'
                f'width:{label_w}px;height:{row_h}px;line-height:{row_h}px;'
                f'color:{color};font-size:0.85em;z-index:5;white-space:nowrap;'
                f'overflow:hidden;text-overflow:ellipsis;cursor:pointer">'
                f'{medium_name(sx)}</div>'
            )
            # TX bars
            for ev in events:
                bar_left = (ev["t_start"] - t_lo) / total_w * 100
                bar_right = (ev["t_end"] - t_lo) / total_w * 100
                bar_w = max(bar_right - bar_left, 0.3)
                tx_id_val = ev["tx_id"]
                # Per-receiver outcome detection
                outcome = rx_outcomes.get((tx_id_val, rx)) if rx_outcomes else None
                is_collision = outcome == "collision"
                is_halfduplex = outcome == "halfduplex"
                step_info = (
                    tx_id_to_step.get(tx_id_val)
                    if tx_id_to_step else None
                )
                is_selected = step_info is not None
                if is_selected:
                    step_num, step_clr = step_info
                    border = (
                        f"border:2px solid {step_clr};"
                        f"box-shadow:0 0 6px {step_clr};"
                    )
                    opacity = "1.0"
                elif is_collision:
                    border = f"border:2px solid {COLLISION_COLOUR};"
                    opacity = "0.75"
                elif is_halfduplex:
                    border = f"border:2px solid {HALFDUPLEX_COLOUR};"
                    opacity = "0.75"
                else:
                    border = ""
                    opacity = "0.55"
                snr_val = ev.get("snr")
                snr_lbl = f"{snr_val:.0f}dB" if snr_val is not None else ""
                pkt_info = tx_id_to_pkt.get(tx_id_val) if tx_id_to_pkt else None
                pkt_num = pkt_info[0] if pkt_info else "?"
                pkt_type = pkt_info[1] if pkt_info else ""
                # Bar label: step number for selected, pkt# + type + SNR otherwise
                if is_selected and step_num > 0:
                    _CIRCLED = "\u2776\u2777\u2778\u2779\u277a\u277b\u277c\u277d"
                    bar_lbl = (
                        _CIRCLED[step_num - 1]
                        if step_num <= len(_CIRCLED)
                        else str(step_num)
                    )
                elif is_collision:
                    bar_lbl = f"\u2716#{pkt_num} {pkt_type} {snr_lbl}"
                elif is_halfduplex:
                    bar_lbl = f"\u23f8#{pkt_num} {pkt_type} {snr_lbl}"
                else:
                    bar_lbl = f"#{pkt_num} {pkt_type} {snr_lbl}"
                # Tooltip: sender→receiver pkt#N type tx#N airtime SNR outcome
                airtime_ms = (ev["t_end"] - ev["t_start"]) * 1000
                outcome_lbl = ""
                if is_collision:
                    outcome_lbl = " COLLISION"
                elif is_halfduplex:
                    outcome_lbl = " HALF-DUPLEX"
                title = (
                    f"{medium_name(sx)}\u2192{medium_name(rx)}"
                    f" pkt#{pkt_num} {pkt_type} tx#{tx_id_val}"
                    f" {airtime_ms:.0f}ms {snr_lbl}{outcome_lbl}"
                )
                parts.append(
                    f'<div class="wf-bar" style="position:absolute;'
                    f'left:{bar_left:.2f}%;'
                    f'top:{y + 2}px;width:{bar_w:.2f}%;height:{row_h - 4}px;'
                    f'background:{color};opacity:{opacity};border-radius:2px;'
                    f'{border}z-index:{4 if is_selected else 3};'
                    f'overflow:hidden;cursor:pointer;'
                    f'color:#fff;font-size:0.8em;line-height:{row_h - 4}px;'
                    f'text-align:center;white-space:nowrap" '
                    f'data-sender="{sx}" data-receiver="{rx}" '
                    f'data-txid="{tx_id_val}" '
                    f'title="{title}">'
                    f'{bar_lbl}</div>'
                )

            y += row_h
            alt += 1

    parts.append('</div>')

    # Update waterfall wrapper height to match content
    wrapper_h = max(total_h, 40)
    tui.waterfall_wrapper.style(
        f"height: {min(wrapper_h, 300)}px; flex-shrink: 0; overflow-y: auto;"
        "border-top: 1px solid #dee2e6; background: #fafafa;"
        "transition: height 0.2s ease"
    )
    tui.waterfall_html.set_content("".join(parts))

    # Attach click delegation via JS (runs after DOM update).
    # Builds a positions lookup so fitBounds can zoom to the edge.
    if positions and map_id:
        pos_json = _json.dumps({
            name: list(coord)
            for name, coord in positions.items()
        })
        wf_el_id = tui.waterfall_html.id
        click_js = f"""
            (function() {{
                var el = getElement({wf_el_id});
                if (!el || !el.$el) return;
                var root = el.$el;
                var pos = {pos_json};
                var mapId = {map_id};
                // Remove previous handler to avoid stacking
                if (root._wfHandler) root.removeEventListener('click', root._wfHandler);
                root._wfHandler = function(e) {{
                    var comp = getElement(mapId);
                    if (!comp || !comp.map) return;
                    // Check for node name click (receiver header or sender label)
                    var n = e.target;
                    while (n && n !== root && !n.dataset.node && !n.dataset.sender) n = n.parentElement;
                    if (n && n.dataset.node) {{
                        var p = pos[n.dataset.node];
                        if (p) comp.map.setView(p, Math.max(comp.map.getZoom(), 15));
                        return;
                    }}
                    // Bar click: zoom to sender→receiver edge
                    if (n && n.dataset.sender) {{
                        var s = n.dataset.sender, r = n.dataset.receiver;
                        var ps = pos[s], pr = pos[r];
                        if (ps && pr) {{
                            comp.map.fitBounds(
                                [[Math.min(ps[0],pr[0]), Math.min(ps[1],pr[1])],
                                 [Math.max(ps[0],pr[0]), Math.max(ps[1],pr[1])]],
                                {{padding: [40,40], maxZoom: 16}}
                            );
                        }}
                    }}
                }};
                root.addEventListener('click', root._wfHandler);
            }})();
        """
        tui.map.client.run_javascript(click_js)


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
    halfduplex_pairs: list[tuple[str, str]] = []
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
        elif ev["type"] == "halfduplex":
            halfduplex_pairs.append((ev["sender"], ev["receiver"]))
            desc = f"{short_name(ev['sender'])}\u2192{short_name(ev['receiver'])} [HALF-DUPLEX]"
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

    # --- Build half-duplex polyline coordinates ---
    halfduplex_coords = []
    for sender, receiver in halfduplex_pairs:
        pos_s = positions.get(sender)
        pos_r = positions.get(receiver)
        if pos_s and pos_r:
            halfduplex_coords.append([list(pos_s), list(pos_r)])

    # --- Build propagation overlay data for selected packet ---
    prop_lines = []   # [{coords: [[lat,lon],[lat,lon]], color: str, step: int}]
    prop_markers = []  # [{pos: [lat,lon], label: str, color: str}]
    prop_collisions = []  # [{pos: [lat,lon]}]
    sel_pkt_idx = getattr(state, "_selected_pkt_idx", None)

    if sel_pkt_idx is not None and 0 <= sel_pkt_idx < len(packets):
        sel_pkt = packets[sel_pkt_idx]
        steps = broadcast_steps(sel_pkt)
        for step_i, step in enumerate(steps):
            color = _STEP_HUES[step_i % len(_STEP_HUES)]
            for hop in step:
                pos_s = positions.get(hop["sender"])
                pos_r = positions.get(hop["receiver"])
                if pos_s and pos_r:
                    prop_lines.append({
                        "coords": [list(pos_s), list(pos_r)],
                        "color": color,
                    })
            # Step number marker at midpoint of first hop
            if step:
                first_hop = step[0]
                p_s = positions.get(first_hop["sender"])
                p_r = positions.get(first_hop["receiver"])
                if p_s and p_r:
                    mid = [(p_s[0] + p_r[0]) / 2, (p_s[1] + p_r[1]) / 2]
                    prop_markers.append({
                        "pos": mid,
                        "label": str(step_i + 1),
                        "color": color,
                    })

        # Collision X-marks
        for col in sel_pkt.get("collisions", []):
            p_s = positions.get(col["sender"])
            p_r = positions.get(col["receiver"])
            if p_s and p_r:
                mid = [(p_s[0] + p_r[0]) / 2, (p_s[1] + p_r[1]) / 2]
                prop_collisions.append({"pos": mid})

        # Half-duplex X-marks (orange)
        for hd in sel_pkt.get("halfduplex", []):
            p_s = positions.get(hd["sender"])
            p_r = positions.get(hd["receiver"])
            if p_s and p_r:
                mid = [(p_s[0] + p_r[0]) / 2, (p_s[1] + p_r[1]) / 2]
                prop_collisions.append({"pos": mid, "color": HALFDUPLEX_COLOUR})

    # --- Map selected packet's tx_ids to broadcast step numbers ---
    # tx_id_to_step: {tx_id: (step_number, step_color)} for waterfall highlight
    tx_id_to_step: dict[int, tuple[int, str]] = {}
    if sel_pkt_idx is not None and 0 <= sel_pkt_idx < len(packets):
        sel_pkt = packets[sel_pkt_idx]
        steps = broadcast_steps(sel_pkt)
        for step_i, step in enumerate(steps):
            step_color = _STEP_HUES[step_i % len(_STEP_HUES)]
            for hop in step:
                tid = hop.get("tx_id")
                if tid is not None:
                    tx_id_to_step[tid] = (step_i + 1, step_color)
        # Also include collision tx_ids (step 0 = unknown step)
        for col in sel_pkt.get("collisions", []):
            tid = col.get("tx_id")
            if tid is not None and tid not in tx_id_to_step:
                tx_id_to_step[tid] = (0, COLLISION_COLOUR)

    # --- Build per-receiver outcome lookup for waterfall ---
    # (tx_id, receiver) -> "collision" | "halfduplex"
    rx_outcomes: dict[tuple[int, str], str] = {}
    # tx_id -> (pkt_num 1-based, short_type_name)
    tx_id_to_pkt: dict[int, tuple[int, str]] = {}
    if state.trace:
        trace_packets = state.trace.get("packets", [])
        for pkt_idx_o, pkt_o in enumerate(trace_packets):
            pkt_num = pkt_idx_o + 1
            pkt_type = pkt_o.get("payload_type_name", "?")[:6]
            for hop in pkt_o.get("hops", []):
                tid = hop.get("tx_id")
                if tid is not None and tid not in tx_id_to_pkt:
                    tx_id_to_pkt[tid] = (pkt_num, pkt_type)
            for col in pkt_o.get("collisions", []):
                tid = col.get("tx_id")
                rx_name = col.get("receiver")
                if tid is not None and rx_name:
                    rx_outcomes[(tid, rx_name)] = "collision"
                if tid is not None and tid not in tx_id_to_pkt:
                    tx_id_to_pkt[tid] = (pkt_num, pkt_type)
            for hd in pkt_o.get("halfduplex", []):
                tid = hd.get("tx_id")
                rx_name = hd.get("receiver")
                if tid is not None and rx_name:
                    rx_outcomes[(tid, rx_name)] = "halfduplex"
                if tid is not None and tid not in tx_id_to_pkt:
                    tx_id_to_pkt[tid] = (pkt_num, pkt_type)

    # --- Build waterfall data (receiver-centric) ---
    waterfall_data = []
    if state._waterfall_visible:
        tx_events = state.trace.get("tx_events", {})
        if tx_events and topo:
            # Build sender → {(receiver, snr)} adjacency from topology edges
            sender_to_receivers: dict[str, list[tuple[str, float]]] = {}
            for edge in topo.edges:
                # a→b direction: SNR as seen by b
                snr_ab = edge.snr
                if edge.a_to_b and edge.a_to_b.snr is not None:
                    snr_ab = edge.a_to_b.snr
                # b→a direction: SNR as seen by a
                snr_ba = edge.snr
                if edge.b_to_a and edge.b_to_a.snr is not None:
                    snr_ba = edge.b_to_a.snr
                sender_to_receivers.setdefault(edge.a, []).append((edge.b, snr_ab))
                sender_to_receivers.setdefault(edge.b, []).append((edge.a, snr_ba))

            wf_window = 2.0
            t_lo = t - wf_window
            t_hi = t + wf_window
            for tid_str, ev in tx_events.items():
                t_start_ev = ev.get("t_start", 0)
                t_end_ev = ev.get("t_end")
                if t_end_ev is None:
                    continue
                if t_end_ev < t_lo or t_start_ev > t_hi:
                    continue
                sender_name = ev["sender"]
                tid = int(tid_str)
                for rx, snr_val in sender_to_receivers.get(sender_name, ()):
                    waterfall_data.append({
                        "sender": sender_name,
                        "receiver": rx,
                        "t_start": t_start_ev,
                        "t_end": t_end_ev,
                        "tx_id": tid,
                        "snr": snr_val,
                    })
                # Self-TX entry: show the sender's own transmission on
                # its receiver row so you can see when it's busy.
                waterfall_data.append({
                    "sender": sender_name,
                    "receiver": sender_name,
                    "t_start": t_start_ev,
                    "t_end": t_end_ev,
                    "tx_id": tid,
                    "snr": None,
                    "self_tx": True,
                })
            waterfall_data.sort(key=lambda x: x["t_start"])

    # --- Single JS call to update everything ---
    styles_json = _json.dumps(styles)
    flow_json = _json.dumps(flow_coords)
    col_json = _json.dumps(collision_coords)
    hd_json = _json.dumps(halfduplex_coords)
    prop_lines_json = _json.dumps(prop_lines)
    prop_markers_json = _json.dumps(prop_markers)
    prop_col_json = _json.dumps(prop_collisions)
    waterfall_json = _json.dumps(waterfall_data)
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

            // Half-duplex edges (orange dotted)
            if (window._wb_hd) {{
                window._wb_hd.forEach(function(l) {{ map.removeLayer(l); }});
            }}
            window._wb_hd = [];
            var hds = {hd_json};
            for (var i = 0; i < hds.length; i++) {{
                var pl = L.polyline(hds[i], {{
                    color: '{HALFDUPLEX_COLOUR}',
                    weight: 4,
                    dashArray: '2,6',
                    opacity: 0.9
                }});
                pl.addTo(map);
                window._wb_hd.push(pl);
            }}

            // Propagation overlay (selected packet journey)
            if (window._wb_prop) {{
                window._wb_prop.forEach(function(l) {{ map.removeLayer(l); }});
            }}
            window._wb_prop = [];
            var propLines = {prop_lines_json};
            for (var i = 0; i < propLines.length; i++) {{
                var pl = L.polyline(propLines[i].coords, {{
                    color: propLines[i].color,
                    weight: 5,
                    opacity: 0.85
                }});
                pl.addTo(map);
                window._wb_prop.push(pl);
            }}
            var propMarkers = {prop_markers_json};
            for (var i = 0; i < propMarkers.length; i++) {{
                var pm = propMarkers[i];
                var icon = L.divIcon({{
                    html: '<div style="background:'+pm.color+';color:#fff;'
                        +'width:20px;height:20px;border-radius:50%;display:flex;'
                        +'align-items:center;justify-content:center;font-size:11px;'
                        +'font-weight:bold;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,0.4)">'
                        +pm.label+'</div>',
                    className: '',
                    iconSize: [20, 20],
                    iconAnchor: [10, 10]
                }});
                var m = L.marker(pm.pos, {{icon: icon, interactive: false}});
                m.addTo(map);
                window._wb_prop.push(m);
            }}
            var propCols = {prop_col_json};
            for (var i = 0; i < propCols.length; i++) {{
                var pc = propCols[i];
                var xColor = pc.color || '{COLLISION_COLOUR}';
                var xIcon = L.divIcon({{
                    html: '<div style="color:'+xColor+';font-size:22px;'
                        +'font-weight:bold;text-shadow:0 0 3px #fff">\u2716</div>',
                    className: '',
                    iconSize: [22, 22],
                    iconAnchor: [11, 11]
                }});
                var xm = L.marker(pc.pos, {{icon: xIcon, interactive: false}});
                xm.addTo(map);
                window._wb_prop.push(xm);
            }}
        }})();
    """

    tui.map.client.run_javascript(js)

    # Update waterfall panel
    _update_waterfall(state, waterfall_data, t,
                      map_id=tui.map_id, positions=tui.positions,
                      tx_id_to_step=tx_id_to_step,
                      rx_outcomes=rx_outcomes, tx_id_to_pkt=tx_id_to_pkt)
