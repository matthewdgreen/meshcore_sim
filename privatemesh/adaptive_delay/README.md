# adaptive_delay — Density-Adaptive Transmit-Delay Experiment

## Hypothesis

MeshCore flood routing suffers from **collision storms**: when multiple
relay nodes hear the same packet simultaneously, they all retransmit at
once (or close to it), causing RF collisions that corrupt each other's
transmissions.

The proposal (*"An Automatic and Optimized Collision Mitigation Strategy
Using the Existing Timing Randomization Role of rxdelay, txdelay and
direct.txdelay in MeshCore Routing"*, Privitt et al., 2026) argues that
**density-adaptive random backoff** reduces these collisions with no change
to the MeshCore wire protocol:

- Count **active neighbors** (peers heard via Advert flood).
- Look up `txdelay` and `direct.txdelay` in a density table.
- Before each flood retransmission, wait a **uniform random delay** in
  `[0, 5 × LORA_AIRTIME_MS × txdelay]`.

The expected result: with even a modest `txdelay` of 1.0–1.3, the
probability that any two relays pick the same time slot drops below 20%.

## Parameters

### Density table (from proposal §9.4.2)

| Neighbor count | Density class | `txdelay` | `direct.txdelay` | Max flood window |
|:--------------:|:-------------:|:---------:|:----------------:|:----------------:|
| 0              | Sparse        | 1.0       | 0.4              | 1650 ms          |
| 1              | Sparse        | 1.1       | 0.5              | 1815 ms          |
| 2–3            | Sparse        | 1.2       | 0.6              | 1980 ms          |
| 4–5            | Medium        | 1.3       | 0.7              | 2145 ms          |
| 6–7            | Medium        | 1.5       | 0.7              | 2475 ms          |
| 8              | Medium        | 1.7       | 0.8              | 2805 ms          |
| 9              | Dense         | 1.8       | 0.8              | 2970 ms          |
| 10             | Dense         | 1.9       | 0.9              | 3135 ms          |
| 11             | Regional      | 2.0       | 0.9              | 3300 ms          |
| 12+            | Regional      | 2.1       | 0.9              | 3465 ms          |

*Max flood window = 5 × LORA_AIRTIME_MS × txdelay, with LORA_AIRTIME_MS = 330 ms.*

### Key constant

```cpp
#define LORA_AIRTIME_MS  330.0f
```

This matches the MeshCore default modulation: **SF10 / BW250 kHz / CR4-5**.
Update this value if experimenting with different LoRa settings.

## Delay formula

```
getRetransmitDelay(packet):
    td = direct_txdelay  if packet.isRouteDirect()
       = txdelay         otherwise
    return uniform_random(0, 5 × LORA_AIRTIME_MS × td)  [ms]
```

This generates a uniformly distributed random delay in milliseconds.
The probability that two nodes independently pick overlapping slots of
width `LORA_AIRTIME_MS` is approximately:

```
P(collision pair) ≈ 1 / (5 × txdelay)
```

For `txdelay=1.0`: P ≈ 20%.  For `txdelay=2.0`: P ≈ 10%.

## Observability

Each time the neighbor count changes, `adaptive_agent` emits:

```json
{"type":"txdelay_update","neighbor_count":4,"txdelay":1.30,"direct_txdelay":0.70}
```

This event is silently ignored by the orchestrator but visible in debug
logs and trace output, confirming that tuning is happening at runtime.

## What is not implemented

- **`rxdelay`** (receive-window randomisation): this requires changes to
  the `SimRadio` layer and is outside the scope of the initial experiment.
- **Coding-rate adjustment** (CR5/CR6/CR8 by density class): requires
  radio reconfiguration commands not present in the current wire protocol.
- **Automatic neighbor scrubbing** (removing stale contacts): the
  simulator uses a static topology, so staleness does not arise.
- **`rxbusy` deferral**: proposal §3.8 (listen-before-talk); requires
  carrier-sense in `SimRadio`, which is not currently modelled.

## Running the experiment

Build:

```bash
cd privatemesh/adaptive_delay
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

Compare baseline vs adaptive on the 3×3 contention scenario (~80 s):

```bash
python3 -m experiments \
    --scenario grid/3x3/contention \
    --binary baseline \
    --binary adaptive
```

Or run all scenarios:

```bash
python3 -m experiments
```

Save traces for visualisation:

```bash
python3 -m experiments \
    --scenario grid/3x3/contention \
    --trace-out-dir /tmp/adaptive_traces
python3 -m viz /tmp/adaptive_traces/grid_3x3_contention_topology.json \
              --trace /tmp/adaptive_traces/grid_3x3_contention_node_agent_trace.json
python3 -m viz /tmp/adaptive_traces/grid_3x3_contention_topology.json \
              --trace /tmp/adaptive_traces/grid_3x3_contention_adaptive_agent_trace.json
```

## Expected results

| Metric              | `node_agent` (baseline)    | `adaptive_agent`            |
|---------------------|----------------------------|-----------------------------|
| Delivery rate       | low (collisions kill msgs) | higher (fewer collisions)   |
| Collision count     | high                       | low/zero                    |
| Avg latency         | low (immediate retransmit) | higher (random backoff)     |
| Flood witness count | same (routing unchanged)   | same (routing unchanged)    |

The **delivery/collision tradeoff** is the core metric.  The adaptive
variant accepts higher latency in exchange for reliable delivery.

## Files changed vs `node_agent`

Only `SimNode.h` and `SimNode.cpp` differ.  The patch adds:

| Addition | Purpose |
|----------|---------|
| `LORA_AIRTIME_MS` macro | Scales timing slots to milliseconds |
| `DENSITY_TABLE[]` | Neighbor-count → txdelay lookup |
| `_txdelay`, `_direct_txdelay` fields | Current timing multipliers |
| `_prev_neighbor_count` field | Detects contact-list changes |
| `_update_timing()` method | Recomputes multipliers from contact count |
| `getRetransmitDelay()` override | Returns random delay from density table |
| `onAdvertRecv()` override | Calls base + `_update_timing()` |

`main.cpp`, `SimRadio.cpp`, `SimClock.cpp`, and `SimRNG.cpp` are
**identical** to `node_agent`.

## Known limitations

- **Timing is in real asyncio time**: the 330 ms/slot delays run in
  real wall-clock time inside the simulator.  Large topologies with many
  hops can make scenarios slow (18-hop 10×10 grid ≈ 40 s per message).
- **Static txdelay after warmup**: in a static simulation topology,
  neighbor count stabilises after the first advert flood.  The tuning
  does not change after that.  A real deployment would retune as nodes
  come and go.
- **No capture effect in test topology**: synthetic grid topologies have
  no `lat`/`lon`, so the collision model uses hard collision (not the
  log-distance capture effect).  Real deployments benefit from the
  capture effect, which reduces collisions further.
