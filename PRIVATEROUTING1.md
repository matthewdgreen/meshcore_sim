# PRIVATEROUTING1 — Permuted Neighbor Identifiers for Path Privacy

**Branch:** `mdg/privaterouting1`
**Status:** Pre-implementation spec (v2)

---

## 1. Goal

Prevent an endpoint (or passive observer) from identifying which relay nodes
carried a packet, by replacing the truncated public-key hashes that relays
append to flood paths with random *Permuted Neighbor Identifiers* (PNIs).

**Privacy property:** a node receiving a direct-routed packet can follow the
path (each relay recognises its own PNI and forwards), but cannot determine
which physical relay occupies each hop.

**Non-goals for this patch:**
- Hiding the sender's or receiver's identity (the `src_hash` / `dest_hash`
  bytes in the payload, and advert broadcasts, remain unchanged).
- Protecting against traffic analysis, timing correlation, or radio
  fingerprinting.
- Changing the encrypted payload format (`PAYLOAD_VER_1`).

These are noted as future directions in §9.

---

## 2. Background: how paths work today

### 2.1 Path field encoding (Packet.h)

```
path_len:  bits 7-6 = (hash_size - 1)   → hash_size ∈ {1, 2, 3} bytes
           bits 5-0 = hash_count         → 0..63 hops
path[]:    hash_count × hash_size bytes, concatenated
```

`PATH_HASH_SIZE` is defined as **1** in `MeshCore.h` (labelled `// V1`),
so the default hash is the single first byte of a node's 32-byte public
key.  The `PAYLOAD_VER_2` comment reads `// FUTURE (eg. 2-byte hashes,
4-byte MAC ??)`, confirming 2-byte hashes are an anticipated upgrade.

### 2.2 Flood forwarding (Mesh.cpp — routeRecvPacket)

When a relay R receives a flood packet it is allowed to forward, MeshCore:

```cpp
// Mesh.cpp:333
self_id.copyHashTo(&packet->path[n * packet->getPathHashSize()],
                   packet->getPathHashSize());
packet->setPathHashCount(n + 1);
return ACTION_RETRANSMIT_DELAYED(packet->getPathHashCount(), d);
```

### 2.3 Direct routing (Mesh.cpp — onRecvPacket)

When a direct packet arrives:

```cpp
// Mesh.cpp:86
if (self_id.isHashMatch(pkt->path, pkt->getPathHashSize()) &&
    allowPacketForward(pkt)) {
    removeSelfFromPath(pkt);   // shift path left by hash_size bytes
    return ACTION_RETRANSMIT_DELAYED(0, d);
}
```

Both `copyHashTo` and `isHashMatch` reference `self_id.pub_key` directly.
Neither is virtual.  Direct routing is radio-broadcast — every neighbor
hears every direct packet; only the one whose hash matches `path[0]`
processes it.

### 2.4 Existing 2-byte support in the relay path

`sendFlood` already accepts `path_hash_size` as a parameter (default 1):

```cpp
void sendFlood(Packet* packet, uint32_t delay_millis=0,
               uint8_t path_hash_size=1);
```

`routeRecvPacket` reads `getPathHashSize()` from the packet at runtime.
`copyHashTo(dest, sz)` copies `sz` bytes of `pub_key`.
`isHashMatch(hash, sz)` compares `sz` bytes.  `tryParsePacket` accepts
`hash_size ∈ {1, 2, 3}`.

**Stock MeshCore firmware already handles 2-byte paths as relays without
any code changes** — the path hash size is a per-packet attribute, not a
per-node configuration.  Only the originating node needs to call
`sendFlood(pkt, delay, 2)` instead of the default.

---

## 3. MeshCore patch: two virtual hooks

Add two virtual methods to `mesh::Mesh` with default implementations that
preserve the current behaviour exactly:

