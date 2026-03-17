"""
cli.py — argparse CLI definition for the orchestrator.
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator",
        description="MeshCore mesh network simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m orchestrator topologies/linear_three.json
  python -m orchestrator topologies/star_five.json --duration 30 --seed 42
  python -m orchestrator topologies/mesh_six.json --log-level debug --report out.txt
""",
    )

    p.add_argument(
        "topology",
        help="Path to topology JSON file",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECS",
        help="Override simulation duration in seconds",
    )
    p.add_argument(
        "--warmup",
        type=float,
        default=None,
        metavar="SECS",
        help="Override warmup period before traffic starts",
    )
    p.add_argument(
        "--traffic-interval",
        type=float,
        default=None,
        metavar="SECS",
        help="Override mean seconds between random text sends",
    )
    p.add_argument(
        "--advert-interval",
        type=float,
        default=None,
        metavar="SECS",
        help="Override seconds between periodic advertisement floods",
    )
    p.add_argument(
        "--agent",
        default=None,
        metavar="PATH",
        help="Override path to node_agent binary",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="RNG seed for reproducible simulations",
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        dest="log_level",
    )
    p.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="Write final metrics report to this file (default: stdout only)",
    )
    p.add_argument(
        "--trace-out",
        default=None,
        metavar="FILE",
        dest="trace_out",
        help="Write packet trace data to this JSON file for use with python3 -m viz",
    )

    return p
