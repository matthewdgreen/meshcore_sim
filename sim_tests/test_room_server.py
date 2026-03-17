"""
test_room_server.py — End-to-end integration tests for RoomServerNode.

Topology (star through a single relay):

    alice (endpoint) ──┐
    bob   (endpoint) ──┼── relay (relay) ── room (room_server)
    carol (endpoint) ──┘

All edges: zero loss, zero latency (fast, deterministic).

Tests verify:
  • room server emits room_post when a TXT_MSG arrives
  • the two non-sending clients receive a recv_text (the forwarded message)
  • the original sender does NOT receive an echo of its own message
  • forwarded text contains the original message text
  • subsequent senders are also forwarded correctly

Skipped automatically when the node_agent binary is not present.
"""

from __future__ import annotations

import asyncio
import random
import unittest
from typing import List

from orchestrator.config import EdgeConfig, NodeConfig, SimulationConfig, TopologyConfig
from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.traffic import TrafficGenerator
from sim_tests.helpers import BINARY_PATH, SKIP_IF_NO_BINARY


# ---------------------------------------------------------------------------
# Topology factory
# ---------------------------------------------------------------------------

def _room_server_star_config() -> TopologyConfig:
    """
    alice, bob, carol (endpoints) connected to a single relay, which in turn
    connects to a room_server node.  Zero loss, zero latency everywhere.
    """
    sim = SimulationConfig(
        warmup_secs=2.5,
        duration_secs=999.0,    # driven manually
        traffic_interval_secs=9999.0,
        advert_interval_secs=9999.0,
        default_binary=BINARY_PATH,
        seed=42,
    )
    nodes = [
        NodeConfig(name="alice", relay=False),
        NodeConfig(name="bob",   relay=False),
        NodeConfig(name="carol", relay=False),
        NodeConfig(name="relay", relay=True),
        NodeConfig(name="room",  room_server=True),
    ]
    edges = [
        EdgeConfig(a="alice", b="relay", loss=0.0, latency_ms=0.0, snr=10.0, rssi=-80.0),
        EdgeConfig(a="bob",   b="relay", loss=0.0, latency_ms=0.0, snr=10.0, rssi=-80.0),
        EdgeConfig(a="carol", b="relay", loss=0.0, latency_ms=0.0, snr=10.0, rssi=-80.0),
        EdgeConfig(a="relay", b="room",  loss=0.0, latency_ms=0.0, snr=10.0, rssi=-80.0),
    ]
    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)


# ---------------------------------------------------------------------------
# Async test driver
# ---------------------------------------------------------------------------

async def _run_room_server_sim(
    *,
    sender: str,
    message: str,
    warmup_secs: float = 2.5,
    delivery_wait: float = 2.5,
) -> tuple[
    dict[str, NodeAgent],
    List[tuple[str, dict]],   # room_post events: (node_name, event)
    List[tuple[str, dict]],   # recv_text events: (node_name, event)
]:
    """
    Bring up the star topology, flood adverts, wait for warmup, then have
    `sender` send `message` to the room server.  Collects all room_post and
    recv_text events seen during the delivery window.

    Returns (agents, room_posts, recv_texts).
    """
    topo_cfg = _room_server_star_config()
    topo_cfg.simulation.warmup_secs = warmup_secs

    rng = random.Random(42)
    metrics = MetricsCollector()
    topology = Topology(topo_cfg)

    agents: dict[str, NodeAgent] = {
        n.name: NodeAgent(n, topo_cfg.simulation)
        for n in topo_cfg.nodes
    }

    room_posts: List[tuple[str, dict]] = []
    recv_texts: List[tuple[str, dict]] = []

    await asyncio.gather(*(a.start() for a in agents.values()))
    await asyncio.gather(*(a.wait_ready(timeout=10.0) for a in agents.values()))

    # PacketRouter sets event_callback = metrics.on_event on every agent.
    # We wrap each agent's callback AFTER creation so both fire.
    PacketRouter(topology, agents, metrics, rng)

    def _wrap(agent: NodeAgent) -> None:
        base_cb = agent.event_callback  # metrics.on_event set by PacketRouter

        async def _chained(node_name: str, event: dict) -> None:
            if base_cb is not None:
                await base_cb(node_name, event)
            etype = event.get("type")
            if etype == "room_post":
                room_posts.append((node_name, event))
            elif etype == "recv_text":
                recv_texts.append((node_name, event))

        agent.event_callback = _chained

    for agent in agents.values():
        _wrap(agent)

    traffic = TrafficGenerator(agents, topology, topo_cfg.simulation, metrics, rng)

    await traffic.run_initial_adverts()
    await asyncio.sleep(warmup_secs)

    # Send the message from `sender` to the room server
    room_pub = agents["room"].state.pub_key
    await agents[sender].send_text(room_pub, message)

    # Wait for forwarding to propagate
    await asyncio.sleep(delivery_wait)

    await asyncio.gather(*(a.quit() for a in agents.values()), return_exceptions=True)
    return agents, room_posts, recv_texts


