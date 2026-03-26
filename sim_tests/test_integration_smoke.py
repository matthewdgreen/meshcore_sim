"""
test_integration_smoke.py — End-to-end simulation smoke tests.

Runs short, fully-wired simulations (router + agents + traffic) and
asserts coarse correctness properties on the resulting metrics.

Skipped automatically when the node_agent binary is not present.
"""

from __future__ import annotations

import asyncio
import re
import unittest

from orchestrator.config import EdgeConfig, NodeConfig, SimulationConfig, TopologyConfig
from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.traffic import TrafficGenerator
from sim_tests.helpers import (
    BINARY_PATH,
    SKIP_IF_NO_BINARY,
    adversarial_config,
    linear_three_config,
    two_node_direct_config,
)

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Shared async simulation runner
# ---------------------------------------------------------------------------

async def _run_sim(
    topo_cfg: TopologyConfig,
    *,
    extra_warmup: float = 3.0,
) -> tuple[dict[str, NodeAgent], MetricsCollector]:
    """
    Spawn agents, run initial adverts, wait warmup + extra_warmup, generate
    one round of traffic, wait a little longer for delivery, then quit.

    Returns (agents dict, metrics).
    """
    import random
    rng = random.Random(topo_cfg.simulation.seed or 42)
    metrics = MetricsCollector()
    topology = Topology(topo_cfg)

    agents: dict[str, NodeAgent] = {
        n.name: NodeAgent(n, topo_cfg.simulation, radio=topo_cfg.radio)
        for n in topo_cfg.nodes
    }
    await asyncio.gather(*(a.start() for a in agents.values()))
    await asyncio.gather(*(a.wait_ready(timeout=10.0) for a in agents.values()))

    router = PacketRouter(topology, agents, metrics, rng)
    traffic = TrafficGenerator(agents, topology, topo_cfg.simulation, metrics, rng)

    await traffic.run_initial_adverts()
    await asyncio.sleep(topo_cfg.simulation.warmup_secs)

    # Second advert round ensures multi-hop discovery completes
    # (e.g. alice→relay1→bob requires two rounds for both directions).
    await traffic.run_initial_adverts()
    await asyncio.sleep(extra_warmup)

    # Generate a small burst of traffic (6 attempts to account for some
    # being channel messages that don't count toward direct delivery).
    endpoints = topology.endpoint_names()
    if len(endpoints) >= 2:
        for _ in range(6):
            await traffic._send_random(endpoints)
            await asyncio.sleep(0.3)

    # Allow deliveries to propagate.  With non-zero retransmit delays a
    # 2-hop message may take 3+ seconds to traverse the network.
    await asyncio.sleep(5.0)

    await asyncio.gather(*(a.quit() for a in agents.values()), return_exceptions=True)
    return agents, metrics


# ---------------------------------------------------------------------------
# Two directly connected nodes (no relay)
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestTwoNodeDirectSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cfg = two_node_direct_config(warmup_secs=3.0)
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg))

    def test_both_nodes_ready(self):
        for agent in self.agents.values():
            self.assertRegex(agent.state.pub_key, _HEX64,
                             msg=f"{agent.config.name} pub_key not valid hex64")

    def test_pub_keys_differ(self):
        pubs = [a.state.pub_key for a in self.agents.values()]
        self.assertEqual(len(pubs), len(set(pubs)), "nodes have duplicate pub keys")

    def test_alice_is_not_relay(self):
        self.assertFalse(self.agents["alice"].state.is_relay)

    def test_bob_is_not_relay(self):
        self.assertFalse(self.agents["bob"].state.is_relay)

    def test_advert_exchange_alice_knows_bob(self):
        # After initial adverts, alice should have seen bob's advert and vice-versa
        alice_peers = self.agents["alice"].state.known_peers
        bob_pub = self.agents["bob"].state.pub_key
        self.assertIn(bob_pub, alice_peers,
                      "alice did not receive bob's advertisement")

    def test_advert_exchange_bob_knows_alice(self):
        bob_peers   = self.agents["bob"].state.known_peers
        alice_pub   = self.agents["alice"].state.pub_key
        self.assertIn(alice_pub, bob_peers,
                      "bob did not receive alice's advertisement")

    def test_some_tx_happened(self):
        total_tx = sum(self.metrics._tx.values())
        self.assertGreater(total_tx, 0, "no packets were transmitted")

    def test_report_is_non_empty_string(self):
        r = self.metrics.report()
        self.assertIsInstance(r, str)
        self.assertGreater(len(r), 0)


