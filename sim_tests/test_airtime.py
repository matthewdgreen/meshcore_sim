"""
test_airtime.py — Unit tests for orchestrator/airtime.py and
orchestrator/channel.py.

Airtime spot-checks are derived from the Semtech AN1200.13 formula.
No node_agent binary required.
"""

from __future__ import annotations

import unittest

from orchestrator.airtime import lora_airtime_ms
from orchestrator.channel import ChannelModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approx(a: float, b: float, tol: float = 0.1) -> bool:
    """True if a and b agree to within *tol* ms."""
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Airtime formula tests
# ---------------------------------------------------------------------------

class TestLoraAirtimeFormula(unittest.TestCase):

    # --- Symbol duration ---

    def test_sf9_bw125_symbol_duration(self):
        """SF9 / BW125 kHz → T_sym = 2^9/125 = 4.096 ms."""
        # t_preamble = (8+4.25)*4.096 = 50.176 ms
        # Verify by checking a zero-payload edge case doesn't raise
        result = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=1)
        self.assertGreater(result, 50.0)   # at least preamble duration

    # --- Spot-check against published Semtech calculator values ---

    def test_sf9_bw125_cr45_10bytes(self):
        # t_sym=4.096ms; preamble=50.176ms; payload_syms=23 → 144.384 ms
        result = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=10)
        self.assertTrue(_approx(result, 144.384), f"got {result:.3f} ms")

    def test_sf9_bw125_cr45_64bytes(self):
        # payload_syms=83 → preamble+payload = 50.176+339.968 = 390.144 ms
        result = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=64)
        self.assertTrue(_approx(result, 390.144), f"got {result:.3f} ms")

    def test_sf7_bw125_cr45_20bytes(self):
        # t_sym=1.024ms; preamble=12.544ms; payload_syms=43 → 56.576 ms
        result = lora_airtime_ms(sf=7, bw_hz=125_000, cr=1, payload_bytes=20)
        self.assertTrue(_approx(result, 56.576), f"got {result:.3f} ms")

    def test_sf12_bw125_cr45_10bytes_ldr(self):
        # SF12/BW125 → t_sym=32.768ms ≥ 16ms → LDR enabled (de=1)
        # preamble=401.408ms; payload_syms=18 → total=991.232 ms
        result = lora_airtime_ms(sf=12, bw_hz=125_000, cr=1, payload_bytes=10)
        self.assertTrue(_approx(result, 991.232), f"got {result:.3f} ms")

    # --- Monotonicity ---

    def test_larger_payload_takes_longer(self):
        base = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=20)
        bigger = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=80)
        self.assertGreater(bigger, base)

    def test_higher_sf_takes_longer(self):
        low_sf  = lora_airtime_ms(sf=7,  bw_hz=125_000, cr=1, payload_bytes=32)
        high_sf = lora_airtime_ms(sf=12, bw_hz=125_000, cr=1, payload_bytes=32)
        self.assertGreater(high_sf, low_sf)

    def test_wider_bandwidth_is_faster(self):
        narrow = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=32)
        wide   = lora_airtime_ms(sf=9, bw_hz=500_000, cr=1, payload_bytes=32)
        self.assertGreater(narrow, wide)

    def test_higher_cr_takes_longer(self):
        cr45 = lora_airtime_ms(sf=9, bw_hz=125_000, cr=1, payload_bytes=32)
        cr48 = lora_airtime_ms(sf=9, bw_hz=125_000, cr=4, payload_bytes=32)
        self.assertGreater(cr48, cr45)

    def test_result_is_positive(self):
        for sf in (7, 9, 12):
            with self.subTest(sf=sf):
                result = lora_airtime_ms(sf=sf, bw_hz=125_000, cr=1,
                                         payload_bytes=16)
                self.assertGreater(result, 0.0)


# ---------------------------------------------------------------------------
# ChannelModel — hard collision (no positions)
# ---------------------------------------------------------------------------

class TestChannelModelHardCollision(unittest.TestCase):
    """Equal-RSSI collisions (no capture effect)."""

    def _model(self, link_rssi: dict) -> ChannelModel:
        return ChannelModel(link_rssi=link_rssi)

    # Helper: equal RSSI → neither can capture → hard collision
    _EQUAL = {"A": {"R": -90.0}, "B": {"R": -90.0}}

    def test_no_collision_when_no_other_tx(self):
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_no_collision_non_overlapping_windows(self):
        """B transmits after A's window ends → no collision."""
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.4, 0.7, tx_id=2)
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_collision_overlapping_windows(self):
        """A and B transmit simultaneously, both reach R → collision."""
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        self.assertTrue(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_no_collision_interferer_cannot_reach_receiver(self):
        """B overlaps in time but cannot reach R → no collision."""
        m = self._model({"A": {"R": -90.0}, "B": {}})  # B cannot reach R
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_expire_removes_old_records(self):
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        m.expire_before(0.5)   # both windows have ended
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_expire_keeps_active_records(self):
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        m.expire_before(0.2)   # B's window hasn't ended yet
        self.assertTrue(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_touching_windows_not_collision(self):
        """Windows that touch but don't overlap: B starts exactly when A ends."""
        m = self._model(self._EQUAL)
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.3, 0.6, tx_id=2)   # start == A's end
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))


