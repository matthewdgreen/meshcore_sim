#!/usr/bin/env bash
#
# setup_meshcore.sh — Switch MeshCore sources and rebuild all binaries
#
# Usage:
#   ./setup_meshcore.sh                                        # official MeshCore (meshcore-dev)
#   ./setup_meshcore.sh https://github.com/stachuman/MeshCore  # user's fork
#   ./setup_meshcore.sh https://github.com/stachuman/MeshCore main
#   ./setup_meshcore.sh https://github.com/stachuman/MeshCore dev
#   ./setup_meshcore.sh --rebuild                              # recompile only, no download
#
# What it does:
#   1. Points the MeshCore/ submodule at the given repository and branch
#   2. Pulls the latest sources
#   3. Builds node_agent (required for simulation)
#   4. Builds C++ tests
#   5. Builds privatemesh experiment agents (if their directories exist)
#   6. Runs a quick sanity check
#
# With --rebuild, skips steps 1-2 and recompiles using whatever sources
# are already in MeshCore/.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
MESHCORE_DIR="${REPO_ROOT}/MeshCore"

# --- Parse flags -------------------------------------------------------------
REBUILD_ONLY=false
if [ "${1:-}" = "--rebuild" ] || [ "${1:-}" = "-r" ]; then
    REBUILD_ONLY=true
    shift
fi

# --- Defaults ----------------------------------------------------------------
DEFAULT_REPO="https://github.com/meshcore-dev/MeshCore"
DEFAULT_BRANCH="main"

REPO="${1:-$DEFAULT_REPO}"
BRANCH="${2:-$DEFAULT_BRANCH}"

# --- Colours (if terminal supports them) -------------------------------------
if [ -t 1 ]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; NC=''
fi

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header(){ echo -e "\n${BOLD}=== $* ===${NC}"; }

# --- Step 1: Switch MeshCore submodule to requested repo/branch --------------
if [ "${REBUILD_ONLY}" = true ]; then
    header "Rebuild only (skipping download)"
    cd "${MESHCORE_DIR}" 2>/dev/null || fail "MeshCore/ directory not found. Run: git submodule update --init"
    REPO="$(git remote get-url origin 2>/dev/null || echo 'local')"
    BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
    COMMIT="$(git log --oneline -1)"
    info "Using existing sources: ${COMMIT}"
    cd "${REPO_ROOT}"
else
    header "MeshCore sources"
    info "Repository: ${REPO}"
    info "Branch:     ${BRANCH}"

    cd "${MESHCORE_DIR}" 2>/dev/null || fail "MeshCore/ directory not found. Run: git submodule update --init"

    CURRENT_URL="$(git remote get-url origin 2>/dev/null || echo '')"
    if [ "${CURRENT_URL}" != "${REPO}" ]; then
        info "Changing remote origin from ${CURRENT_URL} to ${REPO}"
        git remote set-url origin "${REPO}"
    else
        info "Remote already set to ${REPO}"
    fi

    info "Fetching latest from origin..."
    git fetch origin

    # Checkout the requested branch
    info "Checking out ${BRANCH}..."
    git checkout "origin/${BRANCH}" --detach 2>/dev/null \
        || git checkout "${BRANCH}" 2>/dev/null \
        || fail "Branch '${BRANCH}' not found in ${REPO}"

    COMMIT="$(git log --oneline -1)"
    info "HEAD: ${COMMIT}"

    cd "${REPO_ROOT}"
fi

# --- Step 2: Check prerequisites ---------------------------------------------
header "Checking prerequisites"

command -v cmake >/dev/null 2>&1 || fail "cmake not found. Install with: apt install cmake / brew install cmake"
command -v make  >/dev/null 2>&1 || fail "make not found. Install build tools."
command -v python3 >/dev/null 2>&1 || fail "python3 not found."

# Check OpenSSL
if pkg-config --exists openssl 2>/dev/null; then
    info "OpenSSL: $(pkg-config --modversion openssl)"
