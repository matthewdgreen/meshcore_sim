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

## Simulator state  (as of 2026-03-16)

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
| Python unit / integration tests (310 tests, all passing) | ✅ complete |
| Example topologies (linear, star, adversarial, asymmetric hill) | ✅ complete |
| Grid topology generator (`topologies/gen_grid.py`) | ✅ complete |
| Pre-generated 10×10 grid topology (`topologies/grid_10x10.json`) | ✅ complete |
| Path exchange in `SimNode` — flood out, direct return | ✅ complete |
| Grid routing integration tests (3×3 flood→direct, 5×5 smoke) | ✅ complete |
| Privacy baseline tests (`test_privacy_baseline.py`, 20 tests) | ✅ complete |
| `RoomServerNode` — `SimNode` subclass that re-broadcasts TXT_MSG to all contacts | ✅ complete |
| Per-node `binary` field — mixed topologies with different node binaries | ✅ complete |
| `demo/room_server_demo.py` — interactive 10×10 grid room-server demo | ✅ complete |

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

### 4. Privacy protocol experiments  [NEXT]

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

### 5. Adversarial test framework

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

---

## Change log

| Date | Change |
|------|--------|
| 2026-03-16 | `RoomServerNode` + interactive 10×10 demo + integration tests; 310 tests |
| 2026-03-16 | Privacy baseline tests: flood exposure, collusion attack, direct reduction |
| 2026-03-16 | Per-node `binary` field; `default_binary` rename; protocol spec; arch decision recorded |
| 2026-03-16 | Grid topology generator, path exchange in SimNode, grid routing tests |
| 2026-03-16 | Added `PacketTracer` + wire-format decoder; 251 tests |
| 2026-03-16 | Added asymmetric link support to topology |
| 2026-03-16 | Added adversarial node model (drop/corrupt/replay) |
| 2026-03-16 | Built Python orchestrator and node_agent C++ subprocess |
| 2026-03-16 | Initial project setup, MeshCore submodule, C++ tests |
