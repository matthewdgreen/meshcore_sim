# Research Plan — Privacy-Preserving Routing on MeshCore

This file records the research goals, the current state of the simulator,
and the prioritised queue of next steps.  Update it whenever a major
milestone is reached or the direction changes.  It is intentionally
committed to git so it survives local machine loss and context compaction.

---

## Research goal

Experiment with **privacy-preserving routing protocols** for LoRa mesh networks.

The central problem: in standard MeshCore flood routing, every packet carries
an identical encrypted payload at every hop, and the path field accumulates
relay hashes in order.  Any node that observes two copies of the same packet
at different points in the network can:

1. **Correlate** them (same fingerprint → same logical message).
2. **Backtrack** towards the origin (fewer relay hashes = closer to source).

The goal is to design and test routing protocols where **paths are not
explicit**, making it hard to:
- Trace the origin of a message.
- Correlate copies of the same message observed at distant network points.

Adversarial scenarios to test:
- **Passive observation**: colluding relays pool their observations to infer
  origin or destination.
- **Crafted-packet attacks**: adversary injects packets with
  adversarially-chosen nonces / payloads to probe the routing state.
- **Node collusion**: a fraction of relays share all their received packets.

Success criteria:
- Routing still works (low message loss rate, minimal fallback to flood).
- Adversary gains little information even when controlling K relays.

---

## Simulator state  (as of 2026-03-18)

### What exists

| Component | Status |
|-----------|--------|
| `node_agent` C++ subprocess per node | ✅ complete |
| Python orchestrator (router, loss, latency, adversarial) | ✅ complete |
| Asymmetric link support (`a_to_b` / `b_to_a` overrides) | ✅ complete |
| Adversarial nodes (drop / corrupt / replay, per-probability) | ✅ complete |
| `PacketTracer` — per-packet path & witness analysis | ✅ complete |
| `packet.py` — pure-Python MeshCore wire-format decoder | ✅ complete |
| C++ unit tests (crypto shims, packet serialisation) | ✅ complete |
| Python unit / integration tests (354 tests, all passing) | ✅ complete |
| Example topologies (linear, star, adversarial, asymmetric hill) | ✅ complete |
| Grid topology generator (`topologies/gen_grid.py`) | ✅ complete |
| Pre-generated 10×10 grid topology (`topologies/grid_10x10.json`) | ✅ complete |
| Path exchange in `SimNode` — flood out, direct return | ✅ complete |
| Grid routing integration tests (3×3 flood→direct, 5×5 smoke) | ✅ complete |
| Privacy baseline tests (`test_privacy_baseline.py`, 20 tests) | ✅ complete |
| `privatemesh/` research sandbox — multi-experiment layout, per-experiment binary | ✅ complete |
| `privatemesh/nexthop/` — proactive next-hop routing table experiment | ✅ complete |
| `experiments/` framework — `Scenario`, `SimResult`, `ComparisonTable`, CLI | ✅ complete |
| `HopRecord.size_bytes` + `PacketTrace.avg_size_bytes` — wire-format packet size tracking | ✅ complete |
| `RoomServerNode` — `SimNode` subclass that re-broadcasts TXT_MSG to all contacts | ✅ complete |
| Per-node `binary` field — mixed topologies with different node binaries | ✅ complete |
| `demo/room_server_demo.py` — interactive 10×10 grid room-server demo | ✅ complete |
| `tools/fetch_topology.py` — scrape live meshcore-mqtt-live-map → topology JSON | ✅ complete |
| `tools/README.md` — full auth guide, CLI reference, caveats for the scraper | ✅ complete |
| Large-topology FD fix — `_raise_fd_limit()` + batched subprocess spawning in orchestrator | ✅ complete |
| Geo coordinates (`lat`/`lon`) on topology nodes — carried through from scraper for visualisation | ✅ complete |
| `viz/` — Phase 1 static topology viewer (geo map + force-directed, node labels, hover info) | ✅ complete |
| `viz/` — Phase 2 trace overlay: witness heatmap, packet slider, sender/receiver highlight | ✅ complete |
| `--trace-out FILE` flag on orchestrator — exports `PacketTracer` data to JSON for viz | ✅ complete |
| `viz/` — Play/Pause animation with speed control (0.5×–5×) through packet sequence | ✅ complete |
| `viz/` — Hop-by-hop step-through: second slider zooms in on individual (sender→receiver) links | ✅ complete |
| `viz/` — "animate hops" checkbox: Play/Pause drives hop slider for full hop-level playback | ✅ complete |
| `viz/` — Trace validation: mismatch warning when trace topology/nodes don't match loaded topology | ✅ complete |
| `tracer.to_dict()` — embeds `topology` (filename) and `nodes` list for cross-checking in viz | ✅ complete |
| `HopRecord.tx_id` — monotonic counter groups all deliveries from the same broadcast; `to_dict()` emits it | ✅ complete |
| `viz/` — Phase 4 broadcast-aware hop display: hop slider steps through broadcast events (one sender → N receivers per step) | ✅ complete |
| `viz/` — per-packet witness map toggle (global heatmap vs. per-packet binary coloring); progressive witness reveal in hop-animate mode | ✅ complete |
| `orchestrator/airtime.py` — LoRa on-air time formula (Semtech AN1200.13); SF/BW/CR/preamble parameterised | ✅ complete |
| `orchestrator/channel.py` — RF contention model: hard collision + LoRa capture effect (log-distance path-loss); `ChannelModel` class | ✅ complete |
| `--rf-model none\|airtime\|contention` CLI flag; `radio` topology section; `RadioConfig` dataclass with MeshCore defaults (SF10/BW250/CR4-5) | ✅ complete |
| `topologies/grid_10x10.json` — corrected `radio` section to SF10/BW250/CR4-5 (matches MeshCore source) | ✅ complete |
| `tools/fetch_topology.py` — always emits `radio` section; `--sf`, `--bw-hz`, `--cr` CLI flags for override | ✅ complete |
| `EXAMPLES.md` — catalogue of 14 worked simulation scenarios with exact commands and expected output | ✅ complete |

