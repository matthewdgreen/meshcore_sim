# node_agent

A single simulated MeshCore node, compiled as a standalone process.

The node agent wraps the real MeshCore routing and cryptography code (pulled
directly from the `MeshCore/` submodule) behind a simple newline-delimited
JSON interface on **stdin / stdout**.  The Python simulator spawns one process
per node, delivers packets to it via stdin, and reads transmitted packets from
stdout.

---

## Prerequisites

| Tool | Notes |
|------|-------|
| C++17 compiler | AppleClang 17+ or GCC 12+ tested |
| CMake ≥ 3.16 | `brew install cmake` on macOS |
| OpenSSL 3.x | `brew install openssl@3` on macOS; usually pre-installed on Linux |

The MeshCore submodule must be checked out:
```sh
git submodule update --init
```

---

## Build

```sh
cd node_agent
mkdir -p build
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The binary is written to `node_agent/build/node_agent`.

For a debug build (enables assertions, disables optimisations):
```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build
```

---

## Usage

```
node_agent [--relay | --room-server] [--name <str>] [--prv <128-hex-char private key>]
           [--sf <int>] [--bw <int>] [--cr <int>]
```

| Flag | Description |
|------|-------------|
| `--relay` | Node forwards flood packets it hasn't seen before (repeater). Mutually exclusive with `--room-server`. |
| `--room-server` | Node acts as a room server: every received TXT_MSG is re-broadcast to all other known contacts. Emits a `room_post` event per forwarded message. Mutually exclusive with `--relay`. |
| `--name <str>` | Human-readable node name included in the `ready` message and embedded in Advertisement packets. Defaults to `node`. |
| `--prv <hex>` | 128-hex-character (64-byte) Ed25519 private key. The matching public key is derived automatically. If omitted, a deterministic identity is derived from the node name (seeded xoshiro256** PRNG). |
| `--sf <int>` | LoRa spreading factor (7–12). Used by `SimRadio::getEstAirtimeFor()` for accurate airtime estimation. Default: `8`. |
| `--bw <int>` | LoRa bandwidth in Hz. Default: `62500` (62.5 kHz). |
| `--cr <int>` | LoRa coding-rate offset (1=CR4/5 … 4=CR4/8). Default: `4` (CR4/8). |

On startup the node writes a `ready` line to stdout and then enters its main
loop, reading commands from stdin and writing events to stdout.

```sh
# Start a relay node with a generated identity
./build/node_agent --relay --name relay1

# Start an endpoint with a fixed identity
./build/node_agent --name endpoint1 --prv <128-hex-chars>
```

---

## Wire protocol

All messages are newline-delimited JSON (one object per line).  Packet
payloads are hex-encoded.

### stdin → node

| `type` | Additional fields | Description |
|--------|-------------------|-------------|
| `rx` | `hex`, `snr` (float dB), `rssi` (float dBm) | Deliver a raw over-the-air packet to this node's radio queue |
| `rx_start` | `duration_ms` (uint32) | Notify the radio that a preamble has been detected; marks `isReceiving()` true for `duration_ms`, enabling Listen-Before-Talk (Dispatcher defers TX while the channel is busy) |
| `time` | `epoch` (uint32 Unix seconds) | Set / correct the node's RTC clock |
| `send_text` | `dest` (pub-key hex prefix), `text` (UTF-8) | Send an encrypted text message to a known contact |
| `send_channel` | `text` (UTF-8) | Send a public group channel message (flood broadcast to all nodes sharing the channel PSK) |
| `advert` | `name` (UTF-8, optional) | Flood-broadcast an Advertisement from this node |
| `quit` | — | Shut down cleanly |

### node → stdout

| `type` | Additional fields | Description |
|--------|-------------------|-------------|
| `ready` | `pub` (hex), `is_relay` (bool), `role` (str), `name` (str) | Emitted once on startup; `role` is one of `"endpoint"`, `"relay"`, or `"room-server"` |
| `tx` | `hex` | Raw packet the node wants to transmit over the air |
| `recv_text` | `from` (hex pub key), `name` (str), `text` (UTF-8) | Decrypted text message received from a known contact |
| `recv_channel` | `channel` (str), `text` (UTF-8) | A group channel message was received; text is formatted as `"sender_name: message"` by MeshCore |
| `room_post` | `from` (hex pub key), `name` (str), `text` (UTF-8) | Room-server only: a TXT_MSG was received and has been forwarded to all other contacts |
| `recv_data` | `from` (hex), `payload_type` (int), `hex` | Generic decrypted data packet |
| `advert` | `pub` (hex), `name` (str) | A new peer's Advertisement was received |
| `ack` | `crc` (uint32) | An ACK packet was received |
| `log` | `msg` (str) | Informational message (RX/TX summaries, errors) |

### Minimal example

```sh
# Terminal 1 — start a relay
./build/node_agent --relay --name relay1
# {"type":"ready","pub":"43BD...","is_relay":true,"role":"relay","name":"relay1"}

