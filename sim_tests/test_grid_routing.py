"""
test_grid_routing.py — Integration tests for grid topology routing behaviour.

Tests the flood → direct routing transition:
  1. Source (n_0_0) sends first message to dest (n_{R-1}_{C-1})  → flood
  2. Dest sends PATH reply back (via path exchange in SimNode::onPeerDataRecv)
  3. Source (and dest) learn direct paths
  4. Subsequent messages travel direct (lower witness_count)

Run against the compiled node_agent binary; skipped automatically if not found.
"""

from __future__ import annotations

import asyncio
import random
import unittest

from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.packet import ROUTE_TYPE_DIRECT, ROUTE_TYPE_FLOOD
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.tracer import PacketTracer
from sim_tests.helpers import SKIP_IF_NO_BINARY, grid_topo_config


# ---------------------------------------------------------------------------
# Low-level async runner — drives a grid sim with explicit timing control
# ---------------------------------------------------------------------------

async def _run_grid_sim(
    rows: int,
    cols: int,
    *,
    loss: float = 0.0,
    warmup_secs: float = 15.0,
    path_exchange_wait: float = 8.0,
    seed: int = 42,
) -> tuple[dict[str, NodeAgent], MetricsCollector, PacketTracer]:
    """
    Bring up a rows×cols grid, wait for adverts to propagate, then:

      Round 1: src → dst  (expect flood; triggers path exchange)
      Wait:    path_exchange_wait secs for PATH packets to propagate back
      Round 2: src → dst  (expect direct, now that path is known)
      Round 3: dst → src  (expect direct, symmetric exchange)

    Returns (agents, metrics, tracer).
    """
    topo_cfg = grid_topo_config(
        rows, cols,
        warmup_secs=warmup_secs,
        duration_secs=999.0,          # we drive timing manually
        traffic_interval_secs=9999.0, # no auto-traffic
        advert_interval_secs=9999.0,  # no auto-adverts
        seed=seed,
    )
    # Disable link loss for deterministic path-learning
    for e in topo_cfg.edges:
        e.loss = loss

    rng = random.Random(seed)
    metrics = MetricsCollector()
    tracer  = PacketTracer()
    topology = Topology(topo_cfg)

    agents: dict[str, NodeAgent] = {
        n.name: NodeAgent(n, topo_cfg.simulation, radio=topo_cfg.radio)
        for n in topo_cfg.nodes
    }
    await asyncio.gather(*(a.start() for a in agents.values()))
    await asyncio.gather(*(a.wait_ready(timeout=15.0) for a in agents.values()))

    router  = PacketRouter(topology, agents, metrics, rng, tracer=tracer)
    from orchestrator.traffic import TrafficGenerator
    traffic = TrafficGenerator(agents, topology, topo_cfg.simulation, metrics, rng)

    # Flood adverts so every node builds its contacts table
    await traffic.run_initial_adverts()
    await asyncio.sleep(warmup_secs)

    src_name = f"n_0_0"
    dst_name = f"n_{rows - 1}_{cols - 1}"

    async def _send(sender: str, receiver: str, text: str) -> None:
        """Tell the sender agent to send text to the receiver's pub_key."""
        recv_pub = agents[receiver].state.pub_key
        await agents[sender].send_text(recv_pub, text)

    # ------------------------------------------------------------------
    # Round 1: src → dst  (first contact — must flood)
    # ------------------------------------------------------------------
    await _send(src_name, dst_name, "hello-flood")
    # Give enough time for the flood and the PATH reply to propagate
    await asyncio.sleep(path_exchange_wait)

    # ------------------------------------------------------------------
    # Round 2: src → dst again  (src should now have dst's direct path)
    # ------------------------------------------------------------------
    await _send(src_name, dst_name, "hello-direct-fwd")
    await asyncio.sleep(path_exchange_wait)

    # ------------------------------------------------------------------
    # Round 3: dst → src  (dst received path exchange; should be direct)
    # ------------------------------------------------------------------
    await _send(dst_name, src_name, "hello-direct-rev")
    await asyncio.sleep(path_exchange_wait)

    await asyncio.gather(*(a.quit() for a in agents.values()), return_exceptions=True)
    return agents, metrics, tracer


# ---------------------------------------------------------------------------
# Helper: extract traces for a specific fingerprint substring
# ---------------------------------------------------------------------------

def _txt_traces(tracer: PacketTracer):
    """Return all PacketTrace objects whose payload_type is TXT_MSG."""
    from orchestrator.packet import PAYLOAD_TYPE_TXT_MSG
    return [
        tr for tr in tracer.traces.values()
        if tr.payload_type == PAYLOAD_TYPE_TXT_MSG
    ]


