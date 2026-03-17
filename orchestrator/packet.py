"""
packet.py — Wire-format decoder for MeshCore packets.

Mirrors the constants and encoding in MeshCore/src/Packet.h and Packet.cpp.
Pure Python, no C++ compilation required.

Wire format (from Packet::writeTo):
    byte 0:          header  (route_type[1:0] | payload_type[5:2] | ver[7:6])
    [bytes 1-4]:     transport_codes  (only present when route_type has TC flag)
    next byte:       path_len  (hash_count[5:0] | ((hash_size - 1) << 6))
    next N bytes:    path  (hash_count * hash_size bytes)
    remaining bytes: payload  (encrypted, stable across all hops for a given packet)

The payload bytes are the stable identity of a packet — they do NOT change as the
packet hops through the network.  The path field grows (flood) or shrinks (direct)
at each relay.  This lets us fingerprint packets for path tracing purely in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Route type constants  (header bits [1:0])
# ---------------------------------------------------------------------------
ROUTE_TYPE_TRANSPORT_FLOOD  = 0x00   # flood + transport codes
ROUTE_TYPE_FLOOD            = 0x01   # standard flood
ROUTE_TYPE_DIRECT           = 0x02   # direct (source-routed)
ROUTE_TYPE_TRANSPORT_DIRECT = 0x03   # direct + transport codes

_ROUTE_NAMES = {
    ROUTE_TYPE_TRANSPORT_FLOOD:  "TRANSPORT_FLOOD",
    ROUTE_TYPE_FLOOD:            "FLOOD",
    ROUTE_TYPE_DIRECT:           "DIRECT",
    ROUTE_TYPE_TRANSPORT_DIRECT: "TRANSPORT_DIRECT",
}

# ---------------------------------------------------------------------------
# Payload type constants  (header bits [5:2])
# ---------------------------------------------------------------------------
PAYLOAD_TYPE_REQ        = 0x00
PAYLOAD_TYPE_RESPONSE   = 0x01
PAYLOAD_TYPE_TXT_MSG    = 0x02
PAYLOAD_TYPE_ACK        = 0x03
PAYLOAD_TYPE_ADVERT     = 0x04
PAYLOAD_TYPE_GRP_TXT    = 0x05
PAYLOAD_TYPE_GRP_DATA   = 0x06
PAYLOAD_TYPE_ANON_REQ   = 0x07
PAYLOAD_TYPE_PATH       = 0x08
PAYLOAD_TYPE_TRACE      = 0x09
PAYLOAD_TYPE_MULTIPART  = 0x0A
PAYLOAD_TYPE_CONTROL    = 0x0B
PAYLOAD_TYPE_RAW_CUSTOM = 0x0F

_PAYLOAD_NAMES = {
    PAYLOAD_TYPE_REQ:        "REQ",
    PAYLOAD_TYPE_RESPONSE:   "RESPONSE",
    PAYLOAD_TYPE_TXT_MSG:    "TXT_MSG",
    PAYLOAD_TYPE_ACK:        "ACK",
    PAYLOAD_TYPE_ADVERT:     "ADVERT",
    PAYLOAD_TYPE_GRP_TXT:    "GRP_TXT",
    PAYLOAD_TYPE_GRP_DATA:   "GRP_DATA",
    PAYLOAD_TYPE_ANON_REQ:   "ANON_REQ",
    PAYLOAD_TYPE_PATH:       "PATH",
    PAYLOAD_TYPE_TRACE:      "TRACE",
    PAYLOAD_TYPE_MULTIPART:  "MULTIPART",
    PAYLOAD_TYPE_CONTROL:    "CONTROL",
    PAYLOAD_TYPE_RAW_CUSTOM: "RAW_CUSTOM",
}


def route_type_name(rt: int) -> str:
    return _ROUTE_NAMES.get(rt, f"UNKNOWN({rt:#x})")


def payload_type_name(pt: int) -> str:
    return _PAYLOAD_NAMES.get(pt, f"UNKNOWN({pt:#x})")


# ---------------------------------------------------------------------------
# Decoded packet
# ---------------------------------------------------------------------------

@dataclass
class PacketInfo:
    """Decoded fields from the MeshCore wire format."""
    route_type:   int    # one of ROUTE_TYPE_* constants
    payload_type: int    # one of PAYLOAD_TYPE_* constants
    version:      int    # PAYLOAD_VER_* (header bits [7:6])
    path_count:   int    # number of relay hashes currently in path[]
    path_size:    int    # bytes per hash entry (1, 2, or 3)
    path_bytes:   bytes  # raw path bytes (path_count * path_size)
    payload:      bytes  # encrypted payload — STABLE across all hops


def decode_packet(hex_data: str) -> Optional[PacketInfo]:
    """
    Decode a hex-encoded packet (as emitted in "tx" / "rx" events).

    Returns None if the data is too short or structurally invalid.
    The payload field is guaranteed to be identical for all copies of the
    same logical packet as it traverses the network.
    """
    try:
        raw = bytes.fromhex(hex_data)
    except ValueError:
        return None

    if len(raw) < 2:   # need at least header + path_len
        return None

    i = 0
    header = raw[i]; i += 1

    route_type   = header & 0x03
    payload_type = (header >> 2) & 0x0F
    version      = (header >> 6) & 0x03

    has_transport = route_type in (ROUTE_TYPE_TRANSPORT_FLOOD,
                                   ROUTE_TYPE_TRANSPORT_DIRECT)
    if has_transport:
        if i + 4 > len(raw):
            return None
        i += 4   # skip two 16-bit transport_codes

    if i >= len(raw):
        return None
    path_len_byte = raw[i]; i += 1

    path_count = path_len_byte & 0x3F           # lower 6 bits
    path_size  = (path_len_byte >> 6) + 1       # upper 2 bits + 1  (gives 1, 2, or 3)

    # Validate: reserved value 3 in upper 2 bits → hash_size = 4 (reserved, invalid)
    if path_size == 4:
        return None

    path_byte_len = path_count * path_size
    if i + path_byte_len > len(raw):
        return None
    path_bytes = raw[i: i + path_byte_len]; i += path_byte_len

    payload = raw[i:]   # everything after path is payload

    return PacketInfo(
        route_type=route_type,
        payload_type=payload_type,
        version=version,
        path_count=path_count,
        path_size=path_size,
        path_bytes=path_bytes,
        payload=payload,
    )


def packet_fingerprint(info: PacketInfo) -> str:
    """
    Return a stable string that identifies this logical packet regardless of
    which hop it is on.

    The fingerprint is:  hex(payload_type_byte || payload_bytes)

    This matches how MeshCore's own dedup table (calculatePacketHash) identifies
    packets — by payload type and payload content — which are both hop-invariant.
    """
    return bytes([info.payload_type]).hex() + info.payload.hex()
