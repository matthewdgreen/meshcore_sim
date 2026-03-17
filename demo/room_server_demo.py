#!/usr/bin/env python3
"""
demo/room_server_demo.py — Interactive room-server demo on a 10×10 relay grid.

Topology
--------
  n_0_0  = room server  (top-left corner)
  n_0_9  = alice        (top-right corner)
  n_9_0  = bob          (bottom-left corner)
  n_9_9  = carol        (bottom-right corner)
  all 96 interior nodes = relays

The room server re-broadcasts every TXT_MSG it receives to all other known
contacts (forwarding mechanism lives in RoomServerNode::onPeerDataRecv in
SimNode.cpp).

Usage
-----
  cd /path/to/meshcore_sim
  python -m demo.room_server_demo [--binary ./node_agent/build/node_agent]

Interactive commands
--------------------
  alice: hello everyone   — alice sends a message to the room
  bob: how's the signal?  — bob sends a message to the room
  carol: good here!       — carol sends
  /quit                   — stop the demo
  /help                   — show this help

Messages arrive at the room server, which forwards them to the other two
clients.  Received messages are displayed as they arrive.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import threading

# Allow running as  python -m demo.room_server_demo  from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from orchestrator.config import EdgeConfig, NodeConfig, SimulationConfig, TopologyConfig
from orchestrator.metrics import MetricsCollector
from orchestrator.node import NodeAgent
from orchestrator.router import PacketRouter
from orchestrator.topology import Topology
from orchestrator.traffic import TrafficGenerator

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------
_COLOURS = {
    "alice": "\033[36m",   # cyan
    "bob":   "\033[33m",   # yellow
    "carol": "\033[35m",   # magenta
    "room":  "\033[32m",   # green
    "reset": "\033[0m",
    "dim":   "\033[2m",
    "bold":  "\033[1m",
}

def _c(name: str, text: str) -> str:
    """Wrap text in an ANSI colour."""
    return f"{_COLOURS.get(name, '')}{text}{_COLOURS['reset']}"


# ---------------------------------------------------------------------------
# Topology builder
# ---------------------------------------------------------------------------

_ROWS, _COLS = 10, 10

# Corner positions
_ROOM_SERVER = "n_0_0"
_ALICE        = "n_0_9"
_BOB          = "n_9_0"
_CAROL        = "n_9_9"
_CLIENTS      = {_ALICE: "alice", _BOB: "bob", _CAROL: "carol"}
_REVERSE      = {v: k for k, v in _CLIENTS.items()}  # alias → node name


def _build_topology(binary: str) -> TopologyConfig:
    """
    Build a 10×10 grid with the room server at n_0_0 and clients at the
    three other corners.  All other nodes are relays.
    """
    special = {_ROOM_SERVER, _ALICE, _BOB, _CAROL}

    nodes: list[NodeConfig] = []
    for r in range(_ROWS):
        for c in range(_COLS):
            name = f"n_{r}_{c}"
            if name == _ROOM_SERVER:
                nodes.append(NodeConfig(name=name, relay=False, room_server=True))
            elif name in special:
                nodes.append(NodeConfig(name=name, relay=False))
            else:
                nodes.append(NodeConfig(name=name, relay=True))

    edges: list[EdgeConfig] = []
    for r in range(_ROWS):
        for c in range(_COLS):
            if c + 1 < _COLS:
                edges.append(EdgeConfig(
                    a=f"n_{r}_{c}", b=f"n_{r}_{c+1}",
                    loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
                ))
            if r + 1 < _ROWS:
                edges.append(EdgeConfig(
                    a=f"n_{r}_{c}", b=f"n_{r+1}_{c}",
                    loss=0.02, latency_ms=20.0, snr=8.0, rssi=-85.0,
                ))

    sim = SimulationConfig(
        warmup_secs=8.0,
        duration_secs=99999.0,   # runs until /quit
        traffic_interval_secs=99999.0,
        advert_interval_secs=99999.0,
        default_binary=binary,
        seed=42,
    )
    return TopologyConfig(nodes=nodes, edges=edges, simulation=sim)


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------

class RoomDemo:
    def __init__(self, topo_cfg: TopologyConfig) -> None:
        self._topo_cfg  = topo_cfg
        self._agents: dict[str, NodeAgent] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Filled after warmup: alias → full pub-key hex
        self._room_pub: str = ""

    # ------------------------------------------------------------------
    # Event display
    # ------------------------------------------------------------------

    def _display_recv(self, node_name: str, event: dict) -> None:
        """Pretty-print a message received by a client node."""
        alias = _CLIENTS.get(node_name, node_name)
        sender_name = event.get("name", "?")
        text = event.get("text", "")
        # The room server forwards messages prefixed with "[sender_name]: text"
        print(f"\n  {_c(alias, f'▶ {alias}')} {_c('dim', 'received')}  "
              f"{_c('room', sender_name)}: {text}")
        print("  > ", end="", flush=True)

    def _display_room_post(self, event: dict) -> None:
        """Show a room_post event from the server (message arrived at server)."""
        sender_name = event.get("name", "?")
        text = event.get("text", "")
        print(f"\n  {_c('room', '📡 room')} {_c('dim', 'relaying from')} "
              f"{_c('room', sender_name)}: {text}")
        print("  > ", end="", flush=True)

    async def _event_cb(self, node_name: str, event: dict) -> None:
        etype = event.get("type")
        if etype == "recv_text" and node_name in _CLIENTS:
            self._display_recv(node_name, event)
        elif etype == "room_post" and node_name == _ROOM_SERVER:
            self._display_room_post(event)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        topo_cfg   = self._topo_cfg
        rng        = random.Random(42)
        metrics    = MetricsCollector()
        topology   = Topology(topo_cfg)

        self._agents = {
            n.name: NodeAgent(n, topo_cfg.simulation) for n in topo_cfg.nodes
        }

        # Wire event callback before starting so we don't miss early events.
        for agent in self._agents.values():
            agent.event_callback = self._event_cb

        print(_c("bold", "\n  Starting 100 node processes …"), flush=True)
        await asyncio.gather(*(a.start() for a in self._agents.values()))
        print("  Waiting for all nodes to be ready …", flush=True)
        await asyncio.gather(*(
            a.wait_ready(timeout=30.0) for a in self._agents.values()
        ))

        PacketRouter(topology, self._agents, metrics, rng)
        traffic = TrafficGenerator(
            self._agents, topology, topo_cfg.simulation, metrics, rng
        )

        print("  Flooding advertisements …", flush=True)
        await traffic.run_initial_adverts()
        warmup = topo_cfg.simulation.warmup_secs
        print(f"  Warmup {warmup:.0f} s — waiting for routes to propagate …",
              flush=True)
        await asyncio.sleep(warmup)

        # Grab the room server's pub key so clients can address it.
        self._room_pub = self._agents[_ROOM_SERVER].state.pub_key
        if not self._room_pub:
            raise RuntimeError("Room server did not emit a ready event")

        print(_c("bold", "\n  ✓ Network is up.\n"), flush=True)

    async def send(self, alias: str, text: str) -> None:
        """Send a TXT_MSG from the named alias to the room server."""
        node_name = _REVERSE.get(alias)
        if node_name is None:
            print(f"  Unknown alias '{alias}'.  Use: alice, bob, carol")
            return
        agent = self._agents[node_name]
        await agent.send_text(self._room_pub, text)

    async def stop(self) -> None:
        await asyncio.gather(
            *(a.quit() for a in self._agents.values()),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Interactive REPL (runs in a background thread)
    # ------------------------------------------------------------------

    def _repl(self) -> None:
        """Blocking readline loop — runs in a thread; dispatches to the event loop."""
        help_text = (
            "\n"
            "  Commands:\n"
            "    alice: <message>   — send as alice\n"
            "    bob:   <message>   — send as bob\n"
            "    carol: <message>   — send as carol\n"
            "    /quit              — stop the demo\n"
            "    /help              — this help\n"
        )
        print(help_text)
        print("  > ", end="", flush=True)

        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break

            line = line.strip()
            if not line:
                print("  > ", end="", flush=True)
                continue

            if line.lower() in ("/quit", "/exit", "quit", "exit"):
                break

            if line.lower() in ("/help", "help", "?"):
                print(help_text)
                print("  > ", end="", flush=True)
                continue

            # Parse  "alias: text"
            if ":" in line:
                alias, _, text = line.partition(":")
                alias = alias.strip().lower()
                text  = text.strip()
                if alias in _REVERSE and text:
                    asyncio.run_coroutine_threadsafe(
                        self.send(alias, text), self._loop
                    )
                    print("  > ", end="", flush=True)
                    continue

            print("  Unrecognised command.  Type /help for usage.")
            print("  > ", end="", flush=True)

        # Signal the event loop to stop.
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def run_interactive(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.start()

        # Launch the blocking REPL in a thread.
        t = threading.Thread(target=self._repl, daemon=True)
        t.start()

        # Keep the event loop alive until the REPL calls loop.stop().
        try:
            while t.is_alive():
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

        print("\n  Shutting down …", flush=True)
        await self.stop()
        print("  Done.", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    default_binary = os.path.join(
        _REPO_ROOT, "node_agent", "build", "node_agent"
    )
    parser = argparse.ArgumentParser(
        description="Interactive MeshCore room-server demo on a 10×10 relay grid.",
    )
    parser.add_argument(
        "--binary",
        default=default_binary,
        help=f"Path to node_agent binary (default: {default_binary})",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level (default: WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s  %(name)s  %(message)s",
    )

    if not os.path.isfile(args.binary) or not os.access(args.binary, os.X_OK):
        print(
            f"\n  ERROR: node_agent binary not found or not executable:\n"
            f"         {args.binary}\n\n"
            f"  Build it first:\n"
            f"    cd node_agent && cmake -B build && cmake --build build\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        _c("bold", "\n  MeshCore Room-Server Demo") + "\n"
        f"  Binary : {args.binary}\n"
        f"  Grid   : {_ROWS}×{_COLS} ({_ROWS * _COLS} nodes)\n"
        f"  Server : {_ROOM_SERVER}  (room server)\n"
        f"  Clients: alice={_ALICE}  bob={_BOB}  carol={_CAROL}\n"
    )

    topo_cfg = _build_topology(args.binary)
    demo     = RoomDemo(topo_cfg)

    try:
        asyncio.run(demo.run_interactive())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