elif [ -f /usr/include/openssl/ssl.h ] || [ -f /opt/homebrew/include/openssl/ssl.h ]; then
    info "OpenSSL: headers found"
else
    warn "OpenSSL headers not detected — cmake will try to find them"
fi

info "cmake:   $(cmake --version | head -1)"
info "python3: $(python3 --version)"

# --- Step 3: Build node_agent ------------------------------------------------
header "Building node_agent"

NODE_AGENT_DIR="${REPO_ROOT}/node_agent"
cd "${NODE_AGENT_DIR}"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -3
cmake --build build -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)" 2>&1

if [ -x build/node_agent ]; then
    info "node_agent built: ${NODE_AGENT_DIR}/build/node_agent"
else
    fail "node_agent build failed"
fi

cd "${REPO_ROOT}"

# --- Step 4: Build C++ tests -------------------------------------------------
header "Building C++ tests"

TESTS_DIR="${REPO_ROOT}/tests"
if [ -f "${TESTS_DIR}/CMakeLists.txt" ]; then
    cd "${TESTS_DIR}"
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -3
    cmake --build build -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)" 2>&1

    if [ -x build/meshcore_tests ]; then
        info "C++ tests built: ${TESTS_DIR}/build/meshcore_tests"
    else
        warn "C++ tests build failed (non-fatal)"
    fi
    cd "${REPO_ROOT}"
else
    warn "No C++ tests directory found, skipping"
fi

# --- Step 5: Build privatemesh agents (optional) -----------------------------
header "Building experiment agents"

for agent_dir in "${REPO_ROOT}"/privatemesh/*/; do
    if [ -f "${agent_dir}/CMakeLists.txt" ]; then
        name="$(basename "${agent_dir}")"
        cd "${agent_dir}"
        info "Building ${name}..."
        cmake -S . -B build -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -2
        cmake --build build -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)" 2>&1 || {
            warn "${name} build failed (non-fatal)"
            cd "${REPO_ROOT}"
            continue
        }
        # Find the built binary
        binary="$(find build -maxdepth 1 -type f -executable | head -1)"
        if [ -n "${binary}" ]; then
            info "${name} built: ${agent_dir}${binary}"
        fi
        cd "${REPO_ROOT}"
    fi
done

# --- Step 6: Sanity check ----------------------------------------------------
header "Sanity check"

# Quick C++ test run
if [ -x "${TESTS_DIR}/build/meshcore_tests" ]; then
    info "Running C++ tests..."
    if "${TESTS_DIR}/build/meshcore_tests" 2>&1 | tail -1; then
        info "C++ tests passed"
    else
        warn "C++ tests had failures"
    fi
fi

# Verify orchestrator can import
if python3 -c "from orchestrator.config import load_topology; print('orchestrator OK')" 2>&1; then
    info "Python orchestrator imports OK"
else
    warn "Python orchestrator import failed"
fi

# --- Summary ------------------------------------------------------------------
header "Ready"
echo ""
info "MeshCore source: ${REPO} @ ${BRANCH}"
info "  Commit: ${COMMIT}"
echo ""
info "Binaries:"
info "  node_agent:  ${NODE_AGENT_DIR}/build/node_agent"
[ -x "${TESTS_DIR}/build/meshcore_tests" ] && \
    info "  C++ tests:   ${TESTS_DIR}/build/meshcore_tests"
for agent_dir in "${REPO_ROOT}"/privatemesh/*/; do
    binary="$(find "${agent_dir}/build" -maxdepth 1 -type f -executable 2>/dev/null | head -1)"
    [ -n "${binary}" ] && info "  $(basename "${agent_dir}"): ${binary}"
done
echo ""
info "Run a simulation:"
info "  python3 -m orchestrator topologies/linear_three.json --duration 30 --seed 42"
echo ""
info "Run the workbench:"
info "  python3 -m workbench topologies/boston_relays.json"
echo ""
