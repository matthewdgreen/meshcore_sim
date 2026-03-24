# meshcore_sim — Example simulation runs

A catalogue of ready-to-run scenarios.  Each example includes the goal,
the exact commands, and what to look for in the output.

All commands are run from the repository root.  The node agent must be built
first (`cd node_agent && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build && cd ..`).
The visualiser requires `pip install -r requirements.txt`.

---

## 1. Minimal linear chain — quick sanity check

**Goal:** Confirm the simulator is wired up correctly.  Three nodes in a line;
alice sends through relay1 to bob.

```sh
python3 -m orchestrator topologies/linear_three.json \
    --duration 30 --seed 42
```

**What to look for:**
- `Message delivery: N/N (100.0%)` — all messages arrived.
- Latency ≈ 21 ms — two 20 ms hops, plus tiny scheduling overhead.
- `relay1` TX count roughly double alice + bob combined (it re-broadcasts
  flood packets in both directions).

**Visualise the static topology:**

```sh
python3 -m viz topologies/linear_three.json
```

---

## 2. Linear chain — packet trace and step-through

**Goal:** Record every radio hop and explore them interactively in the browser.

```sh
# Simulate and export trace
python3 -m orchestrator topologies/linear_three.json \
    --duration 30 --seed 42 \
    --trace-out /tmp/linear_trace.json

# Open the visualiser with the trace overlay
python3 -m viz topologies/linear_three.json \
    --trace /tmp/linear_trace.json
```

**What to look for in the browser:**
- **Witness heatmap** — nodes coloured by number of packets received.
  relay1 is the hottest node (it sees everything).
- **Packet selector** — pick any packet; orange = sender, green = receivers.
- **Broadcast-step slider** — step through each on-air transmission of one
  packet to see exactly which nodes received it.
- **Packet path trace** printed to stdout at end of simulation: shows
  `ADVERT`, `TXT_MSG`, `PATH`, `ACK` witness counts.

---

## 3. Star topology — high-traffic hub-and-spoke

**Goal:** Exercise a busy hub relay with multiple endpoints at varying link
quality.  delta has a 15 % loss link and 60 ms latency.

```sh
python3 -m orchestrator topologies/star_five.json \
    --duration 120 --traffic-interval 3
```

**What to look for:**
- `hub` dominates TX and RX counts — every packet passes through it.
- Delivery rate may fall below 100 % for delta due to high loss.
- Varying latency per endpoint visible in the per-message latency list
  at the end of the report.

**Log-level debug** to see every radio delivery:

```sh
python3 -m orchestrator topologies/star_five.json \
    --duration 60 --traffic-interval 3 --log-level debug 2>&1 | head -80
```

---

## 4. Adversarial relay — drop, corrupt, and replay

**Goal:** Demonstrate how a compromised middle node degrades the network.
The topology ships with `mode: "corrupt"` and 50 % probability.

```sh
python3 -m orchestrator topologies/adversarial.json \
    --duration 60 --seed 7 \
    --report /tmp/adversarial_report.txt
```

**What to look for:**
- `Adversarial corruptions: N` in the metrics summary.
- Message delivery well below 100 % because the MAC layer rejects
  corrupted packets.
- `Adversarial drops: 0` and `Adversarial replays: 0` (this topology
  uses corrupt mode only).

**Try drop mode** — edit `adversarial.json` to set `"mode": "drop"` and
`"probability": 1.0`:

```sh
# With drop mode and 100 % probability, delivery should reach near zero:
python3 -m orchestrator topologies/adversarial.json \
    --duration 60 --seed 7
```

**Try replay mode** — set `"mode": "replay"`, `"replay_delay_ms": 2000`:

```sh
python3 -m orchestrator topologies/adversarial.json \
    --duration 60 --seed 7
# Look for "Adversarial replays: N" in the report.
# MeshCore's duplicate-suppression cache usually discards the replays,
# but they do consume airtime and may cause spurious packet traces.
```

---

## 5. Asymmetric hill relay — one-way RF link

**Goal:** Model a hill-top relay where the uplink (client → relay) is strong
but the downlink (relay → client) is weak, and one node is in a valley that
cannot hear the relay at all.

```sh
python3 -m orchestrator topologies/asymmetric_hill.json --duration 120
```

**Topology summary:**
- `base_camp` → `hill_relay`: strong uplink (SNR 14 dB), weak downlink
  (SNR 5 dB, 15 % loss).
- `deep_valley` → `hill_relay`: normal uplink; `hill_relay` → `deep_valley`:
  100 % loss (one-way link).

