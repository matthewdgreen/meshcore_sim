"""
test_node_agent.py — Integration tests for orchestrator.node.NodeAgent

Skipped automatically when the node_agent binary is not present.
Each test method drives its async body via asyncio.run().
"""

from __future__ import annotations

import asyncio
import re
import unittest

from orchestrator.config import NodeConfig, SimulationConfig
from orchestrator.node import NodeAgent
from sim_tests.helpers import BINARY_PATH, SKIP_IF_NO_BINARY

_SIM = SimulationConfig(default_binary=BINARY_PATH)

# Hex string: exactly 64 lower/uppercase hex characters
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# _build_cmd — pure logic, no process needed
# ---------------------------------------------------------------------------

class TestBuildCmd(unittest.TestCase):
    """No subprocess; tests only the command-line construction logic."""

    def _agent(self, name: str = "test", **kwargs) -> NodeAgent:
        cfg = NodeConfig(name=name, **kwargs)
        return NodeAgent(cfg, _SIM)

    def test_binary_is_first_element(self):
        cmd = self._agent()._build_cmd()
        self.assertEqual(cmd[0], BINARY_PATH)

    def test_name_flag_present(self):
        cmd = self._agent(name="mynode")._build_cmd()
        self.assertIn("--name", cmd)
        self.assertIn("mynode", cmd)

    def test_no_relay_flag_by_default(self):
        cmd = self._agent()._build_cmd()
        self.assertNotIn("--relay", cmd)

    def test_relay_flag_when_relay_true(self):
        cmd = self._agent(relay=True)._build_cmd()
        self.assertIn("--relay", cmd)

    def test_prv_key_flag_included(self):
        key = "ab" * 64
        cmd = self._agent(prv_key=key)._build_cmd()
        self.assertIn("--prv", cmd)
        idx = cmd.index("--prv")
        self.assertEqual(cmd[idx + 1], key)

    def test_no_prv_flag_without_key(self):
        cmd = self._agent()._build_cmd()
        self.assertNotIn("--prv", cmd)

    def test_per_node_binary_overrides_sim_default(self):
        cfg = NodeConfig(name="app", binary="/custom/app_node_agent")
        cmd = NodeAgent(cfg, _SIM)._build_cmd()
        self.assertEqual(cmd[0], "/custom/app_node_agent")

    def test_no_per_node_binary_uses_sim_default(self):
        cfg = NodeConfig(name="relay")
        cmd = NodeAgent(cfg, _SIM)._build_cmd()
        self.assertEqual(cmd[0], BINARY_PATH)

    def test_room_server_flag_added(self):
        cfg = NodeConfig(name="hub", room_server=True)
        cmd = NodeAgent(cfg, _SIM)._build_cmd()
        self.assertIn("--room-server", cmd)

    def test_room_server_excludes_relay_flag(self):
        """--room-server and --relay are mutually exclusive; room_server wins."""
        cfg = NodeConfig(name="hub", room_server=True, relay=True)
        cmd = NodeAgent(cfg, _SIM)._build_cmd()
        self.assertIn("--room-server", cmd)
        self.assertNotIn("--relay", cmd)

    def test_no_room_server_flag_by_default(self):
        cmd = self._agent()._build_cmd()
        self.assertNotIn("--room-server", cmd)


