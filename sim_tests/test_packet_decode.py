"""
test_packet_decode.py — Unit tests for orchestrator/packet.py.

Tests the wire-format decoder against hand-crafted byte sequences derived
from the MeshCore Packet.h / Packet.cpp wire format spec.  No node_agent
binary is required.

Wire format recap (from Packet::writeTo):
    [header][?transport_codes x4][path_len][path_bytes...][payload_bytes...]

    header byte layout:
        bits [1:0]  route_type  (0=TRANSPORT_FLOOD, 1=FLOOD, 2=DIRECT, 3=TRANSPORT_DIRECT)
        bits [5:2]  payload_type
        bits [7:6]  version

    path_len byte layout:
        bits [5:0]  hash_count  (number of relay hashes in path[])
        bits [7:6]  hash_size - 1  (0→1, 1→2, 2→3; 3→4 is reserved/invalid)
"""

from __future__ import annotations

import unittest

from orchestrator.packet import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_TXT_MSG,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
    PacketInfo,
    decode_packet,
    packet_fingerprint,
    payload_type_name,
    route_type_name,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal valid packet as hex
# ---------------------------------------------------------------------------

def _make_packet(
    route_type: int = ROUTE_TYPE_FLOOD,
    payload_type: int = PAYLOAD_TYPE_ADVERT,
    version: int = 0,
    path_count: int = 0,
    path_size: int = 1,
    path_bytes: bytes = b"",
    payload: bytes = b"\xAA\xBB\xCC",
    transport_codes: bytes = b"",
) -> str:
    header = (route_type & 0x03) | ((payload_type & 0x0F) << 2) | ((version & 0x03) << 6)
    path_len_byte = (path_count & 0x3F) | (((path_size - 1) & 0x03) << 6)
    raw = bytes([header]) + transport_codes + bytes([path_len_byte]) + path_bytes + payload
    return raw.hex()


# ---------------------------------------------------------------------------
# Decode tests
# ---------------------------------------------------------------------------

class TestDecodeBasic(unittest.TestCase):

    def test_simple_flood_advert(self):
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_ADVERT,
            path_count=0,
            payload=b"\x01\x02\x03",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.route_type, ROUTE_TYPE_FLOOD)
        self.assertEqual(info.payload_type, PAYLOAD_TYPE_ADVERT)
        self.assertEqual(info.version, 0)
        self.assertEqual(info.path_count, 0)
        self.assertEqual(info.path_size, 1)
        self.assertEqual(info.path_bytes, b"")
        self.assertEqual(info.payload, b"\x01\x02\x03")

    def test_simple_direct_txt(self):
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_DIRECT,
            payload_type=PAYLOAD_TYPE_TXT_MSG,
            path_count=2,
            path_size=1,
            path_bytes=b"\xAA\xBB",
            payload=b"\xDE\xAD\xBE\xEF",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.route_type, ROUTE_TYPE_DIRECT)
        self.assertEqual(info.payload_type, PAYLOAD_TYPE_TXT_MSG)
        self.assertEqual(info.path_count, 2)
        self.assertEqual(info.path_size, 1)
        self.assertEqual(info.path_bytes, b"\xAA\xBB")
        self.assertEqual(info.payload, b"\xDE\xAD\xBE\xEF")

    def test_ack_packet(self):
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_ACK,
            path_count=0,
            payload=b"\x12\x34\x56\x78",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.payload_type, PAYLOAD_TYPE_ACK)
        self.assertEqual(info.payload, b"\x12\x34\x56\x78")

    def test_version_bits_preserved(self):
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_ADVERT,
            version=2,
            payload=b"\xFF",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.version, 2)

    def test_empty_payload_allowed(self):
        """A packet with no payload bytes is valid (path only)."""
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_ADVERT,
            path_count=0,
            payload=b"",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.payload, b"")


class TestDecodePathSizes(unittest.TestCase):
    """path_size can be 1, 2, or 3 bytes per hash entry."""

    def _check(self, path_size: int, path_count: int):
        path_bytes = bytes(range(path_size * path_count))
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_ADVERT,
            path_count=path_count,
            path_size=path_size,
            path_bytes=path_bytes,
            payload=b"\x99",
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info, f"path_size={path_size}")
        self.assertEqual(info.path_size, path_size)
        self.assertEqual(info.path_count, path_count)
        self.assertEqual(info.path_bytes, path_bytes)
        self.assertEqual(info.payload, b"\x99")

    def test_size1_count0(self):  self._check(1, 0)
    def test_size1_count3(self):  self._check(1, 3)
    def test_size2_count0(self):  self._check(2, 0)
    def test_size2_count2(self):  self._check(2, 2)
    def test_size3_count0(self):  self._check(3, 0)
    def test_size3_count1(self):  self._check(3, 1)