**What to look for:**
- `deep_valley` sends packets that the relay re-broadcasts, but never
  receives any replies.
- `base_camp` has lower delivery in the downlink direction (relay → base_camp).
- RX counts in the metrics table reflect the asymmetry clearly.

```sh
python3 -m viz topologies/asymmetric_hill.json
```

The force-directed layout in the visualiser does not model RF range, but
hover over any edge to see the per-direction SNR/RSSI/loss values.

---

## 6. 10×10 grid — 100 nodes, flood-to-direct routing transition

**Goal:** Observe MeshCore's two-phase routing: initial flood advertisements,
followed by direct (path-routed) messages once routes are learned.

```sh
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --trace-out /tmp/grid_trace.json
```

**What to look for:**
- Early packets are `ADVERT` (type 1) with many witnesses — flood phase.
- Later `TXT_MSG` packets show `route=DIRECT` in the path trace — routing
  has converged.
- The path trace section at the end shows witness count dropping as direct
  routing kicks in.

**Visualise:**

```sh
python3 -m viz topologies/grid_10x10.json --trace /tmp/grid_trace.json
```

- Enable **Animate broadcast steps** and press Play to watch the flood
  propagate outward from corner to corner across all 100 nodes.
- Switch to the **Witness heatmap** tab: interior relay nodes are the
  hottest (they forward every flood).

---

## 7. 10×10 grid — LoRa airtime delays

**Goal:** Add realistic on-air timing to the 10×10 grid.  Each packet is
held in the radio queue for the LoRa on-air time before being delivered to
neighbours.

```sh
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --rf-model airtime \
    --trace-out /tmp/grid_airtime_trace.json
```

**LoRa parameters** (from the `radio` section in `grid_10x10.json`):
SF10 / BW250 kHz / CR4/5 / 8 preamble symbols.
A 40-byte payload has ≈ 330 ms on-air time.

**What to look for:**
- End-to-end latency increases significantly (each hop adds ~330 ms).
- The path trace shows later `first_seen_at` times between hops.
- Message delivery should remain 100 % — airtime adds delay, not loss.

Compare with the baseline (no RF model) to see the latency difference:

```sh
# Baseline (no RF)
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 30 --seed 42

# With airtime delays
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 --rf-model airtime
```

---

## 8. 10×10 grid — RF contention and collision visualisation

**Goal:** Observe real RF collisions.  When multiple relays re-broadcast the
same flood packet simultaneously, shared neighbours see overlapping airtime
windows and the packets collide.

```sh
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 60 --seed 42 \
    --rf-model contention \
    --trace-out /tmp/grid_collision_trace.json
```

**Why the grid produces collisions:**
- `latency_ms` is 0 in the default grid, so all direct neighbours receive
  a flood packet at the same simulation tick.
- All of them re-broadcast immediately (zero retransmit jitter in simulation
  mode), so multiple re-broadcasts overlap at shared neighbours.
- At SF10/BW250 a packet is on-air for ≈ 330 ms — a large overlap window.

**What to look for:**
- `RF collisions dropped: N` in the metrics summary (N > 0 with contention).
- The packet path trace shows some packets with fewer witnesses than expected.

**Visualise collisions:**

```sh
python3 -m viz topologies/grid_10x10.json \
    --trace /tmp/grid_collision_trace.json
```

- Select a flood packet from the packet list.
- Collisions appear as **dashed red edges** in both the geo map and the
  force-directed graph.
- The **Packet info** panel shows "Collisions: N" in red.
- Step through broadcast steps: each step shows the collisions belonging to
  that particular broadcast event.

**Increase collision rate** with a higher spreading factor (longer airtime):

```sh
# Generate a custom grid with SF12, BW125 (airtime ≈ 2.8 s per packet)
python3 topologies/gen_grid.py 10 10 \
    --sf 12 --bw-hz 125000 \
    -o /tmp/grid_sf12.json

python3 -m orchestrator /tmp/grid_sf12.json \
    --duration 60 --seed 42 --rf-model contention
```

---

## 9. Live network import via fetch_topology.py

**Goal:** Pull a real MeshCore relay map from a live `meshcore-mqtt-live-map`
instance and simulate it.

```sh
# Check network statistics without credentials (no auth required):
python3 tools/fetch_topology.py live.bostonme.sh --stats

# Fetch the relay backbone (authenticated):
python3 tools/fetch_topology.py live.bostonme.sh \
    --token <PROD_TOKEN> \
    --only-relays --min-edge-count 5 \
    --output topologies/boston_live.json --verbose

# Simulate the imported network:
python3 -m orchestrator topologies/boston_live.json \
    --duration 120 --seed 42 \
    --trace-out /tmp/boston_trace.json

# Visualise on the real map (nodes have lat/lon from the live data):
python3 -m viz topologies/boston_live.json --trace /tmp/boston_trace.json
```

