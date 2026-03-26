"""
node.py — NodeAgent: asyncio subprocess wrapper for one node_agent process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .config import NodeConfig, RadioConfig, SimulationConfig

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
    role: str = "endpoint"   # "endpoint" | "relay" | "room-server"
    # pub keys of peers whose adverts this node has received
    known_peers: set[str] = field(default_factory=set)
    tx_count: int = 0
    rx_count: int = 0
    rss_kb: int = 0          # RSS snapshot taken at end of simulation (0 = not sampled)


def _make_heap_limit_fn(kb: int):
    """Return a callable suitable for preexec_fn that applies RLIMIT_AS.

    Called in the child process after fork() and before exec().  Errors are
    silently swallowed so the child still starts even if the OS refuses the
    limit (e.g. macOS does not enforce RLIMIT_AS on Apple Silicon).
    """
    import resource as _resource
    limit = kb * 1024
    def _apply():
        try:
            _resource.setrlimit(_resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            pass
    return _apply


class NodeAgent:
    """
    Owns one node_agent subprocess.  Exposes coroutines for sending commands
    and registers callbacks for TX and generic events.
    """

    def __init__(self, config: NodeConfig, sim_config: SimulationConfig,
                 radio: Optional[RadioConfig] = None) -> None:
        self.config = config
        self.sim_config = sim_config
        self._radio = radio
        self.state = NodeState(name=config.name)

        self._proc: Optional[asyncio.subprocess.Process] = None
        # Lazy-initialised inside start() so construction works outside a running loop.
        self._ready_event: Optional[asyncio.Event] = None
        # Set to True by quit() so in-flight deliver tasks don't write to a
        # closing pipe and generate "Future exception was never retrieved" noise.
        self._stopping: bool = False

        # Set by PacketRouter / MetricsCollector after construction
        self.tx_callback: Optional[TxCallback] = None
        self.event_callback: Optional[EventCallback] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the subprocess and begin reading its stdout."""
        self._ready_event = asyncio.Event()
        cmd = self._build_cmd()
        log.debug("[%s] spawning: %s", self.config.name, " ".join(cmd))

        # Apply heap limit if configured (per-node overrides simulation default).
        heap_kb = (
            self.config.max_heap_kb
            if self.config.max_heap_kb is not None
            else self.sim_config.default_max_heap_kb
        )
        preexec = _make_heap_limit_fn(heap_kb) if heap_kb is not None and sys.platform != "win32" else None
        if heap_kb is not None:
            log.debug("[%s] heap limit: %d KB", self.config.name, heap_kb)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **({"preexec_fn": preexec} if preexec is not None else {}),
        )
        # Send initial clock sync before waiting for ready
        await self.send_command({"type": "time", "epoch": self.sim_config.epoch})
        asyncio.create_task(self._reader_loop(), name=f"reader-{self.config.name}")

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Block until the node emits its 'ready' line."""
        if self._ready_event is None:
            raise RuntimeError("wait_ready() called before start()")
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def quit(self) -> None:
        """Shut the subprocess down cleanly."""
        if self._proc is None or self._proc.returncode is not None:
            return
        # Signal early so concurrent deliver_rx / send_command calls that are
        # still in-flight bail out before touching the pipe.
        self._stopping = True
        try:
            await self.send_command({"type": "quit"})
            # Close stdin so the node sees EOF even if it ignores the quit
            # command, and so the asyncio pipe transport flushes its write
            # buffer without racing against the process closing the read end.
            if self._proc.stdin and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
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
        # Bail out silently if quit() has already been called — the process is
        # shutting down and any write attempt would race against the pipe closing,
        # producing "Future exception was never retrieved" BrokenPipeError noise.
        if self._stopping and cmd.get("type") != "quit":
            return
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

    async def send_channel(self, text: str) -> None:
        await self.send_command({"type": "send_channel", "text": text})

    async def notify_rx_start(self, duration_ms: float) -> None:
        """Notify the node that a preamble has been detected (LBT)."""
        await self.send_command({"type": "rx_start", "duration_ms": int(duration_ms)})

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
            self.state.role = event.get("role", "relay" if self.state.is_relay else "endpoint")
            self._ready_event.set()
            log.debug("[%s] ready  pub=%s... role=%s",
                      self.config.name, self.state.pub_key[:16], self.state.role)

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

        elif etype == "recv_channel":
            log.info("[%s] recv_channel [%s]: %r",
                     self.config.name, event.get("channel", "?"), event.get("text", ""))

        elif etype == "room_post":
            log.info("[%s] room_post from %s (%s): %r",
                     self.config.name, event.get("name", "?"),
                     event.get("from", "")[:8], event.get("text", ""))

        elif etype == "log":
            msg = event.get("msg", "")
            # Promote ACK/retry messages to INFO so protocol behavior is visible.
            if "ACK" in msg or "retry" in msg or "failed" in msg:
                log.info("[%s] %s", self.config.name, msg)
            else:
                log.debug("[%s] node-log: %s", self.config.name, msg)

        # Always call the generic event callback (for metrics)
        if self.event_callback is not None:
            await self.event_callback(self.config.name, event)

    # ------------------------------------------------------------------
    # Resource measurement
    # ------------------------------------------------------------------

    async def sample_rss_kb(self) -> Optional[int]:
        """Sample the current RSS of the subprocess in KB via `ps`.

        Returns None if the process is not running or sampling fails.
        Stores the result in self.state.rss_kb.
        """
        if self._proc is None or self._proc.returncode is not None:
            return None
        try:
            ps = await asyncio.create_subprocess_exec(
                "ps", "-o", "rss=", "-p", str(self._proc.pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(ps.communicate(), timeout=2.0)
            rss = int(out.strip())
            self.state.rss_kb = rss
            return rss
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_cmd(self) -> list[str]:
        binary = self.config.binary or self.sim_config.default_binary
        cmd = [binary]
        if self.config.room_server:
            cmd.append("--room-server")
        elif self.config.relay:
            cmd.append("--relay")
        cmd += ["--name", self.config.name]
        if self.config.prv_key:
            cmd += ["--prv", self.config.prv_key]
        if self._radio is not None:
            cmd += ["--sf", str(self._radio.sf),
                    "--bw", str(self._radio.bw_hz),
                    "--cr", str(self._radio.cr)]
        return cmd
