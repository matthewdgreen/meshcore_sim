"""
channel.py — RF channel model for LoRa contention simulation.

Models simultaneous-transmission collisions at shared receivers using the
explicit RSSI values defined on topology edges.

Capture effect (default capture_threshold_db=6.0):
    If the primary signal arrives at least ``capture_threshold_db`` stronger
    than any interferer (comparing edge RSSI values at the receiver), it is
    still decoded correctly.  This matches empirically measured LoRa behaviour
    (Semtech AN1200.22, co-channel rejection ~6 dB for same-SF collisions).
"""

from __future__ import annotations


class ChannelModel:
    """
    Tracks active LoRa transmissions and detects collisions at receivers.

    Typical usage (called by PacketRouter)::

        channel.register_tx(sender, tx_start, tx_end, tx_id)
        # ... after propagation delay ...
        if channel.is_lost(sender, receiver, tx_start, tx_end, tx_id):
            return  # packet dropped due to collision
    """

    def __init__(
        self,
        link_rssi: dict[str, dict[str, float]],
        capture_threshold_db: float = 6.0,
    ) -> None:
        """
        Parameters
        ----------
        link_rssi
            ``{sender_name: {receiver_name: rssi_dBm}}``.  Built from
            topology edges; directional overrides are already resolved.
            Serves double duty: key existence implies reachability, and the
            value is used for capture-effect power comparison.
        capture_threshold_db
            Minimum power advantage (dB) for the primary signal to survive a
            collision (default 6 dB, consistent with LoRa datasheets).
        """
        self._link_rssi = link_rssi
        self._cap_db    = capture_threshold_db

        # tx_id → (sender_name, tx_start, tx_end)
        self._active: dict[int, tuple[str, float, float]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def register_tx(
        self, sender: str, tx_start: float, tx_end: float, tx_id: int
    ) -> None:
        """Record that *sender* is transmitting during ``[tx_start, tx_end]``."""
        self._active[tx_id] = (sender, tx_start, tx_end)

    def expire_before(self, t: float) -> None:
        """Discard TX records whose end time is earlier than *t*."""
        self._active = {k: v for k, v in self._active.items() if v[2] >= t}

    def is_receiver_busy(
        self, receiver: str, rx_start: float, rx_end: float
    ) -> bool:
        """Return ``True`` if *receiver* is transmitting during ``[rx_start, rx_end]``.

        LoRa is half-duplex: a node cannot receive while it is transmitting.
        """
        for _tx_id, (sender, start, end) in self._active.items():
            if sender != receiver:
                continue
            # Temporal overlap
            if start < rx_end and end > rx_start:
                return True
        return False

    def is_lost(
        self,
        primary_sender: str,
        receiver: str,
        tx_start: float,
        tx_end: float,
        tx_id: int,
    ) -> bool:
        """Return ``True`` if the packet is lost at *receiver* due to collision.

        A collision requires all three of:

        1. The interfering sender can reach *receiver* (has an edge with RSSI).
        2. The interfering TX window overlaps ``[tx_start, tx_end]``.
        3. The primary signal is not sufficiently stronger than the interferer
           (capture effect: primary RSSI - interferer RSSI >= threshold).
        """
        primary_rssi = self._link_rssi.get(primary_sender, {}).get(receiver)

        for other_id, (sender, start, end) in self._active.items():
            if other_id == tx_id:
                continue  # skip self
            if sender == primary_sender:
                continue  # same node, different packet

            # Temporal overlap check
            if start >= tx_end or end <= tx_start:
                continue

            # Spatial reachability: can the interferer reach this receiver?
            interferer_rssi = self._link_rssi.get(sender, {}).get(receiver)
            if interferer_rssi is None:
                continue

            # Capture effect: primary survives if sufficiently stronger
            if primary_rssi is not None:
                if primary_rssi - interferer_rssi >= self._cap_db:
                    continue  # primary captures

            return True  # collision — packet lost at this receiver

        return False
