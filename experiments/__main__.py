"""
python3 -m experiments [options]

Run one or more routing-variant experiments and print a comparison table.

Examples
--------
# All scenarios, all available binaries:
    python3 -m experiments

# One scenario, all binaries:
    python3 -m experiments --scenario grid/3x3

# One scenario, one binary:
    python3 -m experiments --scenario grid/3x3 --binary nexthop

# Save trace files for visualisation:
    python3 -m experiments --scenario grid/10x10 --trace-out-dir /tmp/traces
    python3 -m viz /tmp/traces/grid_10x10_topology.json \\
                  --trace /tmp/traces/grid_10x10_node_agent_trace.json
    python3 -m viz /tmp/traces/grid_10x10_topology.json \\
                  --trace /tmp/traces/grid_10x10_nexthop_agent_trace.json

# List available scenarios:
    python3 -m experiments --list
"""

from __future__ import annotations

import argparse
import os
import sys

import json

from experiments.compare import compare
from experiments.runner import run_scenario
from experiments.scenarios import (
    ADAPTIVE_DELAY_BINARY,
    ALL_BINARIES,
    ALL_SCENARIOS,
    BASELINE_BINARY,
    NEXTHOP_BINARY,
    SCENARIO_BY_NAME,
    available_binaries,
)
from orchestrator.config import topology_to_dict

_BINARY_ALIASES: dict[str, str] = {
    "baseline":        BASELINE_BINARY,
    "node_agent":      BASELINE_BINARY,
    "nexthop":         NEXTHOP_BINARY,
    "nexthop_agent":   NEXTHOP_BINARY,
    "adaptive":        ADAPTIVE_DELAY_BINARY,
    "adaptive_delay":  ADAPTIVE_DELAY_BINARY,
    "adaptive_agent":  ADAPTIVE_DELAY_BINARY,
}


def _resolve_binary(name: str) -> str:
    """Resolve a short alias or path to an absolute binary path."""
    if name in _BINARY_ALIASES:
        return _BINARY_ALIASES[name]
    return os.path.abspath(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m experiments",
        description="Compare routing variant experiments.",
    )
    parser.add_argument(
        "--scenario", "-s",
        metavar="NAME",
        help="Run only this scenario (e.g. 'grid/3x3').  Default: all.",
    )
    parser.add_argument(
        "--binary", "-b",
        metavar="NAME_OR_PATH",
        action="append",
        dest="binaries",
        help=(
            "Binary to include.  May be a short alias (baseline, nexthop) or "
            "a file path.  May be repeated.  Default: all available binaries."
        ),
    )
    parser.add_argument(
        "--trace-out-dir", "-t",
        metavar="DIR",
        help=(
            "Write PacketTracer JSON and topology JSON to DIR after each run. "
            "Files are named <scenario_slug>_<binary>_trace.json and "
            "<scenario_slug>_topology.json.  Load them with: "
            "python3 -m viz <DIR>/<slug>_topology.json "
            "--trace <DIR>/<slug>_<binary>_trace.json"
        ),
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available scenarios and binaries, then exit.",
    )
    args = parser.parse_args(argv)

    # --list
    if args.list:
        print("Available scenarios:")
        for s in ALL_SCENARIOS:
            print(f"  {s.name}")
        print("\nAvailable binaries:")
        for b in ALL_BINARIES:
            exists = "✓" if (os.path.isfile(b) and os.access(b, os.X_OK)) else "✗ (not built)"
            print(f"  {os.path.basename(b):<22}  {exists}  {b}")
        return 0

    # Resolve scenarios.
    if args.scenario:
        sc = SCENARIO_BY_NAME.get(args.scenario)
        if sc is None:
            print(f"error: unknown scenario {args.scenario!r}", file=sys.stderr)
            print(f"       available: {', '.join(SCENARIO_BY_NAME)}", file=sys.stderr)
            return 1
        scenarios = [sc]
    else:
        scenarios = ALL_SCENARIOS

    # Resolve binaries.
    if args.binaries:
        binaries = [_resolve_binary(b) for b in args.binaries]
    else:
        binaries = available_binaries()
        if not binaries:
            print("error: no binaries found.  Build node_agent and/or privatemesh/nexthop first.",
                  file=sys.stderr)
            return 1

    missing = [b for b in binaries if not (os.path.isfile(b) and os.access(b, os.X_OK))]
    if missing:
        for b in missing:
            print(f"error: binary not found or not executable: {b}", file=sys.stderr)
        return 1

    trace_dir: Optional[str] = args.trace_out_dir

    # Run.
    for scenario in scenarios:
        # Slug for file names: replace path separators and spaces with underscores.
        slug = scenario.name.replace("/", "_").replace(" ", "_")

        # Write topology JSON once per scenario (before any run so the file
        # exists even if a run is interrupted).
        if trace_dir is not None:
            os.makedirs(trace_dir, exist_ok=True)
            topo_path = os.path.join(trace_dir, f"{slug}_topology.json")
            topo_dict = topology_to_dict(scenario.topo_factory())
            with open(topo_path, "w") as _f:
                json.dump(topo_dict, _f, indent=2)
            print(f"Topology written to: {topo_path}")

        results = []
        for binary in binaries:
            binary_slug = os.path.basename(binary)
            trace_out: Optional[str] = None
            if trace_dir is not None:
                trace_out = os.path.join(trace_dir, f"{slug}_{binary_slug}_trace.json")

            print(f"\nRunning: {scenario.name}  binary={binary_slug} …", flush=True)
            result = run_scenario(scenario, binary, trace_out=trace_out)
            results.append(result)
            print(f"  done in {result.elapsed_s:.1f}s  "
                  f"delivery={result.delivery_rate*100:.0f}%  "
                  f"avg_witness={result.avg_witness_count:.1f}")
            if trace_out is not None:
                print(f"  trace:    {trace_out}")

        if trace_dir is not None and results:
            print(f"\nTo visualise:")
            for r in results:
                binary_slug = os.path.basename(r.binary)
                topo_path = os.path.join(trace_dir, f"{slug}_topology.json")
                trace_path = os.path.join(trace_dir, f"{slug}_{binary_slug}_trace.json")
                print(f"  python3 -m viz {topo_path} --trace {trace_path}")

        if len(results) >= 2:
            compare(results, scenario_name=scenario.name).print()
        elif results:
            compare(results, scenario_name=scenario.name).print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