A pre-fetched snapshot is included at `topologies/boston_relays.json`:

```sh
python3 -m orchestrator topologies/boston_relays.json \
    --duration 120 --seed 42 \
    --trace-out /tmp/boston_trace.json
python3 -m viz topologies/boston_relays.json --trace /tmp/boston_trace.json
```

**What to look for:**
- The visualiser opens in **geo mode** (OpenStreetMap tiles) because every
  node has real `lat`/`lon` coordinates.
- Hub relays with many edges are the highest-exposure nodes in the heatmap.
- The capture effect in `contention` mode uses real node distances, so
  geographically close transmitters are more likely to survive a collision.

---

## 10. Live network import — with RF contention

**Goal:** Simulate a real-world relay network with the full RF physical-layer
model including airtime delays and collision detection.

```sh
python3 -m orchestrator topologies/boston_relays.json \
    --duration 120 --seed 42 \
    --rf-model contention \
    --trace-out /tmp/boston_contention_trace.json
python3 -m viz topologies/boston_relays.json \
    --trace /tmp/boston_contention_trace.json
```

**Note:** `boston_relays.json` already includes a `radio` section with
MeshCore defaults (SF10/BW250).  The `--rf-model contention` flag uses the
node `lat`/`lon` coordinates to apply the **capture effect**: if one
transmitter's signal is at least 6 dB stronger at the receiver (log-distance
path-loss, exponent 3.0), the stronger packet survives the collision.

---

## 11. Room server interactive demo

**Goal:** Exercise MeshCore's room-server forwarding: a central node
re-broadcasts every message it receives to all its contacts, acting as a
group chat relay.

```sh
python3 -m demo.room_server_demo
```

The demo builds its own topology in memory: a 10×10 relay grid with a room
server at `n_0_0` (top-left) and three clients — alice (`n_0_9`), bob
(`n_9_0`), carol (`n_9_9`) — at the other corners.

**Interactive commands at the prompt:**

```
alice: hello everyone
bob: anyone copy?
carol: loud and clear
/help
/quit
```

Messages are routed through the relay grid to the room server, which
forwards them to the other two clients.  Received messages appear with
ANSI colour coding per sender.

**To run with a custom node agent path:**

```sh
python3 -m demo.room_server_demo \
    --binary ./node_agent/build/node_agent
```

---

## 12. Privacy baseline — flood exposure measurement

**Goal:** Quantify how many nodes witness each message type in a large
mesh — the key privacy metric for the research programme.

```sh
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --trace-out /tmp/privacy_trace.json
```

Inspect the **Packet Path Trace** section at the end of the report:

```
  Type              Count  Avg witnesses  Max witnesses
  ----------------  -----  -------------  -------------
  ADVERT                N            ...              ...
  TXT_MSG               N            ...              ...
  PATH                  N            ...              ...
  ACK                   N            ...              ...
```

- **ADVERT** packets always flood — every relay re-broadcasts them; expect
  high witness counts proportional to grid size.
- **TXT_MSG** early in the run will also flood; later ones should switch to
  direct routing with much lower witness counts.
- **High-exposure packets** listed at the bottom name the exact nodes that
  acted as senders and receivers — useful for identifying privacy hot-spots.

**Visualise the witness heatmap:**

```sh
python3 -m viz topologies/grid_10x10.json --trace /tmp/privacy_trace.json
```

The heatmap tab shows each node's total witness count: interior relays near
the centre of the grid accumulate the most witnesses and are the highest
privacy risk for a passive observer.

---

## 13. Generating custom grid topologies

**Goal:** Create grids of arbitrary size or RF parameters for scaling studies.

```sh
# 5×5 grid, low loss, 10 ms latency
python3 topologies/gen_grid.py 5 5 \
    --loss 0.01 --latency 10 \
    -o topologies/grid_5x5.json

# 20×20 grid (400 nodes) with default RF settings
python3 topologies/gen_grid.py 20 20 \
    -o /tmp/grid_20x20.json

# 10×10 grid with non-default radio settings for contention studies
python3 topologies/gen_grid.py 10 10 \
    --sf 12 --bw-hz 125000 \
    -o /tmp/grid_sf12.json

# Show all generator options
python3 topologies/gen_grid.py --help
```

Run and visualise a custom grid:

