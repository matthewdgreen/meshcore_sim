"""
test_privacy_baseline.py — Baseline privacy measurements for standard MeshCore routing.

These tests QUANTIFY the privacy exposure of the current protocol and are
EXPECTED TO PASS — they document the attack surface, not a solution.  When a
privacy-preserving protocol is later implemented, its measurements will be
compared against these baselines in a separate test module.

Metrics established here:
  1. Flood witness count      — how many (sender→receiver) pairs observe a message
  2. Fingerprint stability    — the encrypted payload is identical at every hop,
                                enabling cross-node correlation
  3. Path-count proximity     — the relay-hash counter in the packet header
                                increases with each hop, leaking distance to source
  4. Collusion gain           — what K passive relay observers can collectively infer
  5. Direct routing reduction — how much witness_count shrinks after path exchange

Topology: 3×3 grid (9 nodes), zero link loss.
  SOURCE   = n_0_0  (endpoint)
  DEST     = n_2_2  (endpoint)
  RELAYS   = all 7 interior nodes

  n_0_0 ── n_0_1 ── n_0_2
    |         |         |
  n_1_0 ── n_1_1 ── n_1_2
    |         |         |
  n_2_0 ── n_2_1 ── n_2_2
"""

from __future__ import annotations

import asyncio
import random
import unittest

from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.packet import PAYLOAD_TYPE_TXT_MSG
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.tracer import PacketTracer
from orchestrator.traffic import TrafficGenerator
from sim_tests.helpers import SKIP_IF_NO_BINARY, grid_topo_config


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_ROWS, _COLS = 3, 3
_SRC = "n_0_0"
_DST = "n_2_2"
# All relay node names in the 3×3 grid (everyone except src and dst)
_RELAYS = frozenset(
    f"n_{r}_{c}"
    for r in range(_ROWS)
    for c in range(_COLS)
    if (r, c) not in ((0, 0), (_ROWS - 1, _COLS - 1))
)
# Direct neighbours of the source
_SRC_NEIGHBOURS = frozenset({"n_0_1", "n_1_0"})


# ---------------------------------------------------------------------------
# Async simulation runner
# ---------------------------------------------------------------------------

async def _run_privacy_sim(
    *,
    rounds: int = 1,
    warmup_secs: float = 10.0,
    settle_secs: float = 5.0,
    seed: int = 42,
) -> tuple[dict[str, NodeAgent], PacketTracer]:
    """
    Bring up a 3×3 zero-loss grid, flood adverts during warmup, then send
    `rounds` TXT_MSG messages:

      Round 1: _SRC → _DST  (flood, because nodes have not yet exchanged paths)
      Round 2: _SRC → _DST  (direct, after path exchange triggered by round 1)

    Returns (agents, tracer).
    """
    topo_cfg = grid_topo_config(
        _ROWS, _COLS,
        warmup_secs=warmup_secs,
        duration_secs=9999.0,           # manual timing
        traffic_interval_secs=9999.0,   # no auto-traffic
        advert_interval_secs=9999.0,    # no auto-adverts
        seed=seed,
    )
    for e in topo_cfg.edges:
        e.loss = 0.0   # zero loss → fully deterministic flood coverage

    rng     = random.Random(seed)
    tracer  = PacketTracer()
    metrics = MetricsCollector()
    topology = Topology(topo_cfg)

    agents: dict[str, NodeAgent] = {
        n.name: NodeAgent(n, topo_cfg.simulation, radio=topo_cfg.radio)
        for n in topo_cfg.nodes
    }
    await asyncio.gather(*(a.start() for a in agents.values()))
    await asyncio.gather(*(a.wait_ready(timeout=15.0) for a in agents.values()))

    PacketRouter(topology, agents, metrics, rng, tracer=tracer)
    traffic = TrafficGenerator(agents, topology, topo_cfg.simulation, metrics, rng)

    await traffic.run_initial_adverts()
    await asyncio.sleep(warmup_secs)

    dst_pub = agents[_DST].state.pub_key
    src_pub = agents[_SRC].state.pub_key

    for _ in range(rounds):
        await agents[_SRC].send_text(dst_pub, "privacy-test")
        await asyncio.sleep(settle_secs)

    await asyncio.gather(*(a.quit() for a in agents.values()), return_exceptions=True)
    return agents, tracer


