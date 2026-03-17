"""
test_tracer.py — Unit tests for orchestrator/tracer.py.

Tests the PacketTracer using synthetic packet hex strings.  No node_agent
binary required.
"""

from __future__ import annotations

import unittest

from orchestrator.packet import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_TXT_MSG,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
)
from orchestrator.tracer import PacketTracer


# ---------------------------------------------------------------------------
# Helper: build a minimal valid packet hex
# ---------------------------------------------------------------------------

def _pkt(
    route_type: int = ROUTE_TYPE_FLOOD,
    payload_type: int = PAYLOAD_TYPE_TXT_MSG,
    path_count: int = 0,
    path_size: int = 1,
    path_bytes: bytes = b"",
    payload: bytes = b"\xDE\xAD\xBE\xEF",
) -> str:
    header = (route_type & 0x03) | ((payload_type & 0x0F) << 2)
    path_len_byte = (path_count & 0x3F) | (((path_size - 1) & 0x03) << 6)
    raw = bytes([header, path_len_byte]) + path_bytes + payload
    return raw.hex()


# Same payload, path grows by one relay hash
_MSG_HOP0 = _pkt(route_type=ROUTE_TYPE_FLOOD, path_count=0, payload=b"\xDE\xAD\xBE\xEF")
_MSG_HOP1 = _pkt(route_type=ROUTE_TYPE_FLOOD, path_count=1, path_bytes=b"\xAA",
                 payload=b"\xDE\xAD\xBE\xEF")
_MSG_HOP2 = _pkt(route_type=ROUTE_TYPE_FLOOD, path_count=2, path_bytes=b"\xAA\xBB",
                 payload=b"\xDE\xAD\xBE\xEF")

# A different message
_OTHER_MSG = _pkt(payload=b"\x11\x22\x33\x44")

# An ACK
_ACK = _pkt(route_type=ROUTE_TYPE_FLOOD, payload_type=PAYLOAD_TYPE_ACK, payload=b"\x01\x02\x03\x04")

# A direct-routed message (same payload as _MSG_HOP0 but different route type)
_DIRECT_MSG = _pkt(route_type=ROUTE_TYPE_DIRECT, path_count=1, path_bytes=b"\xCC",
                   payload=b"\xDE\xAD\xBE\xEF")


