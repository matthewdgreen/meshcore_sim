"""
node.py — NodeAgent: asyncio subprocess wrapper for one node_agent process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .config import NodeConfig, SimulationConfig

log = logging.getLogger(__name__)

# Callback types:
#   TxCallback(sender_name, event_dict) -> Awaitable[None]
#   EventCallback(node_name, event_dict) -> Awaitable[None]
TxCallback = Callable[[str, dict], Awaitable[None]]
EventCallback = Callable[[str, dict], Awaitable[None]]


@dataclass
class NodeState:
    name: str
    pub_key: str = ""        # 64-char hex, set after "ready" event
    is_relay: bool = False
    # pub keys of peers whose adverts this node has received
    known_peers: set[str] = field(default_factory=set)
    tx_count: int = 0
    rx_count: int = 0


class NodeAgent:
    """
    Owns one node_agent subprocess.  Exposes coroutines for sending commands
    and registers callbacks for TX and generic events.
    """

    def __init__(self, config: NodeConfig, sim_config: SimulationConfig) -> None:
        self.config = config
        self.sim_config = sim_config
        self.state = NodeState(name=config.name)

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._ready_event = asyncio.Event()

        # Set by PacketRouter / MetricsCollector after construction
        self.tx_callback: Optional[TxCallback] = None
        self.event_callback: Optional[EventCallback] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the subprocess and begin reading its stdout."""
        cmd = self._build_cmd()
        log.debug("[%s] spawning: %s", self.config.name, " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Send initial clock sync before waiting for ready
        await self.send_command({"type": "time", "epoch": self.sim_config.epoch})
        asyncio.create_task(self._reader_loop(), name=f"reader-{self.config.name}")

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Block until the node emits its 'ready' line."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def quit(self) -> None:
        """Shut the subprocess down cleanly."""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            await self.send_command({"type": "quit"})
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if self._proc.returncode is None:
                self._proc.kill()

    # ------------------------------------------------------------------
    # Commands → stdin
    # ------------------------------------------------------------------

    async def send_command(self, cmd: dict) -> None:
        assert self._proc and self._proc.stdin, "process not started"
        data = (json.dumps(cmd) + "\n").encode()
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.warning("[%s] pipe closed before command could be sent", self.config.name)

    async def deliver_rx(self, hex_data: str, snr: float, rssi: float) -> None:
        self.state.rx_count += 1
        await self.send_command({"type": "rx", "hex": hex_data, "snr": snr, "rssi": rssi})

    async def send_text(self, dest_pub_prefix: str, text: str) -> None:
        await self.send_command({"type": "send_text", "dest": dest_pub_prefix, "text": text})

    async def broadcast_advert(self, name: str = "") -> None:
        await self.send_command({"type": "advert", "name": name or self.config.name})

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("[%s] non-JSON stdout: %s", self.config.name, line[:80])
                    continue
                await self._dispatch_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("[%s] reader loop error: %s", self.config.name, exc)

    async def _dispatch_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "ready":
            self.state.pub_key = event.get("pub", "")
            self.state.is_relay = event.get("is_relay", False)
            self._ready_event.set()
            log.debug("[%s] ready  pub=%s...", self.config.name, self.state.pub_key[:16])

        elif etype == "tx":
            self.state.tx_count += 1
            if self.tx_callback is not None:
                await self.tx_callback(self.config.name, event)

        elif etype == "advert":
            peer_pub = event.get("pub", "")
            if peer_pub:
                self.state.known_peers.add(peer_pub)
            log.debug("[%s] learned peer %s (%s)", self.config.name,
                      event.get("name", "?"), peer_pub[:8] if peer_pub else "")

        elif etype == "recv_text":
            log.info("[%s] recv_text from %s: %r",
                     self.config.name, event.get("name", "?"), event.get("text", ""))

        elif etype == "log":
            log.debug("[%s] node-log: %s", self.config.name, event.get("msg", ""))

        # Always call the generic event callback (for metrics)
        if self.event_callback is not None:
            await self.event_callback(self.config.name, event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_cmd(self) -> list[str]:
        cmd = [self.sim_config.agent_binary]
        if self.config.relay:
            cmd.append("--relay")
        cmd += ["--name", self.config.name]
        if self.config.prv_key:
            cmd += ["--prv", self.config.prv_key]
        return cmd
