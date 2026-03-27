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

from .airtime import flood_timeout_secs
from .channel import ChannelModel
from .cli import build_parser
from .config import RadioConfig, load_topology
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
    warmup_overridden = args.warmup is not None                                       # type: ignore[attr-defined]
    if args.traffic_interval is not None:  sim.traffic_interval_secs = args.traffic_interval  # type: ignore[attr-defined]
    if args.advert_interval is not None:   sim.advert_interval_secs = args.advert_interval  # type: ignore[attr-defined]
    if args.agent is not None:             sim.default_binary = args.agent            # type: ignore[attr-defined]
    if args.seed is not None:              sim.seed = args.seed                       # type: ignore[attr-defined]
    if args.max_heap_kb is not None:       sim.default_max_heap_kb = args.max_heap_kb # type: ignore[attr-defined]

    rng = random.Random(sim.seed)
    metrics = MetricsCollector()
    tracer  = PacketTracer()
    topology = Topology(topo_cfg)

    # ------------------------------------------------------------------
    # Spawn node agents
    # ------------------------------------------------------------------
    agents: dict[str, NodeAgent] = {}
    for node_cfg in topo_cfg.nodes:
        agents[node_cfg.name] = NodeAgent(node_cfg, sim, radio=topo_cfg.radio)

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
    # RF physical-layer model (always: airtime + contention)
    # ------------------------------------------------------------------
    # Fall back to EU Narrow defaults when the topology has no explicit
    # radio section.
    radio = topo_cfg.radio or RadioConfig()

    link_rssi: dict[str, dict[str, float]] = {}
    for name in topology.all_names():
        link_rssi[name] = {
            link.other: link.rssi
            for link in topology.neighbours(name)
        }
    channel = ChannelModel(link_rssi=link_rssi)
    log.info("RF contention: capture effect using edge RSSI (6 dB threshold)")

    log.info(
        "RF: SF=%d  BW=%d Hz  CR=4/%d",
        radio.sf, radio.bw_hz, radio.cr + 4,
    )

    # ------------------------------------------------------------------
    # Wire up router (registers callbacks on all agents)
    # ------------------------------------------------------------------
    router = PacketRouter(topology, agents, metrics, rng, tracer=tracer,
                          radio=radio, channel=channel)
    traffic = TrafficGenerator(agents, topology, sim, metrics, rng, radio=radio)

    # ------------------------------------------------------------------
    # Run simulation tasks concurrently
    # ------------------------------------------------------------------
    # Auto-derive warmup from the advert stagger when the user hasn't
    # explicitly set --warmup.  The stagger is computed from the actual
    # radio parameters at runtime, so it accounts for SF/BW/CR correctly.
    # Warmup = stagger + propagation margin, so adverts have time to
    # flood through the network before traffic starts.
    stagger_secs = traffic._stagger_secs
    if not warmup_overridden:
        auto_warmup = stagger_secs + 10.0  # stagger + 10 s propagation margin
        if auto_warmup > sim.warmup_secs:
            log.info("Auto-adjusting warmup: %.0f s → %.0f s (stagger=%.1f s + 10 s margin)",
                     sim.warmup_secs, auto_warmup, stagger_secs)
            sim.warmup_secs = auto_warmup

    # Grace period: let in-flight messages complete ACK/retry cycles after
    # the traffic window closes.  Two components:
    #   - stagger: proxy for max flood propagation time through the network
    #     (scales with node count × airtime)
    #   - flood_timeout: ACK wait with retries (companion_radio formula)
    # Together they cover propagation + round-trip ACK for the last message.
    propagation_margin = min(stagger_secs, 30.0)
    grace_secs = propagation_margin + flood_timeout_secs(
        radio.sf, radio.bw_hz, radio.cr, radio.preamble_symbols,
        retries=1)

    total_time = sim.warmup_secs + sim.duration_secs + grace_secs
    log.info(
        "Simulation: adverts %.0fs + warmup %.0fs + traffic %.0fs + grace %.0fs = %.0fs total  (interval=%.0fs)",
        stagger_secs,
        sim.warmup_secs,
        sim.duration_secs,
        grace_secs,
        stagger_secs + total_time,
        sim.traffic_interval_secs,
    )

    # Initial advertisement floods.  Two rounds are needed so multi-hop
    # topologies converge: round 1 propagates adverts to direct neighbours,
    # and relay Dispatchers (with LBT) forward them; round 2 fills in any
    # endpoints that were missed due to hidden-terminal collisions.
    await traffic.run_initial_adverts()
    reflood_wait = min(stagger_secs + 2.0, 30.0)
    await asyncio.sleep(reflood_wait)          # let relays re-flood round 1
    await traffic.run_initial_adverts()

    # Long-running tasks: traffic stops sending after duration_secs, then the
    # wall-clock timer keeps the sim alive for the grace period so in-flight
    # messages can complete their ACK/retry cycles.
    timer_task = asyncio.create_task(_wall_clock_timer(total_time), name="timer")
    background_tasks = [
        asyncio.create_task(traffic.run_periodic_adverts(),       name="periodic-adverts"),
        asyncio.create_task(traffic.run_traffic(),                name="traffic"),
        asyncio.create_task(router.run_replay_drainer(),          name="replay-drainer"),
    ]

    # Block until the wall-clock timer fires (or KeyboardInterrupt).
    # Traffic may finish before the timer — that's expected; the grace
    # period keeps routing alive so in-flight ACKs can complete.
    await timer_task
    for t in background_tasks:
        t.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Shutdown — sample RSS before quitting so processes are still alive
    # ------------------------------------------------------------------
    log.info("Simulation complete — sampling RSS ...")
    rss_results = await asyncio.gather(
        *(a.sample_rss_kb() for a in agents.values()), return_exceptions=True
    )
    for agent, rss in zip(agents.values(), rss_results):
        if isinstance(rss, int):
            metrics.record_rss(agent.config.name, rss)

    # Snapshot contact discovery and pub→name map before shutdown
    pub_to_name: dict[str, str] = {}
    for name, agent in agents.items():
        if agent.state.pub_key:
            pub_to_name[agent.state.pub_key] = name
    metrics.set_pub_to_name(pub_to_name)

    total_nodes = len(agents)
    for name, agent in sorted(agents.items()):
        if not agent.state.is_relay:
            metrics.record_contacts(name, len(agent.state.known_peers), total_nodes)

    log.info("Shutting down node agents ...")
    await asyncio.gather(*(agent.quit() for agent in agents.values()), return_exceptions=True)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    report = metrics.report() + tracer.report(total_sim_secs=total_time)
    print(report)

    if args.report is not None:                                                        # type: ignore[attr-defined]
        with open(args.report, "w") as fh:                                             # type: ignore[attr-defined]
            fh.write(report)
        log.info("Report written to %s", args.report)                                 # type: ignore[attr-defined]

    # Write trace if requested.
    trace_path: str | None = args.trace_out                                            # type: ignore[attr-defined]

    if trace_path is not None:
        with open(trace_path, "w") as fh:
            json.dump(
                tracer.to_dict(
                    topology_path=args.topology,
                    node_names=list(agents.keys()),
                    metrics=metrics.to_dict(),
                    total_sim_secs=total_time,
                ),
                fh, indent=2,
            )
        log.info("Trace written to %s", trace_path)

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