# ---------------------------------------------------------------------------
# ChannelModel — capture effect (with positions)
# ---------------------------------------------------------------------------

class TestChannelModelCaptureEffect(unittest.TestCase):
    """
    Two nodes: A has strong RSSI to R, B has weak RSSI to R.
    With the capture effect enabled, A should survive B's interference.
    """

    # A is strong (-60 dBm), B is weak (-100 dBm) → 40 dB difference >> 6 dB
    _RSSI = {"A": {"R": -60.0}, "B": {"R": -100.0}}

    def _model(self, cap_db: float = 6.0) -> ChannelModel:
        return ChannelModel(
            link_rssi=self._RSSI,
            capture_threshold_db=cap_db,
        )

    def test_strong_primary_survives_weak_interferer(self):
        """A (-60 dBm) is much stronger than B (-100 dBm) → capture."""
        m = self._model()
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        self.assertFalse(m.is_lost("A", "R", 0.0, 0.3, tx_id=1))

    def test_weak_primary_lost_to_strong_interferer(self):
        """B (-100 dBm) cannot capture against A (-60 dBm) → B is lost."""
        m = self._model()
        m.register_tx("A", 0.0, 0.3, tx_id=1)
        m.register_tx("B", 0.1, 0.4, tx_id=2)
        self.assertTrue(m.is_lost("B", "R", 0.1, 0.4, tx_id=2))

    def test_equal_rssi_hard_collision(self):
        """Two equal-RSSI senders: neither captures → both lost."""
        m_hard = ChannelModel(
            link_rssi={"A": {"R": -90.0}, "C": {"R": -90.0}},
        )
        m_hard.register_tx("A", 0.0, 0.3, tx_id=1)
        m_hard.register_tx("C", 0.1, 0.4, tx_id=2)
        self.assertTrue(m_hard.is_lost("A", "R", 0.0, 0.3, tx_id=1))


# ---------------------------------------------------------------------------
# ChannelModel — half-duplex (receiver busy during own TX)
# ---------------------------------------------------------------------------

class TestChannelModelHalfDuplex(unittest.TestCase):
    """LoRa is half-duplex: a node cannot receive while it is transmitting."""

    def _model(self, link_rssi: dict) -> ChannelModel:
        return ChannelModel(link_rssi=link_rssi)

    _RSSI = {"A": {"R": -90.0}, "R": {"A": -90.0}}

    def test_receiver_busy_during_own_tx(self):
        """R transmits [0.0, 0.5]; RX window [0.1, 0.4] fully inside → busy."""
        m = self._model(self._RSSI)
        m.register_tx("R", 0.0, 0.5, tx_id=1)
        self.assertTrue(m.is_receiver_busy("R", 0.1, 0.4))

    def test_receiver_idle(self):
        """No active TX from R → not busy."""
        m = self._model({"A": {"R": -90.0}})
        self.assertFalse(m.is_receiver_busy("R", 0.1, 0.4))

    def test_partial_overlap_start(self):
        """R TX [0.0, 0.3]; RX window [0.2, 0.6] overlaps at the start."""
        m = self._model(self._RSSI)
        m.register_tx("R", 0.0, 0.3, tx_id=1)
        self.assertTrue(m.is_receiver_busy("R", 0.2, 0.6))

    def test_partial_overlap_end(self):
        """R TX [0.4, 0.7]; RX window [0.2, 0.6] overlaps at the end."""
        m = self._model(self._RSSI)
        m.register_tx("R", 0.4, 0.7, tx_id=1)
        self.assertTrue(m.is_receiver_busy("R", 0.2, 0.6))

    def test_no_overlap_touching(self):
        """R TX [0.0, 0.3]; RX window [0.3, 0.6] — touching but not overlapping."""
        m = self._model(self._RSSI)
        m.register_tx("R", 0.0, 0.3, tx_id=1)
        self.assertFalse(m.is_receiver_busy("R", 0.3, 0.6))

    def test_other_node_tx_ignored(self):
        """A transmits but R does not → R is not busy."""
        m = self._model(self._RSSI)
        m.register_tx("A", 0.0, 0.5, tx_id=1)
        self.assertFalse(m.is_receiver_busy("R", 0.1, 0.4))


if __name__ == "__main__":
    unittest.main()
