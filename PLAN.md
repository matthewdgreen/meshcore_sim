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
| Python unit tests (251 tests, all passing) | ✅ complete |
| Example topologies (linear, star, adversarial, asymmetric hill) | ✅ complete |

### Key invariants

- No changes to MeshCore source are required or made.
- Topology JSON is backward-compatible (all new fields are optional).
- Python 3.9+ compatibility throughout.

### What the tracer can already measure

Every simulation run now emits a **Packet Path Trace** section:

- `witness_count` — how many (sender→receiver) pairs observed a given packet.
- `unique_senders` — which nodes forwarded it (flood broadcast tree shape).
- `is_flood()` — flood vs. direct routing per packet.
- Cross-hop correlation: any two nodes that saw the same fingerprint can
  confirm they saw the same message.  This is the thing to eliminate.

---

## Next steps  (prioritised)

### 1. Routing modification workflow  [NEXT]

Before writing a new protocol, establish the development loop:

- Understand exactly which functions in `Mesh.cpp` and `Dispatcher.cpp`
  control routing decisions.  Key hooks:
  - `routeRecvPacket()` — decides whether/how to rebroadcast a flood packet.
  - `allowPacketForward()` — per-node relay policy (currently `_is_relay`).
  - `getRetransmitDelay()` — jitter before rebroadcast.
  - `createDatagram()` / `sendFlood()` / `sendDirect()` — packet construction.
- Decide the patching strategy: fork the MeshCore submodule on a private
  branch, or use a `node_agent`-level overlay (e.g. a subclass of `SimNode`
  that overrides methods without touching upstream files).
- Write a minimal "canary" modification (e.g. change retransmit jitter) and
  verify that the tracer's `witness_count` changes as expected.

### 2. Scenario-based privacy regression tests

Turn the tracer output into pass/fail assertions:

```python
# e.g. in a new sim_tests/test_privacy_baseline.py
# Run a flood simulation; verify the witness count is above some threshold
# (confirming the baseline is measurable), then verify a "private" protocol
# reduces it.
```

Specific tests to write:
- **Baseline flood**: `witness_count` for a TXT_MSG in a 5-hop network
  should be ≥ N (flood reaches most nodes).
- **Direct routing**: after path learning, `witness_count` drops to exactly
  the path length.
- **Collusion test**: given K adversarial relays each recording all observed
  fingerprints, how often can they identify the sender?

### 3. Privacy protocol experiments

Candidate ideas (in order of complexity):

| Idea | Description | Breaks correlation? | Preserves routing? |
|------|-------------|--------------------|--------------------|
| **Per-hop re-encryption** | Each relay re-encrypts the payload with a fresh symmetric key, changing the fingerprint | ✅ yes | ✅ yes (needs key exchange) |
| **Onion-style layering** | Sender wraps payload in N layers; each relay peels one | ✅ yes | ✅ yes (requires path pre-knowledge) |
| **Dummy traffic** | Nodes inject cover traffic at a fixed rate | ✗ no (correlation still possible) | ✅ yes |
| **Timing randomisation** | Increase retransmit jitter to prevent timing correlation | ✗ partial | ✅ yes |
| **Path hiding** | Replace relay hash accumulation with a random tag | ✅ partial | ✅ yes (weaker path learning) |

Start with **path hiding** (lowest complexity, direct MeshCore change) to
establish the workflow, then move to per-hop re-encryption.

### 4. Adversarial test framework

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
| 2026-03-16 | Added `PacketTracer` + wire-format decoder; 251 tests |
| 2026-03-16 | Added asymmetric link support to topology |
| 2026-03-16 | Added adversarial node model (drop/corrupt/replay) |
| 2026-03-16 | Built Python orchestrator and node_agent C++ subprocess |
| 2026-03-16 | Initial project setup, MeshCore submodule, C++ tests |