def _txt_traces(tracer: PacketTracer):
    return [
        tr for tr in tracer.traces.values()
        if tr.payload_type == PAYLOAD_TYPE_TXT_MSG
    ]


# ---------------------------------------------------------------------------
# 1. Flood exposure baseline
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestFloodExposureBaseline(unittest.TestCase):
    """
    A single flood TXT_MSG in a zero-loss 3×3 grid.
    Tests quantify WHAT AN ADVERSARY CAN OBSERVE from standard flood routing.
    """

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.tracer = asyncio.run(
            _run_privacy_sim(rounds=1, warmup_secs=15.0, settle_secs=20.0)
        )
        traces = _txt_traces(cls.tracer)
        assert traces, f"No TXT_MSG observed. Tracer report:\n{cls.tracer.report()}"
        cls.flood_trace = traces[0]

    # -- Coverage ----------------------------------------------------------

    def test_flood_reaches_destination(self):
        self.assertIn(
            _DST, self.flood_trace.unique_receivers,
            f"Flood did not reach destination {_DST}",
        )

    def test_flood_witness_count_exceeds_shortest_path(self):
        """In a 3×3 grid the shortest path is 4 hops; flood should reach far more."""
        self.assertGreater(
            self.flood_trace.witness_count, 4,
            f"witness_count={self.flood_trace.witness_count}; expected >4 for a 9-node flood",
        )

    def test_all_relay_nodes_observe_flood(self):
        """
        In a zero-loss network every relay node forwards the flood,
        making each a capable passive observer.
        """
        observers = self.flood_trace.unique_senders | self.flood_trace.unique_receivers
        missing = _RELAYS - observers
        self.assertEqual(
            missing, set(),
            f"Relay nodes NOT in observer set: {missing}\n"
            f"Tracer report:\n{self.tracer.report()}",
        )

    # -- Fingerprint stability (correlation attack) -------------------------

    def test_flood_fingerprint_is_non_empty(self):
        """Sanity check: the packet was decoded and fingerprinted."""
        self.assertTrue(self.flood_trace.fingerprint)

    def test_flood_multiple_distinct_senders_share_fingerprint(self):
        """
        Multiple relay nodes retransmitted the SAME encrypted payload.
        An adversary with two colluding relays can trivially correlate
        their observations — same bytes = same message.
        """
        self.assertGreaterEqual(
            len(self.flood_trace.unique_senders), 2,
            "Expected ≥2 distinct senders for the flood packet",
        )

    # -- Path-count proximity leakage --------------------------------------

    def test_path_count_grows_with_hop_distance(self):
        """
        The relay-hash counter in the packet header accumulates as the
        packet hops outward.  Observing a low path_count reveals proximity
        to the source — a structural privacy leak in the current protocol.
        """
        counts = [h.path_count for h in self.flood_trace.hops]
        self.assertGreater(
            max(counts), min(counts),
            f"All hops have the same path_count {counts[0]}; "
            f"expected variation as packet fans out from source",
        )

    def test_source_transmits_with_zero_path_count(self):
        """
        The originating node has no relay hashes yet, so it transmits
        with path_count == 0.  Any node receiving path_count == 0
        knows it is one radio hop from the source.
        """
        min_count = min(h.path_count for h in self.flood_trace.hops)
        self.assertEqual(
            min_count, 0,
            "Expected at least one hop with path_count=0 (source transmission)",
        )

    def test_source_neighbours_receive_zero_path_count(self):
        """
        Nodes one hop from the source receive path_count == 0,
        allowing them to bound the source to their immediate neighbourhood.
        """
        zero_count_receivers = {
            h.receiver for h in self.flood_trace.hops if h.path_count == 0
        }
        overlap = zero_count_receivers & _SRC_NEIGHBOURS
        self.assertTrue(
            overlap,
            f"Expected source neighbours {_SRC_NEIGHBOURS} among zero-path-count "
            f"receivers; got {zero_count_receivers}",
        )


