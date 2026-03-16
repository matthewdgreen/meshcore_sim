"""
adversarial.py — Per-node adversarial packet filter.

Applied on the RECEIVING node (models a compromised relay that tampers with
packets it receives before forwarding them).

Modes
-----
drop    Silent discard of the packet.
corrupt N random bit-flips in the packet bytes before delivery.
replay  Captures the packet and re-emits it after replay_delay_ms;
        suppresses the original so only the delayed copy propagates.
"""

from __future__ import annotations

import binascii
import random
from typing import Optional

from .config import AdversarialConfig


class AdversarialFilter:
    def __init__(self, config: AdversarialConfig, rng: random.Random) -> None:
        self._cfg = config
        self._rng = rng
        # (emit_at_monotonic_seconds, hex_data)
        self._replay_buffer: list[tuple[float, str]] = []

    # ------------------------------------------------------------------
    # Public API called by PacketRouter
    # ------------------------------------------------------------------

    def should_apply(self) -> bool:
        """Probabilistic gate — returns True if this packet triggers the mode."""
        return self._rng.random() < self._cfg.probability

    def filter_packet(self, hex_data: str, now: float) -> Optional[str]:
        """
        Apply the adversarial mode to hex_data.

        Returns
        -------
        None          → drop this packet (do not deliver)
        str           → deliver this (possibly mutated) hex string
        """
        mode = self._cfg.mode
        if mode == "drop":
            return None
        elif mode == "corrupt":
            return self._corrupt(hex_data)
        elif mode == "replay":
            emit_at = now + self._cfg.replay_delay_ms / 1000.0
            self._replay_buffer.append((emit_at, hex_data))
            # Suppress the original — only the replayed copy will propagate
            return None
        # Unknown mode: pass through
        return hex_data

    def drain_replays(self, now: float) -> list[str]:
        """Return all packets whose replay deadline has passed, removing them."""
        ready = [h for t, h in self._replay_buffer if now >= t]
        self._replay_buffer = [(t, h) for t, h in self._replay_buffer if now < t]
        return ready

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _corrupt(self, hex_data: str) -> str:
        data = bytearray(binascii.unhexlify(hex_data))
        if not data:
            return hex_data
        for _ in range(self._cfg.corrupt_byte_count):
            idx = self._rng.randrange(len(data))
            bit = 1 << self._rng.randrange(8)
            data[idx] ^= bit
        return data.hex()
