#!/usr/bin/env bash
# BUILDSCRIPT_COLLISIONS.sh — build everything needed for the collision experiment.
#
# Usage:
#   chmod +x BUILDSCRIPT_COLLISIONS.sh
#   ./BUILDSCRIPT_COLLISIONS.sh
#
# Builds:
#   node_agent              (baseline)
#   privatemesh/nexthop     (nexthop routing variant)
#   privatemesh/adaptive_delay  (adaptive-delay variant — main experiment subject)
#
# All three binaries are placed in their respective build/ subdirectories.
# The script stops immediately on any error.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

BUILD_TYPE="${BUILD_TYPE:-Release}"

echo "==> Building with CMAKE_BUILD_TYPE=${BUILD_TYPE}"
echo "==> Repo root: ${REPO_ROOT}"
echo

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
build_agent() {
    local label="$1"
    local dir="$2"

    echo "------------------------------------------------------------"
    echo "  Building: ${label}"
    echo "  Directory: ${dir}"
    echo "------------------------------------------------------------"
    cmake -S "${dir}" -B "${dir}/build" \
          -DCMAKE_BUILD_TYPE="${BUILD_TYPE}" \
          --log-level=WARNING
    cmake --build "${dir}/build" --parallel "$(sysctl -n hw.logicalcpu 2>/dev/null || nproc)"
    echo "  OK: ${dir}/build/$(ls "${dir}/build" | grep -v '^\.' | grep -v CMake | grep -v Makefile | grep -v cmake | head -1)"
    echo
}

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
build_agent "node_agent (baseline)"              "node_agent"
build_agent "nexthop_agent"                       "privatemesh/nexthop"
build_agent "adaptive_agent (adaptive delay)"     "privatemesh/adaptive_delay"
build_agent "path2_agent (2-byte paths)"          "privatemesh/path2"
build_agent "privaterouting1_agent (PNI)"         "privatemesh/privaterouting1"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "============================================================"
echo "  All binaries built successfully."
echo "============================================================"
echo
echo "  node_agent:              node_agent/build/node_agent"
echo "  nexthop_agent:           privatemesh/nexthop/build/nexthop_agent"
echo "  adaptive_agent:          privatemesh/adaptive_delay/build/adaptive_agent"
echo "  path2_agent:             privatemesh/path2/build/path2_agent"
echo "  privaterouting1_agent:   privatemesh/privaterouting1/build/privaterouting1_agent"
echo
echo "Run the experiment:"
echo
echo "  python3 -m experiments \\"
echo "      --scenario grid/3x3/contention \\"
echo "      --binary baseline \\"
echo "      --binary adaptive \\"
echo "      --trace-out-dir /tmp/adaptive_traces"
echo