### Key invariants

- No changes to MeshCore source are required or made.
- Topology JSON is backward-compatible (all new fields are optional).
- Python 3.9+ compatibility throughout.

### Architecture decisions (locked)

**`node_agent` inherits from `mesh::Mesh` only — will not incorporate `BaseChatMesh`.**

`SimNode` skips `BaseChatMesh` deliberately: it gives us direct control over all
routing hooks (`onPeerDataRecv`, `onPeerPathRecv`, `allowPacketForward`,
`getRetransmitDelay`) without inheriting retry timers, ACK state machines, or
application-level channel logic.  Adding `BaseChatMesh` would make instrumentation
and routing experiments significantly harder.

**`RoomServerNode` is implemented as a `SimNode` subclass — not a separate binary.**

For use cases that need application-layer behaviour (room servers, bot nodes),
we subclass `SimNode` directly rather than introducing `BaseChatMesh`.
`RoomServerNode` (`node_agent/SimNode.h/.cpp`) overrides `onPeerDataRecv` to:
1. Call the base handler (emits `recv_text`, handles path exchange).
2. Emit a `room_post` JSON event so the orchestrator can surface the message.
3. Forward `"[sender]: text"` to every other known contact via `sendTextTo`.

Activated at runtime with the `--room-server` flag; topology JSON uses
`"room_server": true` on a node entry.

**A future `app_node_agent` binary remains planned for heavier application stacks.**

When it becomes necessary to simulate Companion clients or other firmware that
requires `BaseChatMesh` / FILESYSTEM / RTClib.h, a *separate* `app_node_agent/`
directory will contain a second executable.  It will:
- Speak the same stdin/stdout JSON protocol as `node_agent` (see Protocol Spec below).
- Be invoked by specifying `"binary": "./app_node_agent/build/app_node_agent"` on
  individual nodes in the topology JSON.
- Share the `arduino_shim/` and `crypto_shim/` directories with `node_agent`.

Mixed topologies (some nodes running `node_agent`, others `app_node_agent`) are
fully supported by the orchestrator today via the per-node `binary` field.

---

### Node ↔ Orchestrator Protocol Specification

