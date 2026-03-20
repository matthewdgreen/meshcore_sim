# Quickstart — collision-avoidance experiment (macOS)

This guide reproduces the `grid/3x3/contention` experiment that compares
baseline MeshCore flooding against an adaptive-delay variant designed to
reduce RF collisions in dense mesh networks.

Expected result: **node_agent 0% delivery, adaptive_agent 100% delivery.**

---

## 1 — Prerequisites

```bash
# Xcode command-line tools (compiler + git)
xcode-select --install

# Homebrew dependencies
brew install cmake openssl@3

# Python 3.9+  (check first: python3 --version)
# If needed: brew install python@3.12
```

---

## 2 — Clone

```bash
git clone <repo-url> meshcore_sim
cd meshcore_sim
git submodule update --init        # pulls MeshCore C++ source
```

---

## 3 — Python dependencies

The core simulator requires no packages (stdlib only).  The topology
visualiser (step 7) needs Dash:

```bash
pip install -r requirements.txt
```

---

## 4 — Build the node agents

```bash
# Baseline node agent
cd node_agent
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
cd ..

# Adaptive-delay experiment agent
cd privatemesh/adaptive_delay
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
cd ../..

# Optional: nexthop agent (needed only for a three-way comparison)
# cd privatemesh/nexthop
# cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
# cmake --build build
# cd ../..
```

---

## 5 — Run the tests

```bash
# Python tests (orchestrator unit + integration)
python3 -m sim_tests

# C++ tests (crypto shims + packet serialisation) — optional
cd tests && cmake -S . -B build && cmake --build build && ./build/meshcore_tests && cd ..
```

All Python tests should report `OK`.  Integration tests are automatically
skipped when a binary is absent — that is not a failure.

---

## 6 — Run the collision-avoidance experiment

```bash
python3 -m experiments \
    --scenario grid/3x3/contention \
    --binary baseline \
    --binary adaptive \
    --trace-out-dir /tmp/adaptive_traces
```

This runs two back-to-back simulations (~2.5 minutes each, ~5 minutes
total).  When both finish, a comparison table is printed:

```
  Variant                               Delivery  Avg witness  ...  Collisions
  -------------------------------------------------------------------------
  node_agent / grid/3x3/contention          0.0%         10.0  ...         112
  adaptive_agent / grid/3x3/contention    100.0%         16.5  ...         100
```

**The key column is Delivery.**  The baseline agent fails completely (0%)
because a structural collision pattern in the 3×3 grid means the corner
node never successfully receives the sender's advertisement — so no
message is ever dispatched.  The adaptive agent's density-aware backoff
window breaks that pattern and achieves 100% delivery.

The collision counts (112 vs ~100) are similar in absolute terms; the
difference is that the baseline collisions are *systematic* (same two
paths collide on every attempt), while the adaptive collisions are
*scattered* and the network recovers.

---

## 7 — Explore the traces in the visualiser

Open each trace in a separate browser tab to compare visually:

```bash
# Baseline run
python3 -m viz /tmp/adaptive_traces/grid_3x3_contention_topology.json \
              --trace /tmp/adaptive_traces/grid_3x3_contention_node_agent_trace.json

# Adaptive run (in a second terminal)
python3 -m viz /tmp/adaptive_traces/grid_3x3_contention_topology.json \
              --trace /tmp/adaptive_traces/grid_3x3_contention_adaptive_agent_trace.json
```

The visualiser opens automatically at `http://127.0.0.1:8050`.  Use
`--port 8051` for the second instance to avoid a port conflict.

### What to look for

- **Sidebar stats** — shows total packets, flood %, and average witness
  count (how many nodes received each packet).  In the adaptive run,
  witness count is higher because packets successfully propagate further.

- **Node heatmap** — nodes are coloured from light (few witnesses) to
  dark red (many witnesses).  In the baseline run the corner nodes stay
  pale, confirming they never receive the message.

- **Red dashed edges** — mark RF collisions on the map.  Click a packet
  in the slider at the bottom of the sidebar, then step through its hops.
  In the baseline run the same two relay edges (`n_0_1→n_0_0` and
  `n_1_0→n_0_0`) collide on nearly every attempt.  In the adaptive run
  the backoff windows are staggered so at least one relay gets through.

---

## Background

The 3×3 grid has a hard structural problem: `n_0_0` (top-left corner)
can only be reached via two relay nodes (`n_0_1` and `n_1_0`), both
equidistant from the sender (`n_2_2`).  Under the hard-collision RF
model used here, two simultaneous transmissions at a shared receiver
destroy both packets.  With no retransmit delay the two relays fire at
exactly the same time — every time.

The adaptive-delay agent assigns each relay a random backoff window
proportional to its local neighbor density, ensuring the two relays fire
at different times.  See `privatemesh/adaptive_delay/SimNode.cpp` and
`PLAN.md` for the full design rationale.
