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
from orchestrator.tracer import CollisionRecord, PacketTracer


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


class TestPacketTracerCollisions(unittest.TestCase):

    def setUp(self):
        self.tracer = PacketTracer()

    def test_no_collisions_by_default(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(tr.collisions, [])

    def test_record_collision_adds_collision_record(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05, tx_id)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(len(tr.collisions), 1)
        c = tr.collisions[0]
        self.assertEqual(c.sender, "alice")
        self.assertEqual(c.receiver, "relay1")
        self.assertEqual(c.tx_id, tx_id)
        self.assertAlmostEqual(c.t, 0.05)

    def test_collision_is_instance_of_collision_record(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05, tx_id)
        tr = list(self.tracer.traces.values())[0]
        self.assertIsInstance(tr.collisions[0], CollisionRecord)

    def test_multiple_collisions_same_packet(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05, tx_id)
        self.tracer.record_collision("alice", "relay2", _MSG_HOP0, 0.06, tx_id)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(len(tr.collisions), 2)

    def test_collision_with_no_prior_tx_creates_trace_defensively(self):
        """record_collision before record_tx should not crash."""
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05)
        self.assertEqual(len(self.tracer.traces), 1)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(len(tr.collisions), 1)

    def test_collision_invalid_hex_does_not_crash(self):
        self.tracer.record_collision("alice", "relay1", "ZZZZ", 0.05)
        self.assertEqual(len(self.tracer.traces), 0)

    def test_collision_does_not_affect_witness_count(self):
        """A collision is a failed delivery — should not increment witness_count."""
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02, tx_id)
        self.tracer.record_collision("alice", "relay2", _MSG_HOP0, 0.02, tx_id)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(tr.witness_count, 1)   # only relay1 actually received it

    def test_collision_is_separate_from_hops(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.02, tx_id)
        self.tracer.record_collision("alice", "relay2", _MSG_HOP0, 0.02, tx_id)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(len(tr.hops), 1)
        self.assertEqual(len(tr.collisions), 1)

    def test_to_dict_includes_collisions_key(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05, tx_id)
        d = self.tracer.to_dict()
        pkt = d["packets"][0]
        self.assertIn("collisions", pkt)

    def test_to_dict_collision_fields(self):
        tx_id = self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_collision("alice", "relay1", _MSG_HOP0, 0.05, tx_id)
        d = self.tracer.to_dict()
        c = d["packets"][0]["collisions"][0]
        self.assertEqual(c["sender"],   "alice")
        self.assertEqual(c["receiver"], "relay1")
        self.assertEqual(c["tx_id"],    tx_id)
        self.assertAlmostEqual(c["t"],  0.05)

    def test_to_dict_schema_version_3(self):
        d = self.tracer.to_dict()
        self.assertEqual(d["schema_version"], 3)

    def test_to_dict_empty_collisions_list_when_none(self):
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        d = self.tracer.to_dict()
        self.assertEqual(d["packets"][0]["collisions"], [])