All communication is **newline-delimited JSON** over the node's **stdin** (commands
from orchestrator) and **stdout** (events from node).  Stderr is ignored.

#### Commands (orchestrator → node, via stdin)

| `type` | Other fields | Description |
|--------|-------------|-------------|
| `time` | `epoch: int` | Set the simulated Unix epoch.  Sent once at startup before `ready`. |
| `rx` | `hex: str`, `snr: float`, `rssi: float` | Deliver a received packet (hex-encoded bytes). |
| `send_text` | `dest: str`, `text: str` | Send an encrypted text message; `dest` is a pub-key hex prefix. |
| `advert` | `name: str` | Broadcast a self-advertisement with the given display name. |
| `quit` | — | Shut down cleanly. |

#### Events (node → orchestrator, via stdout)

| `type` | Other fields | Description |
|--------|-------------|-------------|
| `ready` | `pub: str`, `is_relay: bool`, `role: str`, `name: str` | Node is initialised; `pub` is the 64-hex public key; `role` is `"endpoint"`, `"relay"`, or `"room-server"`. |
| `tx` | `hex: str`, `len: int` | Node is transmitting a packet; orchestrator routes it to neighbours. |
| `recv_text` | `from: str`, `name: str`, `text: str` | A decrypted text message was received. |
| `room_post` | `from: str`, `name: str`, `text: str` | Room-server only: a TXT_MSG arrived and has been forwarded to all other contacts. |
| `advert` | `pub: str`, `name: str` | A peer advertisement was received and processed. |
| `ack` | `crc: int` | An ACK was received for a previously sent packet. |
| `log` | `msg: str` | Informational log line (debug use). |

Any future node binary (`app_node_agent` or otherwise) **must** implement all
commands and emit at minimum `ready` and `tx` events to interoperate with the
orchestrator.  Additional event types are ignored by the orchestrator unless
explicitly handled.

### What the tracer can already measure

Every simulation run now emits a **Packet Path Trace** section:

- `witness_count` — how many (sender→receiver) pairs observed a given packet.
- `unique_senders` — which nodes forwarded it (flood broadcast tree shape).
- `is_flood()` — flood vs. direct routing per packet.
- Cross-hop correlation: any two nodes that saw the same fingerprint can
  confirm they saw the same message.  This is the thing to eliminate.

---

## Next steps  (prioritised)

### 1. Routing modification workflow  [✅ DONE]

The development loop is established:

- Key hooks mapped: `routeRecvPacket`, `allowPacketForward`, `getRetransmitDelay`,
  `onPeerDataRecv`, `onPeerPathRecv`, `createPathReturn`, `sendFlood`, `sendDirect`.
- Patching strategy decided: `SimNode` inherits directly from `Mesh` (skips
  `BaseChatMesh`), so all routing logic lives in our files without touching upstream.
- Canary modifications verified:
  - `getRetransmitDelay` overridden to 0 → flood propagation now instant.
  - Path exchange added to `onPeerDataRecv` → first message floods, subsequent
    messages are direct.  Confirmed by `test_grid_routing.py` asserting
    `route=FLOOD` for trace[0] and `route=DIRECT` for traces[1] and [2].

### 2. Scenario-based privacy regression tests  [✅ DONE]

`sim_tests/test_privacy_baseline.py` — 20 tests across 3 classes:

- **`TestFloodExposureBaseline`**: single flood message in a zero-loss 3×3 grid.
  Asserts flood reaches all nodes, multiple senders share fingerprint,
  path_count grows with hop distance, source identified by zero path_count.
- **`TestCollusionAttack`**: K colluding passive relay nodes.
  Asserts single relay observes flood, two colluders see identical fingerprint,
  colluders can infer source proximity from path_count, full relay collusion
  covers every hop.
- **`TestDirectRoutingPrivacyReduction`**: compares flood vs direct witness counts.
  Asserts direct has fewer witnesses, ≥2× reduction ratio, residual relay
  exposure on direct path, witness_count bounded by grid edge count.

### 3. Room server + interactive demo  [✅ DONE]

`RoomServerNode` (C++) and `demo/room_server_demo.py` (Python):