```cpp
// Mesh.h — add to protected section:
virtual void writeSelfPathHash(uint8_t* dest, uint8_t sz) {
    self_id.copyHashTo(dest, sz);
}
virtual bool isSelfPathHash(const uint8_t* hash, uint8_t sz) const {
    return self_id.isHashMatch(hash, sz);
}
```

Call-site changes in `Mesh.cpp`:

| Location | Current | Patched |
|----------|---------|---------|
| `routeRecvPacket` (line 333) | `self_id.copyHashTo(...)` | `writeSelfPathHash(...)` |
| `onRecvPacket` (line 86) | `self_id.isHashMatch(...)` | `isSelfPathHash(...)` |

**Total MeshCore delta: +6 lines (2 method bodies in header, 2 call-site
edits).  No new files.  No behavioural change for any existing subclass.**

This is a proper upstream-submittable patch: the hooks are general-purpose
and useful for any future path customisation.

---

## 4. New agents in `privatemesh/`

### 4.1 `privatemesh/path2` — 2-byte path hashes, no PNI

A minimal agent identical to `node_agent` except it originates floods with
`path_hash_size = 2`.  This is the "control" for evaluating 2-byte paths
independently of the PNI mechanism.

**Implementation:** override `broadcastAdvert` and `sendTextTo` to call
`sendFlood(pkt, delay, 2)` instead of the default `sendFlood(pkt, delay, 1)`.
The relay forwarding path (`routeRecvPacket`) already reads
`getPathHashSize()` from the packet at runtime, so it handles 2-byte
correctly with no override needed.

**Estimated new logic LoC:** ~25 lines.

### 4.2 `privatemesh/privaterouting1` — 2-byte paths + PNI

Inherits from the path2 `SimNode` (or duplicates its `sendFlood` override)
and additionally overrides `writeSelfPathHash` and `isSelfPathHash` to use
a per-relay PNI table.

---

## 5. PNI table design

### 5.1 Per-packet random PNI

Each time relay R appends itself to a flood path, it generates a **fresh
random PNI** and stores it.  A static PNI per node would be a permanent
pseudonym — an observer collecting multiple flood paths would quickly learn
that PNI `0xA3B7` always means the same relay, defeating the purpose.

```cpp
void writeSelfPathHash(uint8_t* dest, uint8_t sz) override {
    uint8_t pni[3];
    do {
        getRNG()->random(pni, sz);
    } while (pniExists(pni, sz));    // avoid collision with stored PNIs
    storePNI(pni, sz);               // add to table (FIFO eviction if full)
    memcpy(dest, pni, sz);
}
```

On the direct path, recognition checks all stored PNIs:

```cpp
bool isSelfPathHash(const uint8_t* hash, uint8_t sz) const override {
    if (self_id.isHashMatch(hash, sz)) return true;   // stock-originated packets
    return pniLookup(hash, sz);                        // check PNI table
}
```

### 5.2 Data structure

```cpp
struct PNIEntry {
    uint8_t pni[3];     // 1–3 bytes depending on hash_size
    uint8_t sz;         // actual size used
};
```

A fixed-size circular buffer (not `std::unordered_map` — too much overhead
for embedded targets):

```cpp
static constexpr int PNI_TABLE_CAP = 128;
PNIEntry _pni_table[PNI_TABLE_CAP];
int      _pni_count = 0;
int      _pni_head  = 0;   // next insertion index (FIFO ring)
```

Lookup is linear scan over at most 128 entries (1–3 byte `memcmp` per entry).
On a 240 MHz ESP32, 128 iterations of a 2-byte compare takes <1 µs.

### 5.3 Table eviction

When the table is full, the oldest entry is overwritten (FIFO).  If a stale
PNI is referenced by an in-flight direct packet that hasn't reached this
relay yet, the relay will fail to recognise itself → the packet is dropped.
This is identical to the existing failure mode when a relay reboots and
loses state.  The sender retries via flood, which builds a fresh path with
fresh PNIs.

