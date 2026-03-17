# meshcore_sim

A discrete-event simulator for [MeshCore](https://github.com/meshcore-dev/MeshCore)
mesh networks.  Each simulated node runs the **real MeshCore routing and
cryptography code** as a standalone subprocess; a Python orchestrator wires
them together over simulated radio links with configurable loss, latency, SNR,
RSSI, and adversarial behaviour.

```
alice ──(loss 5%, 20 ms)── relay1 ──(loss 5%, 20 ms)── bob
```
```
$ python3 -m orchestrator topologies/linear_three.json --duration 30 --seed 42
…
  Message delivery: 4/4 (100.0%)
  Latency (send→recv): min=21ms  avg=21ms  max=22ms
```

---

## Table of contents

1. [Repository layout](#repository-layout)
2. [Prerequisites](#prerequisites)
3. [Quick start](#quick-start)
4. [Building the node agent](#building-the-node-agent)
5. [Running the tests](#running-the-tests)
6. [Orchestrator reference](#orchestrator-reference)
7. [Topology file format](#topology-file-format)
8. [Architecture](#architecture)

---

## Repository layout

```
meshcore_sim/
├── MeshCore/               Git submodule — upstream MeshCore C++ source
│
├── node_agent/             Standalone C++ process wrapping one MeshCore node
│   ├── README.md           Build instructions and wire-protocol reference
│   ├── CMakeLists.txt
│   ├── main.cpp            select()-based stdin/stdout main loop
│   ├── SimRadio.h/.cpp     Radio shim: rx queue in, tx JSON out
│   ├── SimClock.h/.cpp     MillisecondClock + RTCClock (wall clock)
│   ├── SimRNG.h/.cpp       RNG from /dev/urandom
│   ├── SimNode.h/.cpp      Mesh subclass: routing policy, event callbacks
│   ├── arduino_shim/       Minimal Arduino Stream stub
│   └── crypto_shim/        SHA-256, AES-128, Ed25519 via OpenSSL 3.x EVP
│
├── orchestrator/           Python package — simulation engine
│   ├── __main__.py         Entry point (python3 -m orchestrator)
│   ├── config.py           Topology JSON loader and dataclasses
│   ├── topology.py         Adjacency graph (directed EdgeLinks)
│   ├── node.py             NodeAgent: asyncio subprocess wrapper
│   ├── router.py           PacketRouter: TX callbacks, loss, latency, adversarial
│   ├── adversarial.py      AdversarialFilter: drop / corrupt / replay modes
│   ├── traffic.py          TrafficGenerator: advert floods, random text sends
│   ├── metrics.py          Counters, delivery rate, latency, report
│   ├── packet.py           Wire-format decoder (pure Python, no binary needed)
│   ├── tracer.py           PacketTracer: per-packet path and witness analysis
│   └── cli.py              argparse CLI definition
│
├── tests/                  C++ test suite (crypto shims + packet serialisation)
│   ├── CMakeLists.txt
│   ├── main.cpp
│   ├── test_crypto.cpp     SHA-256, HMAC, AES-128, Ed25519, ECDH, encrypt/MAC
│   └── test_packets.cpp    Packet serialisation, path encoding, SimpleMeshTables
│
├── sim_tests/              Python test suite (orchestrator + integration)
│   ├── __main__.py         Entry point (python3 -m sim_tests)
│   ├── helpers.py          Shared factories and skip decorators
│   ├── test_config.py           Config loading, DirectionalOverrides
│   ├── test_topology.py         Adjacency graph, asymmetric edges
│   ├── test_adversarial.py      Drop / corrupt / replay filter logic
│   ├── test_metrics.py          Counters, delivery tracking, report formatting
│   ├── test_node_agent.py       NodeAgent lifecycle and commands
│   ├── test_packet_decode.py    Wire-format decoder (30 tests, no binary needed)
│   ├── test_tracer.py           PacketTracer path and witness tracking (26 tests)
│   ├── test_integration_smoke.py  End-to-end simulation smoke tests
│   └── test_cpp_suite.py        Runs the C++ binary as part of the Python suite
│
└── topologies/             Example topology JSON files
    ├── linear_three.json
    ├── star_five.json
    ├── adversarial.json
    └── asymmetric_hill.json
```

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| C++17 compiler | AppleClang 17+ or GCC 12+ | For `node_agent` and C++ tests |
| CMake | ≥ 3.16 | `brew install cmake` |
| OpenSSL | 3.x | `brew install openssl@3`; usually pre-installed on Linux |
| Python | 3.9+ | For the orchestrator and Python tests |

No external Python packages are required — the orchestrator uses only the
standard library (`asyncio`, `json`, `subprocess`, `argparse`, `unittest`).

The MeshCore submodule must be initialised once after cloning:

```sh
git submodule update --init
```

---

## Quick start

```sh
# 1. Get the submodule
git submodule update --init

# 2. Build the node agent
cd node_agent
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
cd ..

# 3. Run a 30-second simulation
python3 -m orchestrator topologies/linear_three.json --duration 30 --seed 42

# 4. Run all tests
python3 -m sim_tests
```

---

## Building the node agent

```sh
cd node_agent
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The binary is written to `node_agent/build/node_agent`.

For a debug build (assertions enabled, optimisations disabled):

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build
```

See [`node_agent/README.md`](node_agent/README.md) for the full wire-protocol
reference (stdin commands and stdout events) if you want to drive a node agent
manually.

---

## Running the tests

### Everything at once

```sh
python3 -m sim_tests
```

This runs all 251 tests:

| Group | Count | Binary needed |
|-------|------:|---------------|
| C++ crypto (SHA-256, HMAC, AES-128, Ed25519, ECDH, encrypt) | 9 groups† | `tests/build/meshcore_tests` |
| Python unit — config, topology, adversarial, metrics | 118 | none |
| Python unit — packet decoder, path tracer | 56 | none |
| Python integration — NodeAgent, simulation smoke tests | 68 | `node_agent/build/node_agent` |

† Each group wrapper drives the C++ binary with a name filter; the 9 wrappers
cover 45 internal C++ test cases and 107 checks.

Integration tests are **skipped** (not failed) when the relevant binary is
absent.  A freshly cloned repo with neither binary built will run the 118 pure
Python unit tests and skip everything else.

### Subsets

```sh
python3 -m sim_tests sim_tests.test_config          # one Python module
python3 -m sim_tests sim_tests.test_adversarial
python3 -m sim_tests sim_tests.test_cpp_suite       # C++ tests only
```

### Building and running the C++ tests separately

```sh
cd tests
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build
./build/meshcore_tests              # all 45 tests
./build/meshcore_tests sha256       # SHA-256 group only
./build/meshcore_tests packet       # Packet serialisation only
cd build && ctest --output-on-failure
```

Expected output: **45 passed, 0 failed (107 checks)**.

---

## Orchestrator reference

### Basic usage

```sh
python3 -m orchestrator <topology.json> [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `topology` | — | Path to topology JSON file (required) |
| `--duration SECS` | from JSON | Override simulation duration |
| `--warmup SECS` | from JSON | Override warmup period before traffic starts |
| `--traffic-interval SECS` | from JSON | Override mean seconds between random text sends |
| `--advert-interval SECS` | from JSON | Override advertisement re-flood interval |
| `--agent PATH` | from JSON | Override path to `node_agent` binary |
| `--seed N` | from JSON | RNG seed for reproducible loss/traffic decisions |
| `--log-level` | `info` | `debug` / `info` / `warning` / `error` |
| `--report FILE` | — | Write final metrics report to a file (always printed to stdout) |

### Examples

```sh
# Quick 20-second run, fixed seed for reproducibility
python3 -m orchestrator topologies/linear_three.json --duration 20 --seed 42

# High-traffic star topology, verbose logging
python3 -m orchestrator topologies/star_five.json \
    --duration 120 --traffic-interval 3 --log-level debug

# Adversarial relay, save report
python3 -m orchestrator topologies/adversarial.json \
    --duration 60 --seed 7 --report results/adversarial_run.txt

# Asymmetric RF links
python3 -m orchestrator topologies/asymmetric_hill.json --duration 120
```

### Metrics report

At the end of every run the orchestrator prints a report to stdout:

```
==================================================
  Simulation Metrics
==================================================

  Node                      TX      RX
  --------------------  ------  ------
  alice                      2       2
  bob                        6       2
  relay1                     2       8

  Message delivery: 6/6 (100.0%)
  Latency (send→recv): min=21ms  avg=21ms  max=22ms

  Link-level packet loss:  0
  Adversarial drops:       0
  Adversarial corruptions: 0
  Adversarial replays:     0

  Delivered messages:
    [    21 ms]  bob → relay1: 'hello from bob t=64864'
    …
==================================================
```

TX and RX counts are orchestrator-level packet counts (not the node's internal
counters).  Latency is wall-clock time from `send_text` command to the
matching `recv_text` event, and includes routing delay and any configured
link latency.

The report also includes a **Packet Path Trace** section produced by the
`PacketTracer` (see [Packet path tracing](#packet-path-tracing)):

```
============================================================
  Packet Path Trace
============================================================

  Unique packets:   14
  Total deliveries: 38
  Flood-routed:     12
  Direct-routed:    2

  Type              Count  Avg witnesses  Max witnesses
  ----------------  -----  -------------  -------------
  ADVERT                6            4.2              6
  TXT_MSG               4            5.5              8
  PATH                  2            1.0              1
  ACK                   2            2.0              2

  Highest-exposure packets (witnesses = nodes that received a copy):

    [TXT_MSG     ] 02deadbeef112233…  witnesses=8  route=FLOOD
      senders:   alice, relay1, relay2
      receivers: relay1, relay2, relay3, bob
    …
============================================================
```

The **witnesses** count for a packet is the number of (sender→receiver) radio
transmissions involving that packet that the orchestrator observed.  This is
the key privacy metric: a packet with many witnesses was seen by many nodes,
making it easier for a network-level adversary to correlate it across the mesh.

---

## Topology file format

A topology file is a JSON object with three top-level keys.

### `nodes`

An array of node objects.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | **required** | Unique identifier used in edges, logging, and metrics |
| `relay` | bool | `false` | Relay nodes forward flood packets to all radio neighbours; endpoints do not |
| `prv_key` | string | — | Fixed 128-hex-char (64-byte) Ed25519 private key. Omit for a fresh random identity on each run |
| `adversarial` | object | — | Omit for an honest node; see [Adversarial nodes](#adversarial-nodes) |

**Relay vs. endpoint.** In MeshCore, a relay node re-broadcasts every flood
packet it has not seen before, extending the effective range of the mesh.
An endpoint processes packets addressed to it but does not forward them.
Every network needs at least one relay for nodes that are not directly
adjacent to each other to communicate.

**Fixed private keys.** If `prv_key` is omitted, the node generates a random
Ed25519 keypair on startup (from `/dev/urandom`), so its public key changes
on every run.  Provide a fixed key when you need deterministic public keys
across runs — for example, to pre-seed a contact list in a test harness.
The key format is `seed[32 bytes] || public_key[32 bytes]` concatenated and
hex-encoded (128 characters), which is the convention used by the
[orlp/ed25519](https://github.com/nicowillis/ed25519) library vendored inside
MeshCore.  Generate a fresh key with:

```sh
python3 -c "import os; print(os.urandom(64).hex())"
```

### `edges`

An array of edge objects.  Each edge is **nominally undirected** — the
symmetric fields apply to both directions — but can carry
**per-direction overrides** for asymmetric RF links.

#### Symmetric fields (apply to both directions unless overridden)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `a` | string | **required** | Name of one endpoint |
| `b` | string | **required** | Name of the other endpoint |
| `loss` | float | `0.0` | Packet loss probability [0.0–1.0] applied independently to each packet |
| `latency_ms` | float | `0.0` | One-way propagation delay in milliseconds |
| `snr` | float | `6.0` | Signal-to-noise ratio delivered to the receiver (dB) |
| `rssi` | float | `-90.0` | Received signal strength delivered to the receiver (dBm) |

#### Directional overrides

Add an `a_to_b` and/or `b_to_a` object to override any subset of the four
parameters for a specific direction.  `a_to_b` means "parameters as experienced
by `b` when `a` transmits"; `b_to_a` is the reverse.  Unspecified fields within
a directional object inherit the symmetric base value.

```json
{
  "a": "base_camp", "b": "hill_relay",
  "loss": 0.02, "latency_ms": 15, "snr": 8.0, "rssi": -82.0,
  "a_to_b": { "snr": 14.0, "rssi": -68.0 },
  "b_to_a": { "snr":  5.0, "rssi": -93.0, "loss": 0.15 }
}
```

Here `latency_ms` is not overridden in either direction, so both directions
use 15 ms.

**One-way (receive-only) link.** Set `loss: 1.0` in one direction:

```json
{
  "a": "deep_valley", "b": "hill_relay",
  "loss": 0.05, "snr": 7.0, "rssi": -88.0,
  "b_to_a": { "loss": 1.0 }
}
```

`deep_valley` can transmit to `hill_relay` (5 % loss); `hill_relay` cannot
reach `deep_valley` at all.

### `simulation`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `warmup_secs` | float | `5.0` | Seconds before traffic starts; allows advertisements to propagate |
| `duration_secs` | float | `60.0` | Total simulation wall-clock time |
| `traffic_interval_secs` | float | `10.0` | Mean seconds between random text sends (Poisson arrival) |
| `advert_interval_secs` | float | `30.0` | How often to re-flood advertisements from all nodes |
| `epoch` | int | `0` | Unix epoch sent to nodes on startup; `0` means use the real wall clock |
| `agent_binary` | string | `./node_agent/build/node_agent` | Path to the compiled `node_agent` binary |
| `seed` | int | — | RNG seed; omit for non-deterministic behaviour |

### Adversarial nodes

Any node can be given an `adversarial` configuration.  The adversarial filter
is applied to packets the node **receives** before it processes or forwards
them — modelling a compromised relay.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | **required** | `"drop"`, `"corrupt"`, or `"replay"` |
| `probability` | float | `1.0` | Fraction of received packets that trigger the behaviour |
| `corrupt_byte_count` | int | `1` | `corrupt` only: number of bytes to bit-flip |
| `replay_delay_ms` | float | `5000.0` | `replay` only: delay before re-emitting the captured packet |

**`drop`** — The packet is silently discarded.  With `probability: 1.0` and
`loss: 0.0` edges, message delivery should fall to near zero.

**`corrupt`** — `corrupt_byte_count` randomly-chosen bytes have a random bit
flipped before the packet is delivered.  The MeshCore MAC layer will reject
most corrupted packets, so the effect is similar to loss from the application's
perspective.

**`replay`** — The original packet is suppressed and a copy is queued for
re-delivery after `replay_delay_ms` milliseconds.  The re-emitted copy is
broadcast to all of that node's neighbours, potentially causing duplicate
processing or flooding.

```json
"adversarial": {
  "mode": "corrupt",
  "probability": 0.5,
  "corrupt_byte_count": 2
}
```

### Complete minimal example

```json
{
  "nodes": [
    { "name": "alice",  "relay": false },
    { "name": "relay1", "relay": true  },
    { "name": "bob",    "relay": false }
  ],
  "edges": [
    { "a": "alice",  "b": "relay1", "loss": 0.05, "latency_ms": 20 },
    { "a": "relay1", "b": "bob",    "loss": 0.05, "latency_ms": 20 }
  ],
  "simulation": {
    "warmup_secs": 5,
    "duration_secs": 60,
    "agent_binary": "./node_agent/build/node_agent"
  }
}
```

---

## Architecture

### Overview

```
┌─────────────────────────────────────────────────────┐
│                Python Orchestrator                  │
│                                                     │
│  ┌──────────┐   TX callback   ┌──────────────────┐  │
│  │NodeAgent │────────────────▶│  PacketRouter    │  │
│  │(alice)   │◀────deliver_rx──│  · loss check    │  │
│  └──────────┘                 │  · adv filter    │  │
│       │ stdin/stdout pipe      │  · asyncio.sleep │  │
│  ┌────▼─────┐                 │    (latency)     │  │
│  │node_agent│                 └──────────────────┘  │
│  │ process  │  ← real MeshCore C++ routing/crypto   │
│  └──────────┘                                       │
│                  ┌──────────────────────────────┐   │
│                  │  TrafficGenerator            │   │
│                  │  · initial advert flood      │   │
│                  │  · periodic re-floods        │   │
│                  │  · Poisson text sends        │   │
│                  └──────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### Node agents

Each simulated node is a separate OS process running the `node_agent` binary.
The binary links directly against the MeshCore C++ source (compiled from the
`MeshCore/` submodule) plus thin shims:

- **SimRadio** — implements `mesh::Radio`; `recvRaw()` pops from an in-process
  queue fed by the orchestrator; `startSendRaw()` writes a JSON `tx` event to
  stdout.
- **SimClock** — wall-clock backed `MillisecondClock` and `RTCClock`.
- **SimRNG** — reads from `/dev/urandom`.
- **crypto shims** — drop-in replacements for the Arduino Crypto library
  classes (`SHA256`, `AES128`, `Ed25519`) backed by OpenSSL 3.x EVP.

No changes to MeshCore source are required.  The submodule is compiled as-is.

### Wire protocol

Communication with each node agent uses **newline-delimited JSON** on
`stdin` / `stdout`.  See [`node_agent/README.md`](node_agent/README.md) for
the full message reference.

In brief:

- **Orchestrator → node**: `rx` (deliver a raw packet), `time` (set RTC),
  `send_text`, `advert`, `quit`.
- **Node → orchestrator**: `ready` (pub key, relay flag), `tx` (raw packet
  to broadcast), `recv_text`, `advert` (new peer seen), `ack`, `log`.

### Packet routing

When a node emits a `tx` event the `PacketRouter` creates one
`asyncio.create_task` per radio neighbour.  Each task:

1. Rolls a random number against the link's `loss` probability and drops the
   packet if it loses.
2. If the **receiving** node is adversarial, applies the adversarial filter
   (drop / corrupt / replay).
3. Sleeps for `latency_ms / 1000` seconds (`asyncio.sleep` — non-blocking).
4. Calls `deliver_rx` on the receiving `NodeAgent`, which writes an `rx`
   command to the node's stdin.

Because each delivery is an independent asyncio task, many packets can be
in-flight simultaneously, correctly modelling concurrent transmissions.

### Advertisement exchange and contact discovery

MeshCore nodes can only send encrypted text to **contacts** — peers whose
Ed25519 public key they know.  Contacts are discovered through Advertisement
flood packets:

1. Node A calls `broadcast_advert()` → emits a `tx` Advertisement packet.
2. The orchestrator delivers it to all of A's neighbours.
3. Relay neighbours re-broadcast it (flood routing), extending range.
4. When node B receives it, MeshCore stores A's identity, computes the ECDH
   shared secret (`X25519(b_prv, a_pub)`), and emits an `advert` event.
5. The orchestrator adds A's public key to `b.state.known_peers`.
6. B can now `send_text` to A using A's public key prefix as `dest`.

The `warmup_secs` parameter exists to allow this advertisement exchange to
complete before the traffic generator starts sending messages.

### Asymmetric links

The `Topology` class maintains a directed adjacency list.  Each `EdgeLink`
carries the parameters for one direction of travel.  When an edge has
`a_to_b` or `b_to_a` overrides, the corresponding `EdgeLink` is built with
the merged values (`override if override is not None else symmetric_base`).
The router always looks up the link from the sender's adjacency list, so it
automatically uses direction-appropriate loss, latency, SNR, and RSSI.

### Packet path tracing

`orchestrator/packet.py` provides a pure-Python wire-format decoder that
parses the hex bytes from every `tx` event into structured fields:

| Field | Description |
|-------|-------------|
| `route_type` | `FLOOD`, `DIRECT`, `TRANSPORT_FLOOD`, `TRANSPORT_DIRECT` |
| `payload_type` | `TXT_MSG`, `ADVERT`, `ACK`, `PATH`, `TRACE`, … |
| `path_count` | Number of relay-hash entries in `path[]` at this hop |
| `path_bytes` | Raw path bytes (grow on flood, shrink on direct) |
| `payload` | Encrypted payload — **hop-invariant**, same bytes at every relay |

The **fingerprint** of a packet — `hex(payload_type_byte || payload_bytes)` —
is stable across all hops because only the path field changes as relays
forward it.  This matches exactly how MeshCore's own dedup table
(`calculatePacketHash`) identifies packets.

`orchestrator/tracer.py` — `PacketTracer` — uses this fingerprint to
correlate every copy of the same logical packet that the orchestrator
observes as it propagates through the network:

- `record_tx(sender, hex_data, t)` — called from `PacketRouter._on_tx`
- `record_rx(sender, receiver, hex_data, t)` — called from `PacketRouter._deliver_to` after all filters pass

At simulation end, `PacketTracer.report()` emits the path trace section of
the report.  The raw trace data is available programmatically via
`tracer.traces` (dict of fingerprint → `PacketTrace`) for downstream
analysis scripts.

**Privacy-research use cases:**
- **Flood multiplicity** — `trace.unique_senders` shows how many relays
  forwarded a message, revealing the shape of the broadcast tree.
- **Witness count** — `trace.witness_count` is the total number of nodes that
  received at least one copy.  Lower is more private.
- **Route mode** — `trace.is_flood()` distinguishes flood from direct routing.
  A direct-routed message reaches only the next-hop relay, whereas a
  flood-routed message may reach the entire network.
- **Cross-hop correlation** — because every copy of the same packet has an
  identical fingerprint, any two nodes that observed the same fingerprint can
  confirm they saw the same message.  Eliminating this correlation (e.g., by
  per-hop re-encryption) is the core challenge of privacy-preserving routing.

### Adversarial model

Adversarial behaviour is modelled at the **receiver**, not the transmitter.
This represents a compromised relay that manipulates packets it receives
before forwarding them.  The `probability` field is checked independently
for each packet, so a 50 %-probability corrupt node corrupts roughly half
of the traffic passing through it while forwarding the rest normally.