- `RoomServerNode` subclasses `SimNode`; on receiving `TXT_MSG` it calls the
  base handler (path exchange, `recv_text` event), emits `room_post`, then
  calls `sendTextTo` for every other contact with `"[sender]: text"`.
- Protected members (`_contacts`, `_search_results`, `emitLog`, `emitJson`)
  moved from private to protected in `SimNode` to support subclassing.
- `--room-server` CLI flag; `NodeConfig.room_server` field in topology JSON.
- `NodeState.role` populated from `ready` event (`"endpoint"/"relay"/"room-server"`).
- `demo/room_server_demo.py`: 10×10 relay grid, room server at `n_0_0`,
  alice/bob/carol at the other three corners; interactive REPL.

Run with:  `python3 -m demo.room_server_demo`

### 4. Privacy protocol experiments  [IN PROGRESS]

#### nexthop experiment results  (10×10 grid, seed=42, 3 rounds, 2% link loss)

`privatemesh/nexthop/nexthop_agent` vs `node_agent/node_agent`:

| Metric | node_agent | nexthop_agent | Change |
|--------|-----------|---------------|--------|
| Delivery rate | 66.7% | 66.7% | 0% (same) |
| Avg witness count (TXT_MSG) | 139.3 | 43.0 | **−69%  (3.24× reduction)** |
| First-message witness count | 351 (flood) | 64 (table-direct) | **−82%  (5.48× reduction)** |
| Second-message witness count | 63 (path-exchange) | 1 | **−98%** |
| Avg latency | 419 ms | 413 ms | −1% |
| Avg packet size | 46.9 B | 46.3 B | −0.6 B |

**Interpretation:** the routing table allows the sender to use direct routing from the
*first* message (bypassing the flood→path-exchange round-trip), giving a 5.48× witness
count reduction on the first message.  By the second message, path exchange has
completed and both variants route directly — but nexthop's first-message direct route
leaves far fewer witnesses because it never floods.

**Known limitations observed:**
- `RT_MAX_ROUTES=128` covers a 100-node network; degrades gracefully (to baseline flood)
  for networks larger than `RT_MAX_ROUTES` peers.  No catastrophic delivery failure
  because Strategy C (flood suppression) is disabled.
- 67% delivery rate is a baseline characteristic of the 10×10 scenario (link loss +
  timing) — not introduced by nexthop.  Both variants fail the same fraction of messages.
- Strategy C (metric-horizon flood suppression) was designed but disabled due to a
  geometric design defect: horizon distances are relay-relative, not source-relative.
  See `privatemesh/nexthop/README.md` for three correct alternative designs.

#### Baseline metrics to beat  (3×3 zero-loss grid, seed=42)

Measured by `test_privacy_baseline.py`.  Any privacy-preserving protocol must
improve on at least one of these figures without breaking message delivery.

| Metric | Baseline | Attack enabled |
|--------|----------|----------------|
| Flood `witness_count` | **22** (12 edges; some traversed >1×) | Same fingerprint at every hop → cross-node correlation |
| Flood node coverage | **100%** (9/9 nodes) | Any passive relay is a full observer |
| Relay observer rate | **100%** (7/7 relays) | Entire relay network is a threat |
| `path_count` range | **0–3** (source=0, corner=3) | Source identifiable by path_count=0; proximity inferred from count |
| Direct `witness_count` | **14** (4 relay senders on path) | Residual exposure on direct path |
| Flood→direct reduction ratio | **1.6×** (22→14) | Direct routing alone is insufficient |

**Targets for a successful privacy protocol:**
- Flood correlation broken: each hop presents a distinct fingerprint (no cross-node linking)
- Source unlinkability: no hop carries a field that identifies origin (eliminate path_count=0 signal)
- Direct path exposure ≤ actual path length (eliminate broadcast spillover to non-path nodes)
- Message delivery rate ≥ 95% (must not break routing)

#### Candidate approaches (in order of complexity)