**Sizing rationale:** in the 3×3 grid, each relay forwards ~30–50 flood
packets per experiment run.  On a real MeshCore network with re-adverts
every 15 minutes, a relay might forward ~200 floods per hour.  128 entries
covers ~30 minutes of activity with margin.

### 5.4 Memory budget

| Hash size | Table cap | Bytes per entry | Total (table + count + head) |
|-----------|-----------|-----------------|------------------------------|
| 1 byte    | 128       | 2 (pni + sz)    | 260 bytes                    |
| 2 byte    | 128       | 3 (pni + sz)    | 388 bytes                    |
| 3 byte    | 128       | 4 (pni + sz)    | 516 bytes                    |

For comparison, `SimpleMeshTables`'s seen-packet table uses
`MAX_HASH_SIZE(8) × 64 = 512 bytes`.  The PNI table is comparable in size.

On ESP32 (320 KB SRAM) this is negligible.  Even on Cortex-M4 (64 KB) it
is well within budget.

---

## 6. Backwards compatibility

### 6.1 Stock relays handle 2-byte path packets correctly

As discussed in §2.4, stock relays read `getPathHashSize()` from the
packet, not from a compile-time constant.  They will correctly append
2-byte hashes when forwarding a 2-byte-path flood, and correctly match
2-byte hashes when processing a direct reply.

### 6.2 Mixed-network compatibility matrix

| Originator | Relay | Behaviour |
|------------|-------|-----------|
| path2 (2-byte) | stock (1-byte code) | ✅ Stock relay reads `getPathHashSize()=2` from packet, appends 2 bytes of its own pub_key. |
| stock (1-byte) | path2 (2-byte code) | ✅ path2 relay reads `getPathHashSize()=1` from packet, operates in 1-byte mode for that packet. |
| PNI (2-byte) | stock | ✅ Stock relay appends its real 2-byte hash; PNI relay appends a PNI. On the direct return, stock relay matches its real hash; PNI relay matches its PNI. Each relay only needs to recognise *itself*. |
| stock | PNI | ✅ PNI relay reads 1-byte mode from packet. `isSelfPathHash` checks real hash first, then PNI table. Works in either mode. |

**Key insight:** because direct packets are radio-broadcast (not unicast),
each relay only needs to recognise *itself* in `path[0]`.  A PNI relay
doesn't need to understand stock relays' hashes, and vice versa.

### 6.3 `PAYLOAD_VER` compatibility

The `path_hash_size` is encoded in `path_len`, NOT in the header's version
bits.  Changing from 1-byte to 2-byte paths does not change `PAYLOAD_VER`.
Stock firmware's `if (pkt->getPayloadVer() > PAYLOAD_VER_1) return false`
check passes for 2-byte-path packets.

---

## 7. Strengths and weaknesses

### Strengths

**S1 — Relay path is opaque to endpoints.**  The destination sees random
PNI bytes in the path.  It cannot identify which relays carried the packet
or infer the sender's approximate location from the relay chain.

**S2 — Per-packet PNI defeats simple traffic analysis.**  Unlike a static
pseudonym, each flood forward uses a fresh PNI.  An observer cannot
correlate two different packets as having traversed the same relay by
comparing path entries.

**S3 — Zero overhead for non-relay nodes.**  Endpoints don't maintain a PNI
table.  The hooks are only exercised by relay nodes.

**S4 — Mixed-network compatible.**  PNI relays coexist with stock relays on
the same network.  No flag day.  Incremental deployment.

**S5 — Tiny memory footprint.**  ~388 bytes per relay with 2-byte hashes
and 128-entry table.  Comparable to the existing seen-packet table.

**S6 — Minimal MeshCore patch.**  6 lines added (2 virtual methods +
2 call-site changes).  All new logic lives in the privatemesh subclass.