# ---------------------------------------------------------------------------
# 3×3 grid — fast, deterministic, exercises the full path-exchange loop
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestGridRouting3x3(unittest.TestCase):
    """
    3×3 grid (9 nodes, src=n_0_0, dst=n_2_2).
    Min-hop path: 4 relays (n_0_1, n_0_2 or n_1_0 etc.) → path_len = 4 bytes.
    """

    ROWS = 3
    COLS = 3

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.metrics, cls.tracer = asyncio.run(
            _run_grid_sim(cls.ROWS, cls.COLS,
                          loss=0.0,
                          warmup_secs=15.0,
                          path_exchange_wait=20.0)
        )
        cls.txt_traces = _txt_traces(cls.tracer)

    # ------------------------------------------------------------------
    # Sanity: we saw at least 3 TXT_MSG logical packets (retransmits
    # may produce extra copies when ACKs arrive after the retry timer)
    # ------------------------------------------------------------------
    def test_three_txt_messages_observed(self):
        self.assertGreaterEqual(
            len(self.txt_traces), 3,
            f"Expected at least 3 TXT_MSG logical packets, got {len(self.txt_traces)}. "
            f"Tracer report:\n{self.tracer.report()}",
        )

    # ------------------------------------------------------------------
    # Round 1: flood (witness_count > 1, i.e. multiple nodes forwarded)
    # ------------------------------------------------------------------
    def test_first_message_is_flood_routed(self):
        tr = self.txt_traces[0]
        self.assertTrue(
            tr.is_flood(),
            f"Expected first message to be flood-routed. "
            f"route_types seen: {[h.route_type for h in tr.hops]}",
        )

    def test_first_message_high_witness_count(self):
        tr = self.txt_traces[0]
        self.assertGreater(
            tr.witness_count, 1,
            f"First message should reach multiple nodes (flood). "
            f"witness_count={tr.witness_count}",
        )

    # ------------------------------------------------------------------
    # Round 2: src → dst direct after path learning
    # ------------------------------------------------------------------
    def test_second_message_is_direct_routed(self):
        # Skip over flood retransmits to find the first direct-routed message.
        direct = [tr for tr in self.txt_traces
                  if any(h.route_type == ROUTE_TYPE_DIRECT for h in tr.hops)]
        self.assertTrue(
            direct,
            f"Expected at least one direct-routed message after path exchange. "
            f"All route_types: {[set(h.route_type for h in tr.hops) for tr in self.txt_traces]}\n"
            f"Full tracer report:\n{self.tracer.report()}",
        )

    def test_second_message_low_witness_count(self):
        """Direct path -> fewer witnesses than full flood."""
        tr_flood = self.txt_traces[0]
        direct = [tr for tr in self.txt_traces
                  if any(h.route_type == ROUTE_TYPE_DIRECT for h in tr.hops)]
        if not direct:
            self.fail("No direct-routed messages found")
        tr_direct = direct[0]
        self.assertLess(
            tr_direct.witness_count,
            tr_flood.witness_count,
            f"Direct message should have fewer witnesses than flood. "
            f"flood={tr_flood.witness_count}, direct={tr_direct.witness_count}",
        )

    # ------------------------------------------------------------------
    # Round 3: dst → src direct (symmetric path exchange)
    # ------------------------------------------------------------------
    def test_third_message_is_direct_routed(self):
        direct = [tr for tr in self.txt_traces
                  if any(h.route_type == ROUTE_TYPE_DIRECT for h in tr.hops)]
        self.assertGreaterEqual(
            len(direct), 2,
            f"Expected at least 2 direct-routed messages (fwd + rev). "
            f"All route_types: {[set(h.route_type for h in tr.hops) for tr in self.txt_traces]}\n"
            f"Full tracer report:\n{self.tracer.report()}",
        )

    # ------------------------------------------------------------------
    # Node identity checks
    # ------------------------------------------------------------------
    def test_src_and_dst_are_endpoints(self):
        for name in (f"n_0_0", f"n_{self.ROWS-1}_{self.COLS-1}"):
            self.assertFalse(
                self.agents[name].state.is_relay,
                f"{name} should be an endpoint, not a relay",
            )

    def test_interior_nodes_are_relays(self):
        for r in range(self.ROWS):
            for c in range(self.COLS):
                name = f"n_{r}_{c}"
                if name in (f"n_0_0", f"n_{self.ROWS-1}_{self.COLS-1}"):
                    continue
                self.assertTrue(
                    self.agents[name].state.is_relay,
                    f"{name} should be a relay",
                )


# ---------------------------------------------------------------------------
# Metrics smoke-check for a larger 5×5 grid
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestGridMetrics5x5(unittest.TestCase):
    """
    5×5 grid (25 nodes).  Just verifies the simulation runs end-to-end and
    produces a non-trivial tracer report — does not assert on direct routing
    (path exchange timing is less deterministic at this scale in a short run).
    """

    ROWS = 5
    COLS = 5

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.metrics, cls.tracer = asyncio.run(
            _run_grid_sim(cls.ROWS, cls.COLS,
                          loss=0.0,
                          warmup_secs=20.0,
                          path_exchange_wait=20.0)
        )

    def test_all_nodes_ready(self):
        for name, agent in self.agents.items():
            self.assertTrue(
                len(agent.state.pub_key) == 64,
                f"{name} has unexpected pub_key length",
            )

    def test_tracer_report_has_content(self):
        report = self.tracer.report()
        self.assertIn("Unique packets", report)
        self.assertIn("TXT_MSG", report)

    def test_at_least_one_txt_message_observed(self):
        traces = _txt_traces(self.tracer)
        self.assertGreaterEqual(
            len(traces), 1,
            f"Expected at least 1 TXT_MSG. Report:\n{self.tracer.report()}",
        )

    def test_first_flood_reaches_most_nodes(self):
        """In a 5×5 zero-loss grid, first flood should hit many nodes."""
        traces = _txt_traces(self.tracer)
        if not traces:
            self.skipTest("No TXT_MSG traces available")
        first = traces[0]
        if not first.is_flood():
            self.skipTest("First trace is not a flood packet — cannot check coverage")
        # 25 nodes, at minimum the 4 direct neighbours of src should receive
        self.assertGreaterEqual(
            first.witness_count, 4,
            f"Flood in 5×5 grid should reach at least 4 nodes; got {first.witness_count}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