# Terminal 2 — start a room server
./build/node_agent --room-server --name hub
# {"type":"ready","pub":"A1B2...","is_relay":false,"role":"room-server","name":"hub"}

# Terminal 3 — pipe commands to test advert emission
echo '{"type":"advert","name":"relay1"}' | ./build/node_agent --relay --name relay1
# {"type":"ready","pub":"...","is_relay":true,"role":"relay","name":"relay1"}
# {"type":"tx","hex":"1100..."}          <- real MeshCore Advertisement packet
# {"type":"log","msg":"TX len=111 type=4 route=F"}
```

---

## Source layout

```
node_agent/
├── CMakeLists.txt
├── main.cpp               # select()-based stdin/stdout main loop
├── SimRadio.h / .cpp      # Radio impl: recvRaw() from queue, startSendRaw() → stdout
├── SimClock.h / .cpp      # MillisecondClock + RTCClock backed by wall clock
├── SimRNG.h   / .cpp      # Deterministic PRNG (xoshiro256**, seeded from --prv or name)
├── SimNode.h  / .cpp      # BaseChatMesh subclass: ACK/retry, event callbacks
├── arduino_shim/
│   ├── Arduino.h          # Minimal Arduino.h shim (ltoa, stdint)
│   └── Stream.h           # Minimal Arduino Stream stub (~50 lines)
└── crypto_shim/
    ├── SHA256.h / .cpp    # SHA-256 and HMAC-SHA-256 via OpenSSL 3 EVP_MAC
    ├── AES.h    / .cpp    # AES-128-ECB via OpenSSL EVP
    └── Ed25519.h          # Ed25519 verify() wrapper around lib/ed25519
```

MeshCore source files compiled in (from `../MeshCore/src/`):
`Packet.cpp`, `Dispatcher.cpp`, `Mesh.cpp`, `Utils.cpp`, `Identity.cpp`,
`helpers/StaticPoolPacketManager.cpp`, `helpers/BaseChatMesh.cpp`,
`helpers/TxtDataHelpers.cpp`, `helpers/AdvertDataHelpers.cpp`,
and the portable `lib/ed25519/*.c` implementation.
**No changes to MeshCore source are required.**

---

## Running the tests

The test suite lives in `../tests/` and covers the crypto shims and packet
serialisation layer.  It uses the same build infrastructure as `node_agent`.

```sh
cd ../tests
mkdir -p build
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build
./build/meshcore_tests
```

Expected output: **45 passed, 0 failed (107 checks)**.

To run a subset of tests, pass a name filter:
```sh
./build/meshcore_tests sha256    # SHA-256 tests only
./build/meshcore_tests ecdh      # ECDH tests only
./build/meshcore_tests packet    # Packet serialisation tests only
./build/meshcore_tests tables    # SimpleMeshTables dedup tests only
```

Tests can also be driven via CTest:
```sh
cd tests/build && ctest --output-on-failure
```

### Test coverage

| Group | What is tested |
|-------|----------------|
| `sha256` | SHA-256 of empty string and `"abc"` against verified vectors; truncation; consistency |
| `hmac` | HMAC-SHA-256 RFC 4231 Test Cases 1 and 2 against verified vectors; key sensitivity |
| `aes128` | NIST SP 800-38A F.1.1 ECB encrypt/decrypt vector; roundtrip; key sensitivity |
| `ed25519` | Sign/verify roundtrip; wrong-key, tampered-message, tampered-signature rejection |
| `ecdh` | Shared-secret symmetry (A→B == B→A); non-trivial; different-peers-differ |
| `encrypt` | `Utils::encryptThenMAC` / `MACThenDecrypt` roundtrip; ciphertext tamper rejected; MAC tamper rejected; wrong-key rejected |
| `packet` | Flood/direct/transport-codes serialisation roundtrips; path encoding; header accessors; `getRawLength`; `isValidPathLen`; corrupt-input rejection; hash stability and sensitivity |
| `tables` | `SimpleMeshTables` flood and ACK deduplication; `clear()`; cross-type independence |

> **Note on reference vectors:** cryptographic test vectors were verified
> against both OpenSSL (`openssl dgst`, `openssl mac`) and Python
> (`hashlib`, `hmac`) before being committed.  Do not rely on LLM-recalled
> values for new vectors — always cross-check.
