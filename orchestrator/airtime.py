"""
airtime.py — LoRa on-air time calculation.

Formula reference: Semtech AN1200.13 "LoRa Modem Designer's Guide" §4.
All timing values are in milliseconds.
"""

from __future__ import annotations

import math


def lora_airtime_ms(
    sf: int,
    bw_hz: int,
    cr: int,
    payload_bytes: int,
    preamble_symbols: int = 8,
    crc: bool = True,
    explicit_header: bool = True,
) -> float:
    """Return the on-air time in milliseconds for a LoRa packet.

    Parameters
    ----------
    sf
        Spreading factor (7–12).
    bw_hz
        Bandwidth in Hz (e.g. 125_000, 250_000, 500_000).
    cr
        Coding-rate denominator offset: 1 = CR4/5, 2 = CR4/6,
        3 = CR4/7, 4 = CR4/8.
    payload_bytes
        Number of bytes in the MAC payload.
    preamble_symbols
        Number of preamble symbols (default 8, standard for LoRa).
    crc
        True if a CRC is appended (almost always True in MeshCore).
    explicit_header
        True if the LoRa explicit header is present (MeshCore default).
    """
    # Symbol duration in milliseconds
    t_sym_ms = (2 ** sf) / (bw_hz / 1000.0)

    # Preamble duration
    t_preamble_ms = (preamble_symbols + 4.25) * t_sym_ms

    # Low data-rate optimisation: mandatory when T_sym >= 16 ms
    # (SF11/SF12 at BW=125 kHz; SF12 at BW=250 kHz)
    de = 1 if t_sym_ms >= 16.0 else 0

    h        = 0 if explicit_header else 1
    crc_flag = 1 if crc else 0

    # Number of payload symbols (Semtech formula)
    numerator   = 8 * payload_bytes - 4 * sf + 28 + 16 * crc_flag - 20 * h
    denominator = 4 * (sf - 2 * de)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)

    t_payload_ms = payload_symbols * t_sym_ms
    return t_preamble_ms + t_payload_ms


# Typical MeshCore self-advert size.  Observed from node_agent TX logs:
# ~106–109 bytes (pub key 32B + advert data/name + Ed25519 signature +
# Packet header/path framing).  Use 110 as a representative upper bound.
_TYPICAL_ADVERT_BYTES = 110


def advert_stagger_secs(
    sf: int,
    bw_hz: int,
    cr: int,
    node_count: int,
    preamble_symbols: int = 8,
    margin: float = 2.0,
) -> float:
    """Compute a safe stagger window for initial advert floods.

    In a hard-collision RF model, two transmissions that overlap at a shared
    receiver destroy each other.  The stagger window must be wide enough for
    every node to transmit its advert AND for relays to forward received
    adverts without half-duplex collisions.  A margin of 2× accounts for the
    relay forwarding overhead; Listen-Before-Talk (LBT) in the node's
    Dispatcher provides additional collision avoidance at the MAC layer.

    This mimics reality: nodes in the field don't all power on at the same
    instant — they boot over seconds to minutes, naturally avoiding collisions.

    Returns the stagger window width in seconds.
    """
    airtime_ms = lora_airtime_ms(sf, bw_hz, cr, _TYPICAL_ADVERT_BYTES,
                                 preamble_symbols)
    return node_count * airtime_ms / 1000.0 * margin


# Typical MeshCore text message size (encrypted payload + framing).
# Overhead is ~11 bytes (packet header + encryption).  A typical 50-char
# real-world message produces ~60 bytes on the wire.
_TYPICAL_MSG_BYTES = 60


def flood_timeout_secs(
    sf: int,
    bw_hz: int,
    cr: int,
    preamble_symbols: int = 8,
    retries: int = 3,
) -> float:
    """Estimate the total ACK wait time for a flood-routed text message.

    Uses the real companion_radio formula:
        flood_timeout = SEND_TIMEOUT_BASE + FLOOD_SEND_TIMEOUT_FACTOR * airtime

    With *retries* additional attempts, total wait =
        (retries + 1) * flood_timeout.

    Returns the result in seconds.
    """
    airtime_ms = lora_airtime_ms(sf, bw_hz, cr, _TYPICAL_MSG_BYTES,
                                 preamble_symbols)
    single_timeout_ms = 500 + 16.0 * airtime_ms
    return (retries + 1) * single_timeout_ms / 1000.0