# ---------------------------------------------------------------------------
# 2. Collusion attack
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestCollusionAttack(unittest.TestCase):
    """
    Quantify what K colluding passive relay nodes can infer.

    In the current protocol:
      - Even a SINGLE relay positioned anywhere on the flood path sees
        the full encrypted payload and can identify the message fingerprint.
      - Two colluding nodes immediately know they saw the same message.
      - ALL relays together cover the entire network — no path is safe.
    """

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.tracer = asyncio.run(
            _run_privacy_sim(rounds=1, seed=7, warmup_secs=15.0, settle_secs=20.0)
        )
        traces = _txt_traces(cls.tracer)
        assert traces, f"No TXT_MSG observed. Tracer report:\n{cls.tracer.report()}"
        cls.flood_trace = traces[0]

    def _observed_by(self, node: str) -> bool:
        tr = self.flood_trace
        return node in tr.unique_senders or node in tr.unique_receivers

    # -- Single observer ---------------------------------------------------

    def test_central_relay_observes_flood(self):
        """n_1_1 (hub of the 3×3 grid) is on every path and sees every flood."""
        self.assertTrue(
            self._observed_by("n_1_1"),
            "Central relay n_1_1 did not observe the flood",
        )

    def test_source_neighbour_observes_flood(self):
        """A relay one hop from the source observes the packet with path_count=0."""
        for neighbour in _SRC_NEIGHBOURS:
            with self.subTest(neighbour=neighbour):
                self.assertTrue(
                    self._observed_by(neighbour),
                    f"Source neighbour {neighbour} did not observe the flood",
                )

    # -- Two colluding observers -------------------------------------------

    def test_two_observers_share_identical_fingerprint(self):
        """
        Any two relay nodes that both forwarded the flood saw IDENTICAL
        payload bytes.  Two colluding parties comparing observations
        immediately confirm they saw the same logical message.
        """
        senders = list(self.flood_trace.unique_senders)
        self.assertGreaterEqual(
            len(senders), 2,
            f"Need ≥2 senders to demonstrate correlation; got {senders}",
        )
        # Both senders appear in the SAME PacketTrace → identical fingerprint.
        # If fingerprints differed, the tracer would have created two traces.
        self.assertIsNotNone(self.flood_trace.fingerprint)

    def test_two_observers_can_infer_source_direction(self):
        """
        The observer with the lower path_count is closer to the source.
        Two colluding nodes can compare path_count values to triangulate origin.
        """
        hop_by_receiver: dict[str, list[int]] = {}
        for h in self.flood_trace.hops:
            hop_by_receiver.setdefault(h.receiver, []).append(h.path_count)

        # Source neighbours should have lower min path_count than far nodes
        near_counts = [
            min(hop_by_receiver[n])
            for n in _SRC_NEIGHBOURS
            if n in hop_by_receiver
        ]
        far_nodes = {"n_2_0", "n_2_1", "n_0_2"}   # away from source corner
        far_counts = [
            min(hop_by_receiver[n])
            for n in far_nodes
            if n in hop_by_receiver
        ]
        if near_counts and far_counts:
            self.assertLessEqual(
                min(near_counts), min(far_counts),
                "Expected near-source nodes to see lower path_count than far nodes",
            )

    # -- Full collusion (all relays) ----------------------------------------

    def test_all_relays_colluding_covers_every_hop(self):
        """
        If every relay cooperates, their union of observations covers
        every hop of the flood — there is NO location in the network
        where a message can pass unseen.
        """
        observers = self.flood_trace.unique_senders | self.flood_trace.unique_receivers
        relay_observers = observers & _RELAYS
        self.assertEqual(
            relay_observers, _RELAYS,
            f"Relay nodes outside observer set: {_RELAYS - relay_observers}\n"
            f"Tracer report:\n{self.tracer.report()}",
        )

    def test_flood_node_coverage_fraction(self):
        """In a zero-loss network, ≥80% of nodes observe every flood."""
        total = _ROWS * _COLS
        observers = self.flood_trace.unique_senders | self.flood_trace.unique_receivers
        coverage = len(observers) / total
        self.assertGreaterEqual(
            coverage, 0.80,
            f"Flood coverage {coverage:.0%} is below 80% in a zero-loss network",
        )