| Idea | Breaks correlation? | Hides source? | Preserves routing? |
|------|--------------------|--------------|--------------------|
| **Path hiding** — replace relay hash accumulation with a random tag | ✗ no | ✅ yes (no path_count=0 signal) | ✅ yes (weaker path learning) |
| **Per-hop re-encryption** — relay re-encrypts payload with fresh symmetric key | ✅ yes | ✅ partial | ✅ yes (needs key exchange) |
| **Onion-style layering** — N encryption layers, each relay peels one | ✅ yes | ✅ yes | ✅ yes (requires path pre-knowledge) |
| **Dummy traffic** — nodes inject cover packets at fixed rate | ✗ no | ✗ no | ✅ yes |
| **Timing randomisation** — increase retransmit jitter | ✗ partial | ✗ no | ✅ yes |

Start with **path hiding** (lowest complexity, directly addresses the
path_count=0 source-identification attack) to establish the modify → test →
measure workflow, then move to per-hop re-encryption to break correlation.

### 5. Topology & trace visualisation tool  (`viz/`)  [Phase 1 + 2 ✅ DONE]

A standalone visualisation tool, entirely self-contained in a `viz/` subdirectory.
It imports nothing from the orchestrator and does not affect the simulator in any way.
It reads topology JSON files and optional trace files produced by the simulator.

**Isolation contract:**
- Lives exclusively under `viz/` — no imports from `orchestrator/` or `sim_tests/`.
- Has its own `requirements.txt`; core simulator has zero new dependencies.
- The existing test suite (`python3 -m sim_tests`) does not import or exercise `viz/`.

**Toolchain:** Dash + Plotly + dash-cytoscape (all pure-Python, pip-installable).

#### Phase 1 — Static topology viewer

- Load any topology JSON and render it interactively in a browser tab.
- **Geo-aware layout** (when `lat`/`lon` are present on nodes): plot on a
  Plotly scattermapbox using OpenStreetMap tiles.  Nodes coloured by role
  (relay = blue, room-server = gold, endpoint = grey); edges drawn as lines
  with thickness ∝ link count (for live-map topologies).
- **Force-directed layout** (synthetic / no-coordinate topologies): use
  dash-cytoscape's built-in `cose` layout.  Hover tooltip shows node name,
  role, edge loss, latency, SNR.
- CLI entry point: `python3 -m viz <topology.json>` → opens browser.

#### Phase 2 — Packet trace overlay

Reads the `PacketTracer` JSON export (to be added to the orchestrator) alongside
the topology file.

- **Witness count heatmap**: colour each node by how many packets it witnessed.
  High count = high privacy exposure.
- **Flood vs direct animation**: step through packets in time order; show
  which nodes forwarded each one, distinguishing flood broadcasts from direct
  links.
- **Time slider** to scrub through the simulation.
- Summary panel: message delivery rate, mean witness count, flood vs direct
  ratio — the exact metrics from `test_privacy_baseline.py`.

#### Phase 3 — Privacy protocol comparison

Side-by-side view of two trace files (baseline vs. modified routing).

- Diff panel: Δ witness_count, Δ source-unlinkability score, Δ delivery rate.
- Useful for quickly checking whether a candidate routing modification
  improves privacy without regressing delivery.

#### Phase 4 — Broadcast-aware hop display  [✅ DONE]

#### Phase 5 — Destination annotation  [POSSIBLE FUTURE]

The viz currently shows the originating sender of each packet but not the
intended final destination.  The destination is encrypted inside the payload
and cannot be recovered from the wire bytes alone.

To add it, the orchestrator would intercept at send time: just before
`NodeAgent.send_text(dest_prefix, text)` is called, queue a
`(sender_name → dest_pub)` pending entry; when the resulting `tx` event
fires in `_on_tx`, consume the entry and pass `dest_node` to
`tracer.record_tx`.  `PacketTrace` and `to_dict()` would then gain a
`dest_node` field that the viz can display alongside `first_sender`.

Caveat: this only covers packets originating from the `TrafficGenerator`.
Packets from other sources (e.g. room server forwards, demo scripts) would
need their own hookup points.

Currently the trace stores one `HopRecord` per `(sender, receiver)` pair.
A single LoRa broadcast from node A to neighbours B, C, D produces three
separate hop entries, which the hop slider steps through sequentially — as
if A transmitted three times in a row.  In reality it was one on-air event.