# ---------------------------------------------------------------------------
# NodeAgent lifecycle
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestNodeAgentLifecycle(unittest.TestCase):

    def test_start_and_ready(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="lifecycle_test"), _SIM)
            await agent.start()
            try:
                await agent.wait_ready(timeout=5.0)
            finally:
                await agent.quit()
        asyncio.run(_run())

    def test_pub_key_is_64_hex_chars(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="pubkey_test"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            pub = agent.state.pub_key
            await agent.quit()
            return pub
        pub = asyncio.run(_run())
        self.assertRegex(pub, _HEX64)

    def test_relay_flag_reflected_in_state(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="relay_test", relay=True), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            flag = agent.state.is_relay
            await agent.quit()
            return flag
        self.assertTrue(asyncio.run(_run()))

    def test_non_relay_flag_reflected_in_state(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="ep_test", relay=False), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            flag = agent.state.is_relay
            await agent.quit()
            return flag
        self.assertFalse(asyncio.run(_run()))

    def test_quit_terminates_process(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="quit_test"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            await agent.quit()
            return agent._proc.returncode
        rc = asyncio.run(_run())
        self.assertIsNotNone(rc)

    def test_double_quit_does_not_raise(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="double_quit"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            await agent.quit()
            await agent.quit()   # second call must be a no-op
        asyncio.run(_run())   # should not raise

    def test_two_nodes_have_different_pub_keys(self):
        async def _run():
            a1 = NodeAgent(NodeConfig(name="node_a"), _SIM)
            a2 = NodeAgent(NodeConfig(name="node_b"), _SIM)
            await a1.start()
            await a2.start()
            await asyncio.gather(a1.wait_ready(timeout=5.0), a2.wait_ready(timeout=5.0))
            pubs = (a1.state.pub_key, a2.state.pub_key)
            await asyncio.gather(a1.quit(), a2.quit())
            return pubs
        p1, p2 = asyncio.run(_run())
        self.assertNotEqual(p1, p2)


# ---------------------------------------------------------------------------
# NodeAgent commands
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestNodeAgentCommands(unittest.TestCase):
    """
    Verify that stdin commands don't crash the process and that
    observable state (rx_count) updates correctly.
    """

    def test_send_time_command_does_not_raise(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="time_cmd"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            await agent.send_command({"type": "time", "epoch": 1_700_000_000})
            await asyncio.sleep(0.05)
            await agent.quit()
        asyncio.run(_run())

    def test_deliver_rx_increments_rx_count(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="rx_count"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            # Send a dummy (non-parseable) packet — node will discard it,
            # but our counter is incremented by the orchestrator before delivery.
            await agent.deliver_rx("deadbeef", snr=5.0, rssi=-90.0)
            await asyncio.sleep(0.05)
            count = agent.state.rx_count
            await agent.quit()
            return count
        self.assertEqual(asyncio.run(_run()), 1)

    def test_deliver_rx_twice_gives_count_two(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="rx_two"), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            await agent.deliver_rx("deadbeef", snr=5.0, rssi=-90.0)
            await agent.deliver_rx("cafebabe", snr=5.0, rssi=-90.0)
            await asyncio.sleep(0.05)
            count = agent.state.rx_count
            await agent.quit()
            return count
        self.assertEqual(asyncio.run(_run()), 2)

    def test_broadcast_advert_produces_tx(self):
        """
        Broadcasting an advert should make the node emit a 'tx' event
        (the raw advertisement packet to be sent over the air).
        """
        async def _run():
            tx_events: list[dict] = []

            agent = NodeAgent(NodeConfig(name="advert_tx"), _SIM)

            async def capture_tx(sender: str, event: dict):
                if event.get("type") == "tx":
                    tx_events.append(event)

            await agent.start()
            await agent.wait_ready(timeout=5.0)
            agent.tx_callback = capture_tx
            await agent.broadcast_advert("advert_tx")
            # Give the node time to emit the tx event
            await asyncio.sleep(0.5)
            await agent.quit()
            return tx_events

        events = asyncio.run(_run())
        self.assertGreater(len(events), 0, "expected at least one tx event after advert")
        self.assertIn("hex", events[0])

    def test_advert_tx_hex_is_valid_hex(self):
        async def _run():
            hexes: list[str] = []
            agent = NodeAgent(NodeConfig(name="hex_check"), _SIM)

            async def capture(sender: str, event: dict):
                if event.get("type") == "tx":
                    hexes.append(event["hex"])

            await agent.start()
            await agent.wait_ready(timeout=5.0)
            agent.tx_callback = capture
            await agent.broadcast_advert()
            await asyncio.sleep(0.5)
            await agent.quit()
            return hexes

        hexes = asyncio.run(_run())
        self.assertTrue(hexes, "no tx hex emitted")
        for h in hexes:
            try:
                bytes.fromhex(h)
            except ValueError:
                self.fail(f"tx hex is not valid hex: {h!r}")

    def test_tx_count_increments_on_advert(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="tx_count"), _SIM)

            async def noop(sender, event): pass
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            agent.tx_callback = noop
            await agent.broadcast_advert()
            await asyncio.sleep(0.5)
            count = agent.state.tx_count
            await agent.quit()
            return count

        self.assertGreater(asyncio.run(_run()), 0)


# ---------------------------------------------------------------------------
# RoomServerNode — subprocess integration
# ---------------------------------------------------------------------------

@SKIP_IF_NO_BINARY
class TestRoomServerNode(unittest.TestCase):
    """
    Verify that a node started with room_server=True:
      • starts and reaches ready state
      • reports role="room-server" in state
      • does NOT report is_relay=True
    """

    def test_room_server_starts_and_ready(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="room_srv", room_server=True), _SIM)
            await agent.start()
            try:
                await agent.wait_ready(timeout=5.0)
            finally:
                await agent.quit()
        asyncio.run(_run())   # must not raise

    def test_room_server_role_in_state(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="rs_role", room_server=True), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            role = agent.state.role
            await agent.quit()
            return role
        self.assertEqual(asyncio.run(_run()), "room-server")

    def test_room_server_is_not_relay(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="rs_relay", room_server=True), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            flag = agent.state.is_relay
            await agent.quit()
            return flag
        self.assertFalse(asyncio.run(_run()))

    def test_room_server_pub_key_is_64_hex(self):
        async def _run():
            agent = NodeAgent(NodeConfig(name="rs_pub", room_server=True), _SIM)
            await agent.start()
            await agent.wait_ready(timeout=5.0)
            pub = agent.state.pub_key
            await agent.quit()
            return pub
        self.assertRegex(asyncio.run(_run()), _HEX64)


if __name__ == "__main__":
    unittest.main()