# ---------------------------------------------------------------------------
# Linear three-node chain through a relay
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestLinearThreeSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cfg = linear_three_config(
            warmup_secs=5.0,
            duration_secs=15.0,
            traffic_interval_secs=2.0,
        )
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg, extra_warmup=3.0))

    def test_all_three_agents_ready(self):
        for name, agent in self.agents.items():
            self.assertRegex(agent.state.pub_key, _HEX64, msg=f"{name}")

    def test_relay1_is_relay(self):
        self.assertTrue(self.agents["relay1"].state.is_relay)

    def test_endpoints_are_not_relays(self):
        self.assertFalse(self.agents["alice"].state.is_relay)
        self.assertFalse(self.agents["bob"].state.is_relay)

    def test_at_least_one_message_delivered(self):
        self.assertGreater(
            len(self.metrics._completed), 0,
            "no messages were delivered end-to-end",
        )

    def test_relay1_forwarded_packets(self):
        # relay1 should appear in both TX and RX counters
        self.assertGreater(self.metrics._tx["relay1"], 0, "relay1 never transmitted")
        self.assertGreater(self.metrics._rx["relay1"], 0, "relay1 never received")

    def test_tx_counts_nonzero_for_multiple_nodes(self):
        tx_nonzero = [n for n, c in self.metrics._tx.items() if c > 0]
        self.assertGreater(len(tx_nonzero), 1,
                           "expected multiple nodes to have transmitted")

    def test_report_contains_all_node_names(self):
        report = self.metrics.report()
        for name in ["alice", "relay1", "bob"]:
            self.assertIn(name, report)

    def test_delivery_rate_in_report(self):
        report = self.metrics.report()
        self.assertRegex(report, r"\d+/\d+")


# ---------------------------------------------------------------------------
# Adversarial — drop mode (probability=1.0) blocks all forwarded packets
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestAdversarialDropSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cfg = adversarial_config("drop", probability=1.0)
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg, extra_warmup=3.0))

    def test_adversarial_drops_recorded(self):
        self.assertGreater(
            self.metrics._adv_drop_count, 0,
            "expected adversarial drops to be recorded",
        )

    def test_zero_link_losses(self):
        # Edges have loss=0.0, so link-level drops should be 0
        self.assertEqual(self.metrics._link_loss_count, 0)

    def test_no_corrupt_counts(self):
        self.assertEqual(self.metrics._adv_corrupt_count, 0)

    def test_no_replay_counts(self):
        self.assertEqual(self.metrics._adv_replay_count, 0)


# ---------------------------------------------------------------------------
# Adversarial — corrupt mode (probability=1.0) garbles all forwarded packets
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestAdversarialCorruptSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cfg = adversarial_config("corrupt", probability=1.0, corrupt_byte_count=2)
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg, extra_warmup=3.0))

    def test_adversarial_corrupt_recorded(self):
        self.assertGreater(
            self.metrics._adv_corrupt_count, 0,
            "expected adversarial corruptions to be recorded",
        )

    def test_zero_drops_and_replays(self):
        self.assertEqual(self.metrics._adv_drop_count, 0)
        self.assertEqual(self.metrics._adv_replay_count, 0)


# ---------------------------------------------------------------------------
# Adversarial — replay mode (probability=1.0)
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestAdversarialReplaySmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cfg = adversarial_config("replay", probability=1.0, replay_delay_ms=300.0)
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg, extra_warmup=3.0))

    def test_adversarial_replays_recorded(self):
        self.assertGreater(
            self.metrics._adv_replay_count, 0,
            "expected adversarial replays to be recorded",
        )

    def test_zero_drops_and_corrupts(self):
        self.assertEqual(self.metrics._adv_drop_count, 0)
        self.assertEqual(self.metrics._adv_corrupt_count, 0)


# ---------------------------------------------------------------------------
# Perfect link (0 % loss) delivers everything
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestPerfectLinkSmoke(unittest.TestCase):
    """
    Three-node chain, zero loss, no adversarial nodes.
    Every sent message should be delivered.
    """

    @classmethod
    def setUpClass(cls):
        cfg = linear_three_config(
            warmup_secs=5.0,
            duration_secs=15.0,
            traffic_interval_secs=2.0,
        )
        # Override edges to zero loss
        for e in cfg.edges:
            e.loss = 0.0
            e.latency_ms = 0.0
        cls.agents, cls.metrics = asyncio.run(_run_sim(cfg, extra_warmup=3.0))

    def test_zero_link_losses(self):
        self.assertEqual(self.metrics._link_loss_count, 0)

    def test_all_sent_messages_delivered(self):
        total_sent = len(self.metrics._completed) + len(self.metrics._pending)
        if total_sent == 0:
            self.skipTest("no messages were sent — topology may need longer run time")
        self.assertEqual(
            len(self.metrics._pending), 0,
            f"{len(self.metrics._pending)} of {total_sent} messages undelivered on a perfect link",
        )


if __name__ == "__main__":
    unittest.main()
