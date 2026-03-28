# BUG: Listen-Before-Talk Race Condition

**Status:** Fixed
**Severity:** High — breaks RF contention fidelity
**Discovered:** 2026-03-28, while investigating why companions transmit over ongoing channel activity

## Summary

Nodes transmit even when a nearby node is already on the air. The orchestrator's
`rx_start` (Listen-Before-Talk) notification can arrive at the node_agent **after**
a `send_text` command has already been processed, so MeshCore's `Dispatcher::checkSend()`
sees a clear channel and fires.

Two separate bugs were found:

1. **Asyncio race** (orchestrator): `rx_start` notification goes through
   `asyncio.sleep(preamble_wait)`, allowing other commands to arrive first.
2. **Overwrite bug** (node_agent): `notifyRxStart()` uses assignment instead of
   `max()`, so a shorter subsequent notification can end the busy window early.

## Observed behaviour

**Packet #396** (TXT_MSG from bob) in the `gdansk` topology:

| Event | Sim-time | What happens |
|-------|----------|--------------|
| `GDA_Karczemki` starts TX (tx#4358) | 6093191.478 | 1099ms ADVERT broadcast |
| Preamble should arrive at bob | 6093191.498 | +20ms propagation delay |
| **bob starts TX** (tx#4365) | 6093191.889 | **411ms after Karczemki started** |
| bob's TX ends | 6093192.300 | 411ms TXT_MSG |
| Karczemki's TX ends | 6093192.577 | |

Bob has an edge to `GDA_Karczemki` (SNR -3.31 dB, latency 20ms). On real hardware,
bob's radio would detect the preamble at `t+20ms` and `Dispatcher::checkSend()` would
defer via `isReceiving() == true`. In the simulator, bob transmits anyway.

## Root cause

The orchestrator uses three independent asyncio mechanisms that are not synchronised:

1. **Traffic injection** (`traffic.py`) writes `{"type":"send_text",...}` directly to
   the node's stdin pipe — no simulated delay.

2. **LBT notification** (`router.py`) sent `{"type":"rx_start",...}` to the same node,
   but only **after** `await asyncio.sleep(preamble_wait)` to simulate propagation delay.

3. **Node agent** (`main.cpp:254-287`) polls stdin with a 1ms `select()` loop and
   processes commands in arrival order, then calls `node.loop()`.

### The race

```
Sim-time 6093191.478:
  GDA_Karczemki transmits (tx#4358).
  Orchestrator creates delivery tasks for all Karczemki neighbours.
  Delivery task for bob: preamble_arrival = 6093191.478 + 0.020 = 6093191.498
    -> await asyncio.sleep(preamble_wait)   [TASK SUSPENDED]

Sim-time ~6093191.5-6093191.8 (asyncio scheduler runs other tasks):
  Traffic generator fires for bob.
    -> await bob.send_text(dest, text)
    -> writes {"type":"send_text",...} to bob's stdin   [IMMEDIATE]

Bob's main loop (wall-clock):
  select() returns, reads "send_text" from stdin.
  dispatch() -> node.sendTextTo() -> MeshCore queues outbound packet.
  node.loop() -> Dispatcher::checkSend()
    -> isReceiving()? -> _rx_active_until not set yet -> returns false
    -> Dispatcher proceeds to startSendRaw()
    -> bob transmits (tx#4365) at sim-time 6093191.889

Later (asyncio sleep completes):
  Delivery task resumes, calls bob.notify_rx_start(1099ms).
  -> writes {"type":"rx_start","duration_ms":1099} to bob's stdin.
  -> TOO LATE: bob already transmitted.
```

### Why `preamble_wait` doesn't help

The sleep is wall-clock-based. The orchestrator runs **much faster than real time**:
300s of sim-time completes in ~30s of wall-clock. The `preamble_wait` of 20ms
sim-time translates to near-zero wall-clock time. But the asyncio scheduler can
interleave other ready tasks (like traffic injection) during even a 0ms sleep,
because `await asyncio.sleep(0)` still yields to the event loop.

## All manifestations of the race

The root problem is not limited to `send_text`. Every code path where the node
can transmit is affected.

### 1. `send_text` / `send_channel` vs `rx_start` (the documented case)

Traffic injection writes immediately to stdin. `rx_start` sleeps in asyncio.
Affects companion nodes originating user traffic (~10-30 per run).

### 2. `advert` injection vs `rx_start`

`broadcast_advert()` writes `{"type":"advert"}` immediately after a stagger
sleep. If a neighbour started transmitting during the stagger, the `rx_start`
is still in the pipeline. Affects both initial floods and periodic re-adverts.

### 3. Autonomous relay retransmissions vs `rx_start` (MOST IMPACTFUL)

When a relay receives a flood packet, `Dispatcher::checkRecv()` queues it for
retransmission via `queueOutbound()` with a score-based delay (as low as 50ms).
On the next `checkSend()` call, if `isReceiving()` is false, the relay transmits.

**No orchestrator command is involved** — the relay autonomously decides to
retransmit. The only prevention is `isReceiving()` being true, which requires
`rx_start` to have arrived on time.

This is the most impactful variant because **flood retransmissions are the
majority of all transmissions**. In the gdansk topology (~35 nodes), a single
packet triggers 10-20 relay retransmissions, each of which can race against
neighbours' ongoing TXes.

### 4. `notifyRxStart()` overwrite bug (separate, in SimRadio.cpp)

```cpp
void SimRadio::notifyRxStart(uint32_t duration_ms) {
    _rx_active_until = _ms.getMillis() + duration_ms;  // ASSIGNMENT, not max
}
```

If two neighbours transmit overlapping packets with different airtimes:
- A: 1099ms, `rx_start(1099)` at t=0  ->  `_rx_active_until = 1099`
- B: 411ms,  `rx_start(411)`  at t=500 ->  `_rx_active_until = 911`

Channel appears clear at t=911, but A doesn't finish until t=1099. **188ms
window** where the node thinks it's safe to transmit but isn't.

### Impact by variant

| Variant | Frequency | Severity |
|---------|-----------|----------|
| #1 send_text/send_channel | ~10-30 per run | High |
| #2 advert injection | Every re-advert round | Medium |
| **#3 relay retransmissions** | **Hundreds per run** | **Critical** |
| #4 rx_start overwrite | Every overlapping TX pair | High |

## Affected code paths

| File | Lines | Role |
|------|-------|------|
| `orchestrator/router.py` | `_on_tx()` | Created delivery tasks without pre-notifying LBT |
| `orchestrator/router.py` | `_deliver_to()` | `rx_start` notification after asyncio.sleep |
| `orchestrator/traffic.py` | `_send_random()` | Injects traffic immediately |
| `orchestrator/node.py` | `notify_rx_start()` | Writes to stdin pipe |
| `node_agent/main.cpp` | Main loop | select() + dispatch + node.loop() |
| `node_agent/SimRadio.cpp` | `notifyRxStart()` | Used assignment instead of max |
| `MeshCore/src/Dispatcher.cpp` | `checkSend()` | Checks `isReceiving()` for LBT |

## Constraints on the fix

1. **No changes to MeshCore source** (design invariant). The fix must be in the
   orchestrator or node_agent shim.
2. **The node_agent uses wall-clock time** (`SimClock::getMillis()` = `std::chrono`).
   This is correct for the Dispatcher's duty-cycle enforcement but means LBT timing
   depends on command arrival order, not simulated time.
3. **asyncio task ordering is not deterministic** at equal simulated times (documented
   in DETERMINISM.md). Any fix must either eliminate the race structurally or make the
   ordering explicit.

## Fix applied

### Fix 1: Synchronous LBT in `_on_tx()` (Approach B)

**File:** `orchestrator/router.py`

Moved `rx_start` notification from `_deliver_to()` (where it went through
`asyncio.sleep(preamble_wait)`) to `_on_tx()`, where it runs **synchronously
before** delivery tasks are created.

```python
# In _on_tx(), after registering with channel model, before creating
# delivery tasks:
if airtime_ms > 0:
    snr_min = SNR_THRESHOLD.get(self._radio.sf, -20.0)
    lbt_coros = []
    for link in self._topology.neighbours(sender_name):
        if snr_min is not None and link.snr < snr_min:
            continue  # below SF threshold -- real CAD wouldn't detect
        receiver = self._agents.get(link.other)
        if receiver is not None:
            lbt_coros.append(receiver.notify_rx_start(airtime_ms))
    if lbt_coros:
        await asyncio.gather(*lbt_coros)
```

**Why this works:** The `rx_start` is written to every neighbour's stdin pipe
before `_on_tx()` returns. No `asyncio.sleep()` is involved, so no other task
can interleave. When the subprocess next calls `select()`, it reads `rx_start`
before any subsequent commands. The inner read loop in `main.cpp:266-284`
drains all available bytes before calling `node.loop()`, so `isReceiving()`
is true when `checkSend()` runs.

**Trade-off:** Real preamble detection has 5-20ms propagation delay. This is
below the simulator's timing resolution (1ms select loop) and negligible
relative to packet airtime (400-1100ms).

### Fix 2: `notifyRxStart()` uses max instead of assignment

**File:** `node_agent/SimRadio.cpp`

```cpp
void SimRadio::notifyRxStart(uint32_t duration_ms) {
    unsigned long new_until = _ms.getMillis() + duration_ms;
    if (new_until > _rx_active_until) {
        _rx_active_until = new_until;
    }
}
```

Overlapping notifications now extend the busy window rather than shortening it.

## Design note: CAD timeout interaction

`Dispatcher::checkSend()` has a CAD timeout: if `isReceiving()` stays true for
>4s (`getCADFailMaxDuration()`), MeshCore force-transmits assuming the radio is
stuck. With the fix, dense networks with back-to-back transmissions could
legitimately keep the channel busy for >4s, triggering the force-transmit.
This is **correct real-hardware behaviour** — the same timeout exists on
physical devices for the same reason.

## Reproduction (pre-fix)

1. Generate gdansk topology: `python3 tools/import_topology.py tools/topology.json -o topologies/gdansk.json --companions 5 -v`
2. Run simulation: `python3 -m orchestrator topologies/gdansk.json --duration 300`
3. In the trace, find any companion `TXT_MSG` packet and check if the companion's
   TX overlaps with a neighbour's ongoing TX.
4. The waterfall panel in the workbench makes this visually obvious -- enable it,
   find a companion's TX bar, and check if neighbour bars overlap on the same
   receiver row.