class TestDecodeTransportCodes(unittest.TestCase):

    def test_transport_flood_has_codes(self):
        transport = b"\x01\x02\x03\x04"
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_TRANSPORT_FLOOD,
            payload_type=PAYLOAD_TYPE_ADVERT,
            path_count=0,
            payload=b"\xAB\xCD",
            transport_codes=transport,
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.route_type, ROUTE_TYPE_TRANSPORT_FLOOD)
        # The payload should still decode correctly after skipping transport codes
        self.assertEqual(info.payload, b"\xAB\xCD")

    def test_transport_direct_has_codes(self):
        transport = b"\xAA\xBB\xCC\xDD"
        hex_data = _make_packet(
            route_type=ROUTE_TYPE_TRANSPORT_DIRECT,
            payload_type=PAYLOAD_TYPE_TXT_MSG,
            path_count=1,
            path_size=1,
            path_bytes=b"\x77",
            payload=b"\x11\x22",
            transport_codes=transport,
        )
        info = decode_packet(hex_data)
        self.assertIsNotNone(info)
        self.assertEqual(info.route_type, ROUTE_TYPE_TRANSPORT_DIRECT)
        self.assertEqual(info.path_bytes, b"\x77")
        self.assertEqual(info.payload, b"\x11\x22")


class TestDecodeInvalid(unittest.TestCase):

    def test_none_on_empty_hex(self):
        self.assertIsNone(decode_packet(""))

    def test_none_on_single_byte(self):
        self.assertIsNone(decode_packet("11"))

    def test_none_on_invalid_hex(self):
        self.assertIsNone(decode_packet("ZZZZ"))

    def test_none_on_reserved_path_size(self):
        """path_len with upper bits = 0b11 (hash_size = 4) is reserved/invalid."""
        # path_len_byte = 0b11_000001 = 0xC1 → hash_size = 4 (reserved), count = 1
        raw = bytes([
            0x11,   # header: FLOOD | ADVERT
            0xC1,   # path_len: reserved size (4), count=1
            0xFF,   # one would-be path byte
            0xAA,   # payload
        ])
        self.assertIsNone(decode_packet(raw.hex()))

    def test_none_when_path_bytes_truncated(self):
        """path_len claims more path bytes than are present."""
        # header=FLOOD|ADVERT, path_len=2 hashes of size 1, but only provide 1 path byte
        raw = bytes([
            0x11,   # header
            0x02,   # path_len: size=1, count=2  → needs 2 path bytes
            0xAA,   # only 1 path byte present (should be 2)
            0xBB,   # this is payload, but decoder should fail before reaching it
        ])
        # After consuming header (1) + path_len (1) + 2 path bytes → but only 2 bytes
        # left (0xAA, 0xBB).  Actually 0xAA is the first path byte and 0xBB is the
        # second, so the decode should succeed with empty payload.
        # Let's instead use count=3:
        raw = bytes([
            0x11,   # header
            0x03,   # path_len: size=1, count=3  → needs 3 path bytes
            0xAA,   # only 1 path byte
        ])
        self.assertIsNone(decode_packet(raw.hex()))

    def test_none_when_transport_codes_truncated(self):
        """TRANSPORT_FLOOD but fewer than 4 transport code bytes."""
        raw = bytes([
            ROUTE_TYPE_TRANSPORT_FLOOD | (PAYLOAD_TYPE_ADVERT << 2),  # header
            0x01, 0x02,   # only 2 transport code bytes (need 4)
        ])
        self.assertIsNone(decode_packet(raw.hex()))

    def test_none_when_no_path_len_byte(self):
        """Transport codes eat all remaining bytes; no path_len byte."""
        raw = bytes([
            ROUTE_TYPE_TRANSPORT_FLOOD | (PAYLOAD_TYPE_ADVERT << 2),
            0x01, 0x02, 0x03, 0x04,   # transport codes — fine
            # no path_len byte follows
        ])
        self.assertIsNone(decode_packet(raw.hex()))


# ---------------------------------------------------------------------------
# Fingerprint tests
# ---------------------------------------------------------------------------

