"""
__main__.py — Entry point: python -m orchestrator <topology.json> [options]
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import resource
import sys

from .cli import build_parser
from .config import load_topology
from .metrics import MetricsCollector
from .node import NodeAgent
from .router import PacketRouter
from .topology import Topology
from .tracer import PacketTracer
from .traffic import TrafficGenerator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def run(args: object) -> int:
    topo_cfg = load_topology(args.topology)  # type: ignore[attr-defined]
    sim = topo_cfg.simulation

    # Apply CLI overrides
    if args.duration is not None:          sim.duration_secs = args.duration          # type: ignore[attr-defined]
    if args.warmup is not None:            sim.warmup_secs = args.warmup              # type: ignore[attr-defined]
    if args.traffic_interval is not None:  sim.traffic_interval_secs = args.traffic_interval  # type: ignore[attr-defined]
    if args.advert_interval is not None:   sim.advert_interval_secs = args.advert_interval  # type: ignore[attr-defined]
    if args.agent is not None:             sim.default_binary = args.agent            # type: ignore[attr-defined]
    if args.seed is not None:              sim.seed = args.seed                       # type: ignore[attr-defined]

    rng = random.Random(sim.seed)
    metrics = MetricsCollector()
    tracer  = PacketTracer()
    topology = Topology(topo_cfg)

    # ------------------------------------------------------------------
    # Spawn node agents
    # ------------------------------------------------------------------
    agents: dict[str, NodeAgent] = {}
    for node_cfg in topo_cfg.nodes:
        agents[node_cfg.name] = NodeAgent(node_cfg, sim)

    log.info("Starting %d node agent(s) ...", len(agents))
    # Start agents in batches to avoid file-descriptor exhaustion on large
    # topologies (each subprocess consumes ~5 FDs; macOS default limit is 256).
    _BATCH = 50
    all_agents = list(agents.values())
    for _i in range(0, len(all_agents), _BATCH):
        await asyncio.gather(*(a.start() for a in all_agents[_i:_i + _BATCH]))

    log.info("Waiting for all nodes to become ready ...")
    try:
        await asyncio.gather(*(agent.wait_ready(timeout=15.0) for agent in agents.values()))
    except asyncio.TimeoutError:
        log.error("One or more nodes failed to become ready within 15 s — aborting")
        await asyncio.gather(*(agent.quit() for agent in agents.values()), return_exceptions=True)
        return 1

    for name, agent in sorted(agents.items()):
        pub_short = agent.state.pub_key[:16] + "..." if agent.state.pub_key else "?"
        role = "relay" if agent.state.is_relay else "endpoint"
        log.info("  %-20s  %s  pub=%s", name, role, pub_short)

    # ------------------------------------------------------------------
    # Wire up router (registers callbacks on all agents)
    # ------------------------------------------------------------------
    router = PacketRouter(topology, agents, metrics, rng, tracer=tracer)
    traffic = TrafficGenerator(agents, topology, sim, metrics, rng)

    # ------------------------------------------------------------------
    # Run simulation tasks concurrently
    # ------------------------------------------------------------------
    log.info(
        "Simulation running for %.0f s  (warmup=%.0f s, traffic_interval=%.0f s) ...",
        sim.duration_secs,
        sim.warmup_secs,
        sim.traffic_interval_secs,
    )

    # Initial advertisement flood is one-shot; run it before the main loop so
    # it doesn't race against the duration timer.
    await traffic.run_initial_adverts()

    # Long-running tasks: only the timer is expected to complete first.
    sim_tasks = [
        asyncio.create_task(traffic.run_periodic_adverts(),       name="periodic-adverts"),
        asyncio.create_task(traffic.run_traffic(),                name="traffic"),
        asyncio.create_task(router.run_replay_drainer(),          name="replay-drainer"),
        asyncio.create_task(_wall_clock_timer(sim.duration_secs), name="timer"),
    ]

    # Block until the wall-clock timer fires (or KeyboardInterrupt)
    done, pending = await asyncio.wait(sim_tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    # Allow pending cancellations to propagate
    await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    log.info("Simulation complete — shutting down node agents ...")
    await asyncio.gather(*(agent.quit() for agent in agents.values()), return_exceptions=True)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    report = metrics.report() + tracer.report()
    print(report)

    if args.report is not None:                                                        # type: ignore[attr-defined]
        with open(args.report, "w") as fh:                                             # type: ignore[attr-defined]
            fh.write(report)
        log.info("Report written to %s", args.report)                                 # type: ignore[attr-defined]

    if args.trace_out is not None:                                                     # type: ignore[attr-defined]
        with open(args.trace_out, "w") as fh:                                          # type: ignore[attr-defined]
            json.dump(tracer.to_dict(), fh, indent=2)
        log.info("Trace written to %s", args.trace_out)                               # type: ignore[attr-defined]

    return 0


async def _wall_clock_timer(secs: float) -> None:
    await asyncio.sleep(secs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _raise_fd_limit(needed: int = 2048) -> None:
    """Raise the open-file-descriptor soft limit to *needed* if it is lower.

    Large topologies (100+ nodes) each spawn a subprocess consuming ~5 FDs.
    macOS defaults to 256; Linux defaults to 1024.  We silently clamp to the
    hard limit if *needed* exceeds it and skip the raise if the OS refuses.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < needed:
            target = min(needed, hard) if hard > 0 else needed
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        pass  # hard limit too low; user must run `ulimit -n` manually


def main() -> None:
    _raise_fd_limit()
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    try:
        rc = asyncio.run(run(args))
        sys.exit(rc)
    except KeyboardInterrupt:
        log.info("Interrupted — exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