# ---------------------------------------------------------------------------
# Test class: alice sends, bob and carol receive
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestRoomServerForwarding(unittest.TestCase):
    """
    Alice sends one message to the room server.
    Expected outcome:
      • room emits exactly one room_post event
      • bob receives exactly one recv_text (the forwarded message)
      • carol receives exactly one recv_text (the forwarded message)
      • alice does NOT receive a recv_text (no self-echo)
      • the forwarded text contains alice's original message
    """

    MESSAGE = "hello from alice"

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.room_posts, cls.recv_texts = asyncio.run(
            _run_room_server_sim(sender="alice", message=cls.MESSAGE)
        )
        # Index recv_text events by recipient node name
        cls.recv_by_node: dict[str, list[dict]] = {}
        for node_name, event in cls.recv_texts:
            cls.recv_by_node.setdefault(node_name, []).append(event)

    # ------------------------------------------------------------------
    # room_post event
    # ------------------------------------------------------------------

    def test_room_post_emitted(self):
        self.assertGreater(
            len(self.room_posts), 0,
            "room server should have emitted at least one room_post event",
        )

    def test_room_post_emitted_by_room_node(self):
        names = [n for n, _ in self.room_posts]
        self.assertIn("room", names, "room_post should come from the 'room' node")

    def test_room_post_text_contains_message(self):
        room_events = [(n, e) for n, e in self.room_posts if n == "room"]
        self.assertTrue(room_events, "no room_post events from 'room' node")
        _, event = room_events[0]
        self.assertIn(
            self.MESSAGE, event.get("text", ""),
            f"room_post text should contain the original message. event={event}",
        )

    # ------------------------------------------------------------------
    # Forwarding to non-senders
    # ------------------------------------------------------------------

    def test_bob_receives_forwarded_message(self):
        self.assertIn(
            "bob", self.recv_by_node,
            "bob should have received a forwarded message from the room server",
        )

    def test_carol_receives_forwarded_message(self):
        self.assertIn(
            "carol", self.recv_by_node,
            "carol should have received a forwarded message from the room server",
        )

    def test_forwarded_text_to_bob_contains_message(self):
        events = self.recv_by_node.get("bob", [])
        self.assertTrue(events, "bob received no recv_text events")
        found = any(self.MESSAGE in e.get("text", "") for e in events)
        self.assertTrue(
            found,
            f"none of bob's recv_text messages contain '{self.MESSAGE}'. "
            f"Received: {[e.get('text') for e in events]}",
        )

    def test_forwarded_text_to_carol_contains_message(self):
        events = self.recv_by_node.get("carol", [])
        self.assertTrue(events, "carol received no recv_text events")
        found = any(self.MESSAGE in e.get("text", "") for e in events)
        self.assertTrue(
            found,
            f"none of carol's recv_text messages contain '{self.MESSAGE}'. "
            f"Received: {[e.get('text') for e in events]}",
        )

    def test_alice_does_not_receive_self_echo(self):
        """The room server must not forward a message back to its sender."""
        alice_events = self.recv_by_node.get("alice", [])
        self_echos = [e for e in alice_events if self.MESSAGE in e.get("text", "")]
        self.assertEqual(
            len(self_echos), 0,
            f"alice should not receive an echo of her own message, "
            f"but got: {self_echos}",
        )


# ---------------------------------------------------------------------------
# Test class: second sender (bob → room, alice and carol receive)
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestRoomServerSecondSender(unittest.TestCase):
    """
    Bob sends a message to the room server.
    Alice and carol should receive the forwarded message; bob should not echo.
    """

    MESSAGE = "bob checking in"

    @classmethod
    def setUpClass(cls):
        cls.agents, cls.room_posts, cls.recv_texts = asyncio.run(
            _run_room_server_sim(sender="bob", message=cls.MESSAGE)
        )
        cls.recv_by_node: dict[str, list[dict]] = {}
        for node_name, event in cls.recv_texts:
            cls.recv_by_node.setdefault(node_name, []).append(event)

    def test_room_post_emitted_for_bob_message(self):
        self.assertGreater(len(self.room_posts), 0,
                           "expected room_post when bob sends a message")

    def test_alice_receives_bobs_message(self):
        events = self.recv_by_node.get("alice", [])
        found = any(self.MESSAGE in e.get("text", "") for e in events)
        self.assertTrue(
            found,
            f"alice should receive bob's forwarded message. "
            f"alice recv_texts: {[e.get('text') for e in events]}",
        )

    def test_carol_receives_bobs_message(self):
        events = self.recv_by_node.get("carol", [])
        found = any(self.MESSAGE in e.get("text", "") for e in events)
        self.assertTrue(
            found,
            f"carol should receive bob's forwarded message. "
            f"carol recv_texts: {[e.get('text') for e in events]}",
        )

    def test_bob_not_echoed(self):
        bob_events = self.recv_by_node.get("bob", [])
        echos = [e for e in bob_events if self.MESSAGE in e.get("text", "")]
        self.assertEqual(len(echos), 0,
                         f"bob should not receive an echo: {echos}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