```sh
python3 -m orchestrator /tmp/grid_20x20.json \
    --duration 120 --seed 1 \
    --rf-model contention \
    --trace-out /tmp/grid_20x20_trace.json
python3 -m viz /tmp/grid_20x20.json --trace /tmp/grid_20x20_trace.json
```

---

## 14. Saving and comparing reports

**Goal:** Save metrics reports for offline comparison or regression tracking.

```sh
mkdir -p results

# Baseline — no RF model
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --report results/grid_baseline.txt

# With airtime delays
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --rf-model airtime \
    --report results/grid_airtime.txt

# With full contention
python3 -m orchestrator topologies/grid_10x10.json \
    --duration 120 --seed 42 \
    --rf-model contention \
    --report results/grid_contention.txt

# Quick diff — delivery rate and collision count
grep -E "Message delivery|RF collisions|Latency" results/grid_*.txt
```

Expected pattern:
- Delivery rate 100 % for baseline and airtime; may drop with contention
  if collisions are severe.
- `RF collisions dropped: 0` for baseline and airtime; > 0 for contention.
- Average latency much higher with airtime / contention (each hop ≈ 330 ms
  at SF10/BW250 vs. near-zero for baseline).

---

## 15. Boston real-world mesh — privacy comparison

**Goal:** Run the PNI (Permuted Neighbor Identifier) privacy experiment on the
157-node Boston mesh topology scraped from `live.bostonme.sh`, then compare
baseline vs. PNI routing using the privacy analyser and the visualiser.

The Boston topology has 155 relays with degree variance from 1 to 52, making it
a realistic stress test for privacy-preserving routing at real-world scale.

**Run with the experiments framework (easiest):**

```sh
# Run all available binaries on the Boston scenario, saving traces
python3 -m experiments --scenario boston --trace-out-dir /tmp/boston_traces

# Visualise the baseline trace on the geo map
python3 -m viz /tmp/boston_traces/boston_topology.json \
    --trace /tmp/boston_traces/boston_node_agent_trace.json

# Visualise the PNI trace
python3 -m viz /tmp/boston_traces/boston_topology.json \
    --trace /tmp/boston_traces/boston_privaterouting1_agent_trace.json
```

**Run individual variants with privacy analysis:**

```sh
# Baseline
python3 -c "
from experiments.scenarios import BOSTON, BASELINE_BINARY
from experiments.runner import run_scenario
r = run_scenario(BOSTON, BASELINE_BINARY, trace_out='/tmp/boston_baseline.json')
print(f'Delivery: {r.delivery_rate:.0%}  Hops: {r.total_hops}')
"

# PNI (privaterouting1)
python3 -c "
from experiments.scenarios import BOSTON, PRIVATEROUTING1_BINARY
from experiments.runner import run_scenario
r = run_scenario(BOSTON, PRIVATEROUTING1_BINARY, trace_out='/tmp/boston_pni.json')
print(f'Delivery: {r.delivery_rate:.0%}  Hops: {r.total_hops}')
"
```

**Analyse privacy metrics (no re-run needed if traces exist):**

```sh
python3 -c "
import json
from experiments.privacy import analyze_privacy, format_privacy_report
for label, path in [('baseline', '/tmp/boston_baseline.json'),
                    ('PNI',      '/tmp/boston_pni.json')]:
    with open(path) as f:
        trace = json.load(f)
    print(format_privacy_report(analyze_privacy(trace), f'{label} / boston'))
"
```

**Visualise a trace on the geo map:**

```sh
python3 -m viz topologies/boston_relays.json --trace /tmp/boston_pni.json
```

Expected results:
- **Path Hash Entropy** jumps from near-zero (baseline: each relay uses the
  same 1-byte hash every time) to ~1.0 (PNI: fresh random hash per packet).
- **Cross-Path Linkability** drops from ~0.60 (baseline: same hashes across
  packets are trivially correlated) to 0.00 (PNI: no shared hash values).
- **Delivery rate** should be 100 % for both baseline and PNI on this topology.
- The heatmap in the visualiser highlights high-degree relays (e.g. the
  downtown Boston cluster) that witness the most traffic — the privacy
  hot-spots that PNI is designed to protect.

**Variations:**

```sh
# Compare all five binary variants side by side
python3 -m experiments --scenario boston --trace-out-dir /tmp/boston_traces

# Run just baseline vs PNI
python3 -m experiments --scenario boston -b baseline -b pni \
    --trace-out-dir /tmp/boston_traces

# List all available scenarios and binaries
python3 -m experiments --list
```