class TestPacketSizeTracking(unittest.TestCase):
    """
    size_bytes is the wire-format byte count of each packet copy at the hop
    where it was received.  For flood packets this grows by one byte (per 1-byte
    relay hash) each time a relay retransmits.  For direct packets the size is
    fixed.
    """

    def setUp(self):
        self.tracer = PacketTracer()

    # ---- HopRecord.size_bytes ----

    def test_hop_size_bytes_is_nonzero_after_rx(self):
        """A recorded hop must have a positive size_bytes."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        hop = list(self.tracer.traces.values())[0].hops[0]
        self.assertGreater(hop.size_bytes, 0)

    def test_hop_size_bytes_equals_hex_length_divided_by_two(self):
        """size_bytes must equal len(hex_data) // 2 for the received packet."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        hop = list(self.tracer.traces.values())[0].hops[0]
        self.assertEqual(hop.size_bytes, len(_MSG_HOP0) // 2)

    def test_flood_packet_grows_by_one_byte_per_relay(self):
        """
        Each relay appends a 1-byte hash to the path field; successive hops
        of the same flood packet must increase by one byte.
        """
        # hop0: alice sends 6-byte packet (header + path_len + 4-byte payload)
        # hop1: relay1 sends 7-byte packet (one relay hash appended)
        # hop2: relay2 sends 8-byte packet (two relay hashes appended)
        self.tracer.record_tx("alice",  _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice",  "relay1", _MSG_HOP0, 0.01)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.02)
        self.tracer.record_rx("relay1", "relay2", _MSG_HOP1, 0.03)
        self.tracer.record_tx("relay2", _MSG_HOP2, 0.04)
        self.tracer.record_rx("relay2", "bob",    _MSG_HOP2, 0.05)

        hops = list(self.tracer.traces.values())[0].hops
        self.assertEqual(len(hops), 3)
        # Each successive hop must be exactly 1 byte larger.
        self.assertEqual(hops[1].size_bytes, hops[0].size_bytes + 1)
        self.assertEqual(hops[2].size_bytes, hops[1].size_bytes + 1)

    def test_direct_packet_size_is_fixed_across_hops(self):
        """
        A direct-routed packet doesn't accumulate relay hashes; every hop
        that receives it should have the same size.
        """
        # Transmit the direct message twice simulating two receivers.
        tx_id = self.tracer.record_tx("alice", _DIRECT_MSG, 0.0)
        self.tracer.record_rx("alice", "relay1", _DIRECT_MSG, 0.01, tx_id)
        self.tracer.record_rx("alice", "bob",    _DIRECT_MSG, 0.02, tx_id)

        hops = list(self.tracer.traces.values())[0].hops
        self.assertEqual(hops[0].size_bytes, hops[1].size_bytes)

    # ---- PacketTrace.avg_size_bytes ----

    def test_avg_size_bytes_is_zero_with_no_hops(self):
        """avg_size_bytes must return 0.0 when no hops have been recorded."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        tr = list(self.tracer.traces.values())[0]
        self.assertEqual(tr.avg_size_bytes, 0.0)

    def test_avg_size_bytes_single_hop(self):
        """avg_size_bytes of a single hop equals that hop's size."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        tr = list(self.tracer.traces.values())[0]
        self.assertAlmostEqual(tr.avg_size_bytes, len(_MSG_HOP0) // 2)

    def test_avg_size_bytes_three_hop_flood(self):
        """avg_size_bytes of a 3-hop flood is the mean of all three sizes."""
        self.tracer.record_tx("alice",  _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice",  "relay1", _MSG_HOP0, 0.01)
        self.tracer.record_tx("relay1", _MSG_HOP1, 0.02)
        self.tracer.record_rx("relay1", "relay2", _MSG_HOP1, 0.03)
        self.tracer.record_tx("relay2", _MSG_HOP2, 0.04)
        self.tracer.record_rx("relay2", "bob",    _MSG_HOP2, 0.05)

        tr = list(self.tracer.traces.values())[0]
        s0 = len(_MSG_HOP0) // 2  # 6
        s1 = len(_MSG_HOP1) // 2  # 7
        s2 = len(_MSG_HOP2) // 2  # 8
        expected = (s0 + s1 + s2) / 3
        self.assertAlmostEqual(tr.avg_size_bytes, expected)

    # ---- to_dict serialisation ----

    def test_to_dict_hop_includes_size_bytes(self):
        """Each hop record in to_dict() must contain a 'size_bytes' field."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        d = self.tracer.to_dict()
        hop = d["packets"][0]["hops"][0]
        self.assertIn("size_bytes", hop)
        self.assertEqual(hop["size_bytes"], len(_MSG_HOP0) // 2)

    def test_to_dict_packet_includes_avg_size_bytes(self):
        """Each packet record in to_dict() must contain an 'avg_size_bytes' field."""
        self.tracer.record_tx("alice", _MSG_HOP0, 0.0)
        self.tracer.record_rx("alice", "relay1", _MSG_HOP0, 0.01)
        d = self.tracer.to_dict()
        pkt = d["packets"][0]
        self.assertIn("avg_size_bytes", pkt)
        self.assertGreater(pkt["avg_size_bytes"], 0.0)


if __name__ == "__main__":
    unittest.main()