**S7 — 2-byte path hashes reduce collision probability by 256×.**  From
1/256 per pair (1-byte) to 1/65 536 (2-byte).  This is a standalone
improvement independent of PNI.

### Weaknesses

**W1 — Advert packets are still fully public.**  The 32-byte public key is
broadcast in every advert.  PNI protects the relay chain but not the
identity broadcast.

**W2 — `src_hash` / `dest_hash` leak sender/receiver identity.**  The
first bytes of the data payload are cleartext.  Combined with advert
knowledge, a passive observer can correlate messages to identities.  This
is a `PAYLOAD_VER_2` problem, outside the scope of this patch.

**W3 — PNI table is finite; stale entries can cause direct routing failure.**
If the table overflows and evicts a PNI referenced by an in-flight direct
packet, that packet is dropped.  No worse than relay reboot.

**W4 — Per-packet PNI generation consumes RNG bytes.**  2–3 random bytes
per flood forward.  Negligible on ESP32 (hardware RNG) or in the simulator
(software PRNG).

**W5 — Does not protect against RF-level adversary.**  An adversary with
directional antennas or co-located receivers can map PNIs to physical
transmitters regardless of path obfuscation.

**W6 — Max path length halved with 2-byte hashes.**  `MAX_PATH_SIZE = 64`
→ 32 hops max (vs 63 with 1-byte).  MeshCore paths rarely exceed 10–15
hops, so this is not a constraint.

**W7 — Path hop count is still visible.**  `path_len & 63` reveals how
many relays are on the path.  In a known topology, this narrows the set of
possible relay chains.

---

## 8. Experiment plan

### 8.1 New scenarios

| Scenario name | Grid | Agents | Purpose |
|---------------|------|--------|---------|
| `grid/3x3/path2` | 3×3 | baseline, path2 | Verify 2-byte paths deliver identically to 1-byte |
| `grid/3x3/pni` | 3×3 | baseline, path2, privaterouting1 | Verify PNI delivers identically; verify path privacy |
| `grid/3x3/mixed` | 3×3 | mixed: baseline + path2 | Verify mixed 1-byte/2-byte interop |
| `grid/3x3/mixed-pni` | 3×3 | mixed: baseline + PNI | Verify mixed stock/PNI interop |
| `grid/10x10/path2` | 10×10 | baseline, path2 | Stress-test 2-byte paths at scale |
| `grid/10x10/pni` | 10×10 | baseline, path2, privaterouting1 | Stress-test PNI at scale; verify path privacy |
| `grid/10x10/mixed` | 10×10 | mixed: baseline + path2 | Mixed interop at scale |
| `grid/10x10/mixed-pni` | 10×10 | mixed: baseline + PNI | Mixed PNI interop at scale |

For mixed-network scenarios, the experiment runner needs a new mode where
different nodes run different binaries.  This requires extending `Scenario`
and `runner.py` to accept a per-node binary mapping (e.g. a dict from node
name pattern to binary path, or a callback).

### 8.2 Metrics to collect

| Metric | How measured | Target |
|--------|-------------|--------|
| Delivery rate | `MetricsCollector.delivery_pct()` | ≥ baseline for all scenarios |
| Witness count | `MetricsCollector.avg_witnesses()` | Same as baseline |
| Path privacy | New assertion: for PNI agents, no flood path entry matches any node's `pub_key[0:sz]` | 100% of PNI relay entries are non-matching |
| Patch LoC (MeshCore) | Lines changed in Mesh.h + Mesh.cpp | Report exact count |
| Patch LoC (agent) | `diff` against node_agent, excluding CMakeLists | Report exact count |
| Memory (PNI table) | Entry count at end of run via `{"type":"pni_table_size","entries":N,"bytes":B}` | Report per relay |
| Latency | `MetricsCollector` (wall-clock) | No regression vs baseline |

### 8.3 Path privacy assertion

After each experiment run, the trace analyser verifies PNI effectiveness:

```python
for packet in trace.flood_packets():
    for i, hop_hash in enumerate(packet.path_hashes()):
        for node in topology.nodes():
            assert hop_hash != node.pub_key[:hash_size], \
                f"path entry {i} matches {node.name} — PNI not applied"
```

This assertion should **pass** for privaterouting1 runs and **fail** for
baseline/path2 runs (confirming the test is meaningful).

---

## 9. Future directions

**F1 — Encrypted adverts / pseudonymous discovery.**  Replace the cleartext
32-byte public key in adverts with an ephemeral pseudonym derivable only by
nodes sharing a pre-existing secret.  Addresses W1.

**F2 — `PAYLOAD_VER_2` with encrypted src/dest hashes.**  Move `src_hash`
and `dest_hash` inside the encrypted payload.  Addresses W2.

**F3 — PNI table rotation.**  Periodically clear the PNI table and
regenerate all entries, bounding the window of PNI-based correlation.

**F4 — Onion-style layered PNI.**  Each relay encrypts the remainder of
the path under its own key before appending its PNI, so relay R_i can only
see the next hop.  Significantly more complex; increases path size.

---

## 10. Implementation checklist

### Phase 1: MeshCore hooks (prerequisite for both agents)

- [ ] Add `writeSelfPathHash` virtual method to `Mesh.h`
- [ ] Add `isSelfPathHash` virtual method to `Mesh.h`
- [ ] Change `routeRecvPacket` call site to use `writeSelfPathHash`
- [ ] Change `onRecvPacket` direct-routing check to use `isSelfPathHash`
- [ ] Verify all existing tests pass (no behavioural change)

### Phase 2: `privatemesh/path2` agent

- [ ] Create `privatemesh/path2/` with CMakeLists.txt, SimNode.h, SimNode.cpp
- [ ] Override flood origination to use `path_hash_size = 2`
- [ ] Build and verify `grid/3x3` and `grid/10x10` — identical delivery
- [ ] Add `path2` to `experiments/scenarios.py` binary list

### Phase 3: `privatemesh/privaterouting1` agent

- [ ] Create `privatemesh/privaterouting1/` with CMakeLists.txt
- [ ] Implement PNI table (fixed-size ring buffer, FIFO eviction, 128 cap)
- [ ] Override `writeSelfPathHash` — generate and store fresh PNI
- [ ] Override `isSelfPathHash` — check real hash, then PNI table
- [ ] Emit `pni_table_size` JSON event for metrics
- [ ] Build and verify `grid/3x3/pni` and `grid/10x10/pni`
- [ ] Add to `experiments/scenarios.py`

### Phase 4: Mixed-network experiments

- [ ] Extend `Scenario` / `runner.py` for per-node binary mapping
- [ ] Run `grid/3x3/mixed` and `grid/10x10/mixed` (baseline + path2)
- [ ] Run `grid/3x3/mixed-pni` and `grid/10x10/mixed-pni` (baseline + PNI)

### Phase 5: LoC and memory measurement

- [ ] Measure MeshCore patch LoC (Mesh.h + Mesh.cpp)
- [ ] Measure agent patch LoC (diff against node_agent)
- [ ] Collect `pni_table_size` from experiment traces
- [ ] Report all metrics in experiment output table

---

## 11. Estimated LoC summary

| Component | Files | New logic LoC | Boilerplate LoC |
|-----------|-------|---------------|-----------------|
| MeshCore hooks | Mesh.h, Mesh.cpp | 8 | 0 |
| path2 agent | SimNode.h, SimNode.cpp, CMakeLists.txt | ~25 | ~80 |
| privaterouting1 agent | SimNode.h, SimNode.cpp, CMakeLists.txt | ~70 | ~80 |
| Experiment scenarios | scenarios.py, runner.py | ~60 | 0 |
| Path privacy assertion | test file | ~20 | 0 |
| **Total** | | **~183** | **~160** |