# ---------------------------------------------------------------------------
# 3. Direct routing as a privacy improvement (baseline comparison)
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestDirectRoutingPrivacyReduction(unittest.TestCase):
    """
    Measure the privacy improvement from path-exchange direct routing.

    After the first flood triggers path exchange, subsequent messages travel
    directly and produce far fewer witnesses.  This establishes the UPPER BOUND
    on improvement achievable by direct routing alone (without any encryption change).
    """

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.tracer = asyncio.run(
            _run_privacy_sim(rounds=2, warmup_secs=15.0, settle_secs=20.0)
        )
        cls.txt_traces = _txt_traces(cls.tracer)
        # Separate flood and direct traces (retransmits may add extra floods)
        from orchestrator.packet import ROUTE_TYPE_DIRECT
        cls.flood_traces = [tr for tr in cls.txt_traces if tr.is_flood()]
        cls.direct_traces = [tr for tr in cls.txt_traces
                             if any(h.route_type == ROUTE_TYPE_DIRECT for h in tr.hops)]

    def test_two_txt_messages_observed(self):
        self.assertGreaterEqual(
            len(self.txt_traces), 2,
            f"Expected ≥2 TXT_MSG (flood + direct). Report:\n{self.tracer.report()}",
        )

    def test_first_message_is_flood(self):
        self.assertTrue(
            self.txt_traces[0].is_flood(),
            "First message should be flood-routed",
        )

    def test_direct_has_fewer_witnesses_than_flood(self):
        """The key privacy improvement: direct routing reduces witness_count."""
        if not self.direct_traces:
            self.fail("No direct-routed messages found")
        flood_wc  = self.flood_traces[0].witness_count
        direct_wc = self.direct_traces[0].witness_count
        self.assertLess(
            direct_wc, flood_wc,
            f"Expected direct witness_count < flood witness_count; "
            f"flood={flood_wc}, direct={direct_wc}",
        )

    def test_direct_witness_count_bounded(self):
        """
        A direct-routed packet can be witnessed at most once per radio link
        (edge) in the grid.  For a 3×3 grid there are 12 edges, so
        witness_count must not exceed that structural maximum.

        Note: even with direct routing each relay broadcasts to ALL its radio
        neighbours (not just the next hop), so the count can approach the
        edge count in a zero-loss network — the meaningful improvement is
        the reduction RATIO relative to flood, not an absolute number.
        """
        if not self.direct_traces:
            self.fail("No direct-routed messages found")
        # Edges in an R×C grid: R*(C-1) horizontal + (R-1)*C vertical
        max_edges = _ROWS * (_COLS - 1) + (_ROWS - 1) * _COLS
        direct_wc = self.direct_traces[0].witness_count
        self.assertLessEqual(
            direct_wc, max_edges,
            f"Direct witness_count={direct_wc} exceeds total grid edges={max_edges}",
        )

    def test_direct_still_exposes_path_nodes(self):
        """
        Direct routing does NOT eliminate privacy risk — relay nodes on the
        direct path still see the packet (with a stable fingerprint).
        This test confirms residual exposure exists even after path exchange.
        """
        if not self.direct_traces:
            self.fail("No direct-routed messages found")
        direct_tr = self.direct_traces[0]
        # At least one relay node must appear as sender (it forwarded the packet)
        relay_senders = direct_tr.unique_senders & _RELAYS
        self.assertTrue(
            relay_senders,
            "Expected at least one relay to forward the direct message; "
            "if the path is 0 hops this test should be revisited",
        )

    def test_flood_witness_reduction_ratio(self):
        """
        Quantifies the improvement: direct routing should achieve a measurable
        reduction in witness_count relative to the flood.

        Observed baseline: flood~22, direct~12-14, ratio~1.6-1.8x.
        Direct routing alone is insufficient for privacy (PLAN.md §4), so we
        assert a modest >=1.2x reduction rather than a hard 2x target.  The
        meaningful benchmark is that improvement exists; how much is tracked
        in PLAN.md and compared against future privacy-protocol experiments.
        """
        if not self.direct_traces:
            self.fail("No direct-routed messages found")
        flood_wc  = self.flood_traces[0].witness_count
        direct_wc = self.direct_traces[0].witness_count
        if direct_wc == 0:
            self.skipTest("direct_wc=0; cannot compute ratio")
        ratio = flood_wc / direct_wc
        self.assertGreaterEqual(
            ratio, 1.2,
            f"Expected >=1.2x witness reduction (flood/direct); got {ratio:.1f}x "
            f"(flood={flood_wc}, direct={direct_wc})",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