class TestPacketTracerBasic(unittest.TestCase):

    def setUp(self):
        self.tracer = PacketTracer()

    def test_record_tx_returns_tx_id(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.assertIsNotNone(tx_id)
        self.assertIsInstance(tx_id, int)

    def test_record_tx_tx_ids_are_unique(self):
        """Each TX event gets a distinct tx_id even for the same packet."""
        tx_id1 = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        tx_id2 = self.tracer.record_tx("relay1", _MSG_HOP1, 0.1)
        self.assertNotEqual(tx_id1, tx_id2)

    def test_record_tx_invalid_hex_returns_none(self):
        fp = self.tracer.record_tx("alice", "ZZZZ", 0.0)
        self.assertIsNone(fp)

    def test_empty_tracer_has_no_traces(self):
        self.assertEqual(len(self.tracer.traces), 0)

    def test_single_tx_creates_one_trace(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.assertEqual(len(self.tracer.traces), 1)

    def test_same_fingerprint_second_tx_no_new_trace(self):
        """Two TX events with the same payload → one trace (relay forwarding)."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.1)
        self.assertEqual(len(self.tracer.traces), 1)

    def test_different_payload_creates_separate_traces(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_tx("alice", _OTHER_MSG, 0.1)
        self.assertEqual(len(self.tracer.traces), 2)

    def test_first_sender_is_set_from_first_tx(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.1)
        traces = list(self.tracer.traces.values())
        self.assertEqual(traces[0].first_sender, "alice")

    def test_first_seen_at_is_first_tx_time(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 10.0)
        self.tracer.record_tx("relay1", _MSG_HOP1, 20.0)
        traces = list(self.tracer.traces.values())
        self.assertAlmostEqual(traces[0].first_seen_at, 10.0)


class TestPacketTracerHops(unittest.TestCase):

    def setUp(self):
        self.tracer = PacketTracer()

    def test_no_rx_means_no_hops(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        traces = list(self.tracer.traces.values())
        self.assertEqual(traces[0].witness_count, 0)
        self.assertEqual(traces[0].hops, [])

    def test_single_rx_creates_one_hop(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02)
        traces = list(self.tracer.traces.values())
        self.assertEqual(traces[0].witness_count, 1)
        self.assertEqual(traces[0].hops[0].sender, "alice")
        self.assertEqual(traces[0].hops[0].receiver, "relay1")

    def test_two_hops_same_packet(self):
        """alice→relay1→bob (flood): two distinct (sender, receiver) records."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.03)
        self.tracer.record_rx("relay1", "bob", _MSG_HOP1, 0.05)
        traces = list(self.tracer.traces.values())
        self.assertEqual(len(traces), 1)        # one logical packet
        self.assertEqual(traces[0].witness_count, 2)

    def test_flood_fan_out(self):
        """alice→relay1 and alice→bob both receive the same flood packet."""
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02,  tx_id)
        self.tracer.record_rx("alice", "bob",    _MSG_HOP0, 0.025, tx_id)
        traces = list(self.tracer.traces.values())
        self.assertEqual(traces[0].witness_count, 2)

    def test_flood_fan_out_hops_share_tx_id(self):
        """All deliveries from one broadcast have identical tx_id."""
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02,  tx_id)
        self.tracer.record_rx("alice", "bob",    _MSG_HOP0, 0.025, tx_id)
        hops = list(self.tracer.traces.values())[0].hops
        self.assertEqual(hops[0].tx_id, tx_id)
        self.assertEqual(hops[1].tx_id, tx_id)

    def test_different_tx_events_have_different_tx_ids(self):
        """alice TX and relay1 TX produce distinct tx_ids on their respective hops."""
        tx_id1 = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02, tx_id1)
        tx_id2 = self.tracer.record_tx("relay1", _MSG_HOP1, 0.03)
        self.tracer.record_rx("relay1", "bob", _MSG_HOP1, 0.05, tx_id2)
        hops = list(self.tracer.traces.values())[0].hops
        self.assertNotEqual(hops[0].tx_id, hops[1].tx_id)

    def test_rx_before_tx_handled_defensively(self):
        """record_rx for an unknown fingerprint should not crash."""
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.0)
        traces = list(self.tracer.traces.values())
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].witness_count, 1)

    def test_hop_records_path_count(self):
        """path_count in HopRecord reflects the header at TX time."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.02)
        self.tracer.record_rx("relay1", "bob", _MSG_HOP1, 0.03)

        traces = list(self.tracer.traces.values())
        hops = traces[0].hops
        self.assertEqual(hops[0].path_count, 0)  # alice transmitted with 0 hashes
        self.assertEqual(hops[1].path_count, 1)  # relay1 transmitted with 1 hash

    def test_hop_records_route_type(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        traces = list(self.tracer.traces.values())
        self.assertEqual(traces[0].hops[0].route_type, ROUTE_TYPE_FLOOD)


class TestPacketTracerWitnessProperties(unittest.TestCase):

    def setUp(self):
        self.tracer = PacketTracer()

    def _populate_flood(self):
        """Simulate a 3-hop flood: alice → relay1 → bob, plus a branch relay2 → carol."""
        self.tracer.record_tx("alice",  _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice",  "relay1", _MSG_HOP0, 0.01)
        self.tracer.record_rx("alice",  "relay2", _MSG_HOP0, 0.01)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.02)
        self.tracer.record_rx("relay1", "bob",   _MSG_HOP1, 0.03)
        self.tracer.record_tx("relay2", _MSG_HOP1, 0.02)
        self.tracer.record_rx("relay2", "carol", _MSG_HOP1, 0.03)

    def test_unique_senders(self):
        self._populate_flood()
        tr = list(self.tracer.traces.values())[0]
        self.assertSetEqual(tr.unique_senders, {"alice", "relay1", "relay2"})

    def test_unique_receivers(self):
        self._populate_flood()
        tr = list(self.tracer.traces.values())[0]
        self.assertSetEqual(tr.unique_receivers, {"relay1", "relay2", "bob", "carol"})

    def test_is_flood(self):
        self._populate_flood()
        tr = list(self.tracer.traces.values())[0]
        self.assertTrue(tr.is_flood())

    def test_is_not_flood_when_direct(self):
        self.tracer.record_tx("alice", _DIRECT_MSG, 0.0)
        self.tracer.record_rx("alice", "bob", _DIRECT_MSG, 0.01)
        tr = list(self.tracer.traces.values())[0]
        self.assertFalse(tr.is_flood())


class TestPacketTracerByType(unittest.TestCase):

    def test_traces_by_type_groups_correctly(self):
        tracer = PacketTracer()
        tracer.record_tx("a", _MSG_HOP0, 0.0)     # TXT_MSG
        tracer.record_tx("a", _OTHER_MSG, 0.0)    # TXT_MSG (different payload)
        tracer.record_tx("a", _ACK, 0.0)          # ACK
        by_type = tracer.traces_by_type()
        self.assertIn(PAYLOAD_TYPE_TXT_MSG, by_type)
        self.assertIn(PAYLOAD_TYPE_ACK,     by_type)
        self.assertEqual(len(by_type[PAYLOAD_TYPE_TXT_MSG]), 2)
        self.assertEqual(len(by_type[PAYLOAD_TYPE_ACK]),     1)


class TestPacketTracerReport(unittest.TestCase):

    def test_empty_report_contains_no_packets_line(self):
        tracer = PacketTracer()
        report = tracer.report()
        self.assertIn("no packets", report)

    def test_report_includes_summary_section(self):
        tracer = PacketTracer()
        tracer.record_tx("alice", _MSG_HOP0, 0.0)
        tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        report = tracer.report()
        self.assertIn("Unique packets", report)
        self.assertIn("Total deliveries", report)

    def test_report_includes_payload_type_names(self):
        tracer = PacketTracer()
        tracer.record_tx("alice", _MSG_HOP0, 0.0)
        report = tracer.report()
        self.assertIn("TXT_MSG", report)

    def test_report_includes_flood_direct_counts(self):
        tracer = PacketTracer()
        tracer.record_tx("alice", _MSG_HOP0, 0.0)
        tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        report = tracer.report()
        self.assertIn("Flood-routed", report)
        self.assertIn("Direct-routed", report)

    def test_report_is_string(self):
        tracer = PacketTracer()
        tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.assertIsInstance(tracer.report(), str)

    def test_report_shows_top_exposure_packets(self):
        tracer = PacketTracer()
        # One high-exposure packet
        tracer.record_tx("alice", _MSG_HOP0, 0.0)
        for dest in ["relay1", "relay2", "bob", "carol"]:
            tracer.record_rx("alice", dest, _MSG_HOP0, 0.01)
        report = tracer.report()
        self.assertIn("witnesses", report)
        self.assertIn("alice", report)


if __name__ == "__main__":
    unittest.main()
