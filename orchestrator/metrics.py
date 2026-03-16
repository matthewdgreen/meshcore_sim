"""
metrics.py — Simulation-wide event counters and latency tracker.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SendRecord:
    sender: str
    dest_pub: str
    text: str
    sent_at: float                          # monotonic seconds
    received_at: Optional[float] = None
    received_by: Optional[str] = None


class MetricsCollector:
    def __init__(self) -> None:
        # Per-node packet counts (orchestrator-level, not node-reported)
        self._tx: dict[str, int] = defaultdict(int)
        self._rx: dict[str, int] = defaultdict(int)

        # Drops
        self._link_loss_count: int = 0
        self._adv_drop_count: int = 0
        self._adv_corrupt_count: int = 0
        self._adv_replay_count: int = 0

        # Message delivery tracking — keyed by message text
        # (unique per send because TrafficGenerator embeds a timestamp)
        self._pending: dict[str, SendRecord] = {}
        self._completed: list[SendRecord] = []

    # ------------------------------------------------------------------
    # Recording methods (called by router / traffic generator)
    # ------------------------------------------------------------------

    def record_tx(self, node: str) -> None:
        self._tx[node] += 1

    def record_rx(self, receiver: str) -> None:
        self._rx[receiver] += 1

    def record_link_loss(self, sender: str, receiver: str) -> None:
        self._link_loss_count += 1

    def record_adversarial_drop(self, receiver: str) -> None:
        self._adv_drop_count += 1

    def record_adversarial_corrupt(self, receiver: str) -> None:
        self._adv_corrupt_count += 1

    def record_adversarial_replay(self, node: str) -> None:
        self._adv_replay_count += 1

    def record_send_attempt(self, sender: str, dest_pub: str, text: str) -> None:
        self._pending[text] = SendRecord(
            sender=sender,
            dest_pub=dest_pub,
            text=text,
            sent_at=time.monotonic(),
        )

    # ------------------------------------------------------------------
    # Event callback (registered on all NodeAgents)
    # ------------------------------------------------------------------

    async def on_event(self, node_name: str, event: dict) -> None:
        etype = event.get("type")
        if etype == "recv_text":
            text = event.get("text", "")
            if text in self._pending:
                rec = self._pending.pop(text)
                rec.received_at = time.monotonic()
                rec.received_by = node_name
                self._completed.append(rec)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> str:
        lines: list[str] = ["", "=" * 50, "  Simulation Metrics", "=" * 50, ""]

        # Per-node TX / RX
        all_nodes = sorted(set(self._tx) | set(self._rx))
        if all_nodes:
            lines.append(f"  {'Node':<20}  {'TX':>6}  {'RX':>6}")
            lines.append(f"  {'-'*20}  {'-'*6}  {'-'*6}")
            for n in all_nodes:
                lines.append(f"  {n:<20}  {self._tx[n]:>6}  {self._rx[n]:>6}")
        lines.append("")

        # Delivery rate
        total = len(self._completed) + len(self._pending)
        delivered = len(self._completed)
        rate = delivered / total * 100.0 if total else 0.0
        lines.append(f"  Message delivery: {delivered}/{total} ({rate:.1f}%)")
        if self._pending:
            lines.append(f"  Undelivered:      {len(self._pending)} message(s) still in flight")

        # Latency
        if self._completed:
            latencies = [
                (r.received_at - r.sent_at) * 1000.0
                for r in self._completed
                if r.received_at is not None
            ]
            if latencies:
                avg = sum(latencies) / len(latencies)
                mx = max(latencies)
                mn = min(latencies)
                lines.append(
                    f"  Latency (send→recv): min={mn:.0f}ms  avg={avg:.0f}ms  max={mx:.0f}ms"
                )

        lines.append("")

        # Drop / adversarial counts
        lines.append(f"  Link-level packet loss:  {self._link_loss_count}")
        lines.append(f"  Adversarial drops:       {self._adv_drop_count}")
        lines.append(f"  Adversarial corruptions: {self._adv_corrupt_count}")
        lines.append(f"  Adversarial replays:     {self._adv_replay_count}")
        lines.append("")

        # Delivered message log
        if self._completed:
            lines.append("  Delivered messages:")
            for r in self._completed:
                lat = (
                    (r.received_at - r.sent_at) * 1000.0
                    if r.received_at is not None
                    else -1.0
                )
                lines.append(
                    f"    [{lat:6.0f} ms]  {r.sender} → {r.received_by}: {r.text!r}"
                )
            lines.append("")

        lines.append("=" * 50)
        return "\n".join(lines)