class TestFingerprint(unittest.TestCase):

    def _info(self, payload_type: int, payload: bytes,
               path_count: int = 0, path_bytes: bytes = b"") -> PacketInfo:
        return PacketInfo(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=payload_type,
            version=0,
            path_count=path_count,
            path_size=1,
            path_bytes=path_bytes,
            payload=payload,
        )

    def test_same_payload_same_fingerprint(self):
        a = self._info(PAYLOAD_TYPE_TXT_MSG, b"\xDE\xAD\xBE\xEF", path_count=0)
        b = self._info(PAYLOAD_TYPE_TXT_MSG, b"\xDE\xAD\xBE\xEF", path_count=2,
                       path_bytes=b"\xAA\xBB")
        # Path changes per hop, but payload is the same → fingerprint matches
        self.assertEqual(packet_fingerprint(a), packet_fingerprint(b))

    def test_different_payload_different_fingerprint(self):
        a = self._info(PAYLOAD_TYPE_TXT_MSG, b"\xDE\xAD\xBE\xEF")
        b = self._info(PAYLOAD_TYPE_TXT_MSG, b"\xDE\xAD\xBE\xEE")
        self.assertNotEqual(packet_fingerprint(a), packet_fingerprint(b))

    def test_different_type_different_fingerprint(self):
        """Same payload bytes but different payload_type → different fingerprint."""
        a = self._info(PAYLOAD_TYPE_TXT_MSG,  b"\xAA\xBB\xCC")
        b = self._info(PAYLOAD_TYPE_ADVERT, b"\xAA\xBB\xCC")
        self.assertNotEqual(packet_fingerprint(a), packet_fingerprint(b))

    def test_fingerprint_is_hex_string(self):
        a = self._info(PAYLOAD_TYPE_ADVERT, b"\x01\x02\x03")
        fp = packet_fingerprint(a)
        # Must be a valid hex string
        bytes.fromhex(fp)   # would raise if not

    def test_fingerprint_length(self):
        """Length = 2 hex chars for type byte + 2 * len(payload)."""
        payload = b"\xAA\xBB\xCC\xDD"
        a = self._info(PAYLOAD_TYPE_TXT_MSG, payload)
        fp = packet_fingerprint(a)
        expected_len = 2 + 2 * len(payload)   # 1 type byte + payload bytes
        self.assertEqual(len(fp), expected_len)

    def test_roundtrip_via_decode(self):
        """decode_packet → packet_fingerprint is consistent for the same logical packet."""
        payload = b"\x01\x02\x03\x04\x05"
        # Packet at hop 0 (no relay hashes yet)
        hex0 = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_TXT_MSG,
            path_count=0,
            path_bytes=b"",
            payload=payload,
        )
        # Same packet at hop 1 (one relay hash appended)
        hex1 = _make_packet(
            route_type=ROUTE_TYPE_FLOOD,
            payload_type=PAYLOAD_TYPE_TXT_MSG,
            path_count=1,
            path_size=1,
            path_bytes=b"\xFF",
            payload=payload,
        )
        info0 = decode_packet(hex0)
        info1 = decode_packet(hex1)
        self.assertIsNotNone(info0)
        self.assertIsNotNone(info1)
        self.assertEqual(packet_fingerprint(info0), packet_fingerprint(info1))


# ---------------------------------------------------------------------------
# Name helper tests
# ---------------------------------------------------------------------------

class TestNameHelpers(unittest.TestCase):

    def test_known_route_types(self):
        self.assertEqual(route_type_name(ROUTE_TYPE_FLOOD),            "FLOOD")
        self.assertEqual(route_type_name(ROUTE_TYPE_DIRECT),           "DIRECT")
        self.assertEqual(route_type_name(ROUTE_TYPE_TRANSPORT_FLOOD),  "TRANSPORT_FLOOD")
        self.assertEqual(route_type_name(ROUTE_TYPE_TRANSPORT_DIRECT), "TRANSPORT_DIRECT")

    def test_unknown_route_type(self):
        # All 2-bit values are defined, but test the fallback just in case
        name = route_type_name(99)
        self.assertIn("UNKNOWN", name)

    def test_known_payload_types(self):
        self.assertEqual(payload_type_name(PAYLOAD_TYPE_TXT_MSG), "TXT_MSG")
        self.assertEqual(payload_type_name(PAYLOAD_TYPE_ADVERT),  "ADVERT")
        self.assertEqual(payload_type_name(PAYLOAD_TYPE_ACK),     "ACK")

    def test_unknown_payload_type(self):
        name = payload_type_name(0xC)
        self.assertIn("UNKNOWN", name)


if __name__ == "__main__":
    unittest.main()
