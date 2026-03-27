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
        self._rss_kb: dict[str, int] = {}  # RSS snapshot at end of simulation

        # Drops
        self._link_loss_count: int = 0
        self._adv_drop_count: int = 0
        self._adv_corrupt_count: int = 0
        self._adv_replay_count: int = 0
        self._collision_count: int = 0
        self._halfduplex_drop_count: int = 0
        self._snr_drop_count: int = 0

        # Message delivery tracking — keyed by message text
        # (unique per send because TrafficGenerator embeds a timestamp)
        self._pending: dict[str, SendRecord] = {}
        self._completed: list[SendRecord] = []

        # Channel (group) message tracking
        self._channel_sent: int = 0
        self._channel_recv: int = 0

        # ACK outcome counters (parsed from C++ node "log" events)
        self._ack_confirmed: int = 0
        self._ack_retries: int = 0
        self._ack_failed: int = 0

        # Contact discovery snapshot (populated before shutdown)
        self._contacts: dict[str, tuple[int, int]] = {}  # name → (discovered, total)

        # Pub-key → node name mapping (populated before report)
        self._pub_to_name: dict[str, str] = {}

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

    def record_collision(self, sender: str, receiver: str) -> None:
        self._collision_count += 1

    def record_halfduplex_drop(self, sender: str, receiver: str) -> None:
        self._halfduplex_drop_count += 1

    def record_snr_drop(self, sender: str, receiver: str) -> None:
        self._snr_drop_count += 1

    def record_rss(self, node: str, rss_kb: int) -> None:
        self._rss_kb[node] = rss_kb

    def record_contacts(self, name: str, discovered: int, total: int) -> None:
        self._contacts[name] = (discovered, total)

    def set_pub_to_name(self, mapping: dict[str, str]) -> None:
        self._pub_to_name = mapping

    def record_send_attempt(self, sender: str, dest_pub: str, text: str) -> None:
        self._pending[text] = SendRecord(
            sender=sender,
            dest_pub=dest_pub,
            text=text,
            sent_at=time.monotonic(),
        )

    def record_channel_send(self, sender: str, text: str) -> None:
        self._channel_sent += 1

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
        elif etype == "recv_channel":
            self._channel_recv += 1
        elif etype == "log":
            msg = event.get("msg", "")
            if msg.startswith("ACK confirmed"):
                self._ack_confirmed += 1
            elif msg.startswith("ACK timeout"):
                self._ack_retries += 1
            elif msg.startswith("msg delivery failed"):
                self._ack_failed += 1

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Computed properties (for programmatic access by experiments/)
    # ------------------------------------------------------------------

    @property
    def delivered_count(self) -> int:
        return len(self._completed)

    @property
    def attempted_count(self) -> int:
        return len(self._completed) + len(self._pending)

    @property
    def delivery_rate(self) -> float:
        """Fraction of attempted messages that were delivered (0.0–1.0)."""
        total = self.attempted_count
        return self.delivered_count / total if total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        """Average send→receive latency in milliseconds (0.0 if none delivered)."""
        latencies = [
            (r.received_at - r.sent_at) * 1000.0
            for r in self._completed
            if r.received_at is not None
        ]
        return sum(latencies) / len(latencies) if latencies else 0.0

    @property
    def channel_sent(self) -> int:
        return self._channel_sent

    @property
    def channel_recv(self) -> int:
        return self._channel_recv

    @property
    def collision_count(self) -> int:
        return self._collision_count

    @property
    def halfduplex_drop_count(self) -> int:
        return self._halfduplex_drop_count

    @property
    def snr_drop_count(self) -> int:
        return self._snr_drop_count

    def to_dict(self) -> dict:
        """Export all metrics as a JSON-serialisable dict.

        This dict is embedded in the trace JSON so that the workbench can
        display comprehensive simulation statistics without re-running.
        """
        # Delivery
        total = len(self._completed) + len(self._pending)
        delivered = len(self._completed)
        rate = delivered / total if total else 0.0

        # Latency percentiles
        latencies = sorted(
            (r.received_at - r.sent_at) * 1000.0
            for r in self._completed
            if r.received_at is not None
        )
        latency: dict = {}
        if latencies:
            latency = {
                "min_ms": latencies[0],
                "p50_ms": latencies[len(latencies) // 2],
                "avg_ms": sum(latencies) / len(latencies),
                "p95_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)],
                "max_ms": latencies[-1],
            }

        # Per-node TX/RX
        all_nodes = sorted(set(self._tx) | set(self._rx))
        per_node = {
            n: {"tx": self._tx[n], "rx": self._rx[n]}
            for n in all_nodes
        }

        # Contacts
        contacts = {
            name: {"discovered": disc, "total": tot}
            for name, (disc, tot) in self._contacts.items()
        }

        return {
            "delivery": {
                "attempted": total,
                "delivered": delivered,
                "rate": rate,
            },
            "latency": latency,
            "ack_outcomes": {
                "confirmed": self._ack_confirmed,
                "retries": self._ack_retries,
                "failed": self._ack_failed,
            },
            "channel_messages": {
                "sent": self._channel_sent,
                "recv": self._channel_recv,
            },
            "drops": {
                "snr_below_threshold": self._snr_drop_count,
                "link_loss": self._link_loss_count,
                "collisions": self._collision_count,
                "halfduplex": self._halfduplex_drop_count,
                "adversarial_drop": self._adv_drop_count,
                "adversarial_corrupt": self._adv_corrupt_count,
                "adversarial_replay": self._adv_replay_count,
            },
            "contacts": contacts,
            "per_node": per_node,
        }

    def report(self) -> str:
        # Per-node TX / RX — column width adapts to the longest node name
        all_nodes = sorted(set(self._tx) | set(self._rx))
        col = max((len(n) for n in all_nodes), default=0)
        col = max(col, len("Node"))          # never narrower than the header
        banner_w = max(50, col + 18)         # 18 = "  " + "  " + "  ------  ------"

        lines: list[str] = ["", "=" * banner_w, "  Simulation Metrics", "=" * banner_w, ""]

        if all_nodes:
            lines.append(f"  {'Node':<{col}}  {'TX':>6}  {'RX':>6}")
            lines.append(f"  {'-'*col}  {'-'*6}  {'-'*6}")
            for n in all_nodes:
                lines.append(f"  {n:<{col}}  {self._tx[n]:>6}  {self._rx[n]:>6}")
        lines.append("")

        # Endpoint contact discovery
        if self._contacts:
            lines.append("  Endpoint contacts discovered:")
            for name in sorted(self._contacts):
                disc, total = self._contacts[name]
                pct = disc / total * 100.0 if total else 0.0
                lines.append(f"    {name:<{col}}  {disc}/{total}  ({pct:.0f}%)")
            lines.append("")

        # Delivery rate
        total = len(self._completed) + len(self._pending)
        delivered = len(self._completed)
        rate = delivered / total * 100.0 if total else 0.0
        lines.append(f"  Message delivery: {delivered}/{total} ({rate:.1f}%)")
        if self._pending:
            lines.append(f"  Undelivered:      {len(self._pending)} message(s) still in flight")

        # ACK outcome breakdown
        ack_total = self._ack_confirmed + self._ack_retries + self._ack_failed
        if ack_total:
            lines.append(
                f"  ACK outcomes:     confirmed={self._ack_confirmed}  "
                f"retries={self._ack_retries}  failed={self._ack_failed}"
            )

        # Channel (group) message stats
        if self._channel_sent or self._channel_recv:
            lines.append(
                f"  Channel messages: sent={self._channel_sent}  "
                f"recv={self._channel_recv}"
            )

        # Latency with percentiles
        if self._completed:
            latencies = sorted(
                (r.received_at - r.sent_at) * 1000.0
                for r in self._completed
                if r.received_at is not None
            )
            if latencies:
                avg = sum(latencies) / len(latencies)
                mn = latencies[0]
                mx = latencies[-1]
                p50 = latencies[len(latencies) // 2]
                p95_idx = min(int(len(latencies) * 0.95), len(latencies) - 1)
                p95 = latencies[p95_idx]
                lines.append(
                    f"  Latency (send→recv): min={mn:.0f}ms  p50={p50:.0f}ms  "
                    f"avg={avg:.0f}ms  p95={p95:.0f}ms  max={mx:.0f}ms"
                )

        lines.append("")

        # Drop / adversarial counts
        lines.append(f"  SNR below threshold:     {self._snr_drop_count}")
        lines.append(f"  Link-level packet loss:  {self._link_loss_count}")
        lines.append(f"  RF collisions dropped:   {self._collision_count}")
        lines.append(f"  Half-duplex RX drops:    {self._halfduplex_drop_count}")
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

        # Undelivered message log
        if self._pending:
            lines.append("  Undelivered messages:")
            for r in self._pending.values():
                dest_name = self._pub_to_name.get(r.dest_pub, r.dest_pub[:16] + "...")
                lines.append(
                    f"    {r.sender} → {dest_name}: {r.text!r}"
                )
            lines.append("")

        # RSS snapshot (omitted when no samples were collected)
        if self._rss_kb:
            lines.append("  RSS at simulation end:")
            for n in sorted(self._rss_kb):
                lines.append(f"    {n:<{col}}  {self._rss_kb[n]:>6} KB")
            lines.append("")

        lines.append("=" * banner_w)
        return "\n".join(lines)