Work items:
- ~~Add a `tx_id` counter to `PacketTracer`; increment it in `record_tx` and
  store it on every `HopRecord` created by the corresponding `record_rx` calls.~~  ✅ done
- ~~Update `to_dict()` to include `tx_id` on each hop object.~~  ✅ done
- ~~In the viz hop slider, group hops by `tx_id`; each "step" advances one
  `tx_id` group rather than one individual `(sender→receiver)` pair.
  Highlight the sender in orange and all receivers in that group in green
  simultaneously, giving an accurate picture of the broadcast event.~~  ✅ done

### 6. RF physical layer fidelity  [✅ DONE]

#### 6a. Airtime modelling  [✅ DONE]

`orchestrator/airtime.py` — `lora_airtime_ms(sf, bw_hz, cr, payload_bytes,
preamble_symbols, crc, explicit_header)` implements the full Semtech AN1200.13
formula, including the low-data-rate optimisation (LDR enabled automatically
when `t_sym >= 16 ms`, i.e. SF11/SF12 at BW125).

`--rf-model airtime`: each packet delivery is delayed by
`tx_end + latency_ms/1000 - now`, where `tx_end = tx_start + airtime_ms/1000`.
The tracer stores `airtime_ms` on each `HopRecord` and emits it in trace JSON.

At the MeshCore defaults (SF10 / BW250 kHz), a 40-byte packet takes ~330 ms
on-air — comparable to multi-hop propagation delays in dense topologies.

#### 6b. RF contention / channel occupancy  [✅ DONE]

`orchestrator/channel.py` — `ChannelModel` tracks active TX windows.
`--rf-model contention`: two transmissions that overlap in time AND both reach
the same receiver cause a collision; both packets are dropped.

**LoRa capture effect** (when `lat`/`lon` are present on all nodes): if the
primary signal is ≥ 6 dB stronger than the interferer (log-distance path-loss
model, exponent 3.0), the primary packet survives.  Without position data,
every overlap is treated as a hard collision.

`metrics.py` exposes `collision_count`; the report prints
`RF collisions dropped: N` alongside `Link-level packet loss: N`.

`sim_tests/test_airtime.py` — 19 tests covering the airtime formula spot-checks
(verified against Semtech calculator), hard collision detection, non-overlap
cases, interferer reachability, window expiry, and capture-effect outcomes.

#### 6c. Collision visibility in trace and viz  [✅ DONE]

`CollisionRecord` dataclass added to `tracer.py` (parallel to `HopRecord`).
`PacketTrace.collisions` list accumulates all RF collision events for a packet.
`PacketTracer.record_collision(sender, receiver, hex_data, t, tx_id)` records
a failed delivery; `PacketRouter._deliver_to` calls it immediately before the
collision-check `return`.  `to_dict()` emits `"collisions"` alongside `"hops"`
in every packet object; schema bumped to version 2.

Viz changes:
- **Geo map**: red line overlay on collided edges (stepped with the hop slider,
  all shown when step_idx = −1).  Always emitted as an empty trace when there
  are no collisions, preserving `uirevision` trace count for pan/zoom stability.
- **Cytoscape**: dashed red edge stylesheet entries for collided pairs.
- **Packet info panel**: `Collisions: N` in red when N > 0.
- **Broadcast-step panel**: `Collisions: N` in red for the current tx_id step.
- **Sidebar legend**: `━ collision` swatch added alongside sender/receiver/witnessed.

12 new tests in `test_tracer.py` covering `CollisionRecord`, `record_collision`,
defensive handling, `to_dict` schema v2, and independence from `witness_count`.

### 7. Adversarial test framework

Extend the adversarial node model to support:
- **Passive observer**: records all packets and makes them available for
  post-simulation analysis (already possible via tracer).
- **Colluding observers**: multiple adversarial nodes pool their fingerprint
  lists; compute joint information gain.
- **Active prober**: sends crafted packets with chosen nonces to test
  whether a victim node responds in a distinguishable way.

The colluding observer case is already almost expressible: at the end of a
simulation, `tracer.traces` contains all observed packets; you can filter
by `unique_receivers` to see which adversarial nodes saw which packets.

---

## Open questions

1. Does MeshCore's path hash (1-byte truncated hash) provide meaningful
   unlinkability, or do collisions make it exploitable?
2. Is ECDH shared-secret reuse across messages a privacy leak?  (If an
   adversary can correlate `(dest_hash, src_hash)` pairs, it can build a
   social graph even without decrypting payloads.)
3. What is the right threat model — local passive adversary (one colluding
   relay) vs. global passive adversary (all relays collude)?
4. **Strategy C geometry**: how to make metric-horizon flood suppression
   work without access to the source's position?  Three candidate approaches
   documented in `privatemesh/nexthop/README.md`.
5. **Routing-table scaling**: for networks larger than `RT_MAX_ROUTES`, the
   privacy benefit degrades to baseline (flood).  Two long-term options:
   (a) network partitioning — assign nodes a partition ID so each node
   only needs a routing table for its local partition (~50–100 nodes), with
   inter-partition routing handled by designated border relays; (b) dynamic
   table sizing based on observed advert count.  Option (a) may also address
   the 1-byte relay hash collision problem since hashes only need to be unique
   within a partition.

---

## Change log

| Date | Change |
|------|--------|
| 2026-03-16 | `tools/README.md` — full auth guide and CLI reference for scraper; FD-limit fix for large topologies |
| 2026-03-17 | RF physical-layer model: `--rf-model airtime\|contention`; `airtime.py` (Semtech AN1200.13); `channel.py` (hard collision + capture effect); `RadioConfig` defaults corrected to SF10/BW250/CR4-5; `grid_10x10.json` updated; `fetch_topology.py` gains `--sf/--bw-hz/--cr` and always emits `radio` section; 19 new tests |
| 2026-03-18 | `privatemesh/nexthop/` — proactive next-hop routing table experiment; 3.24× witness reduction on 10×10 grid at equal delivery rate; `experiments/` comparison framework; packet size tracking (`HopRecord.size_bytes`); 354 tests |
| 2026-03-17 | `EXAMPLES.md` — 14 worked simulation scenarios covering all topology types, RF models, collision viz, live network import, room server demo, and report comparison |
| 2026-03-17 | `viz/` — per-packet witness map toggle; progressive witness reveal in hop-animate mode; fixed global heatmap overlay; default to per-packet + animate-hops |
| 2026-03-17 | `viz/` — Phase 4: hop slider now steps through broadcast events (one sender → N receivers); uses `tx_id` grouping |
| 2026-03-17 | `tracer` — `HopRecord.tx_id`: monotonic counter groups all deliveries from the same broadcast event; emitted in trace JSON |
| 2026-03-17 | `viz/` — hop-by-hop step-through slider; Play/Pause drives hop animation; "animate hops" checkbox; trace mismatch validation; trace JSON now embeds topology name + node list |
| 2026-03-17 | `viz/` Phase 2 — witness-count heatmap, packet step-through slider, sender/receiver highlight; Play/Pause with speed control; `--trace-out` flag on orchestrator |
| 2026-03-17 | `viz/` Phase 1 — static topology viewer with geo map (OpenStreetMap) and force-directed layouts; shortened node labels; hover detail panel |
| 2026-03-16 | `viz/` subdirectory planned — static topology viewer + trace overlay (Dash + Plotly + dash-cytoscape) |
| 2026-03-16 | `tools/fetch_topology.py` — live network scraper for meshcore-mqtt-live-map |
| 2026-03-16 | `RoomServerNode` + interactive 10×10 demo + integration tests; 310 tests |
| 2026-03-16 | Privacy baseline tests: flood exposure, collusion attack, direct reduction |
| 2026-03-16 | Per-node `binary` field; `default_binary` rename; protocol spec; arch decision recorded |
| 2026-03-16 | Grid topology generator, path exchange in SimNode, grid routing tests |
| 2026-03-16 | Added `PacketTracer` + wire-format decoder; 251 tests |
| 2026-03-16 | Added asymmetric link support to topology |
| 2026-03-16 | Added adversarial node model (drop/corrupt/replay) |
| 2026-03-16 | Built Python orchestrator and node_agent C++ subprocess |
| 2026-03-16 | Initial project setup, MeshCore submodule, C++ tests |
