# Developer notes for Claude

## README maintenance

**Update the relevant README(s) whenever you make a change that affects them.**

| Change type | File(s) to update |
|-------------|-------------------|
| New topology JSON field or changed semantics | `README.md` → Topology file format section |
| New orchestrator CLI flag | `README.md` → Orchestrator reference section |
| New orchestrator module or significant refactor | `README.md` → Repository layout and/or Architecture sections |
| node_agent wire-protocol change (new stdin/stdout message type) | `node_agent/README.md` → Wire protocol tables |
| node_agent build system change (new dependency, new flag) | `node_agent/README.md` → Prerequisites and Build sections |
| New C++ test group | `node_agent/README.md` → Running the tests / Test coverage table |
| Change to `python3 -m sim_tests` test count or groups | `README.md` → Running the tests table |
| New example topology file | `README.md` → Repository layout (topologies/) |

## Before committing

1. Run `python3 -m sim_tests` and confirm all non-skipped tests pass.
2. If the node_agent was rebuilt, also verify `./tests/build/meshcore_tests`
   reports 45 passed, 0 failed.

## Test locations

- **C++ tests** (crypto shims, packet serialisation): `tests/`
  Run: `cd tests && cmake -S . -B build && cmake --build build && ./build/meshcore_tests`

- **Python tests** (orchestrator unit + integration + C++ wrapper): `sim_tests/`
  Run: `python3 -m sim_tests`

Integration tests in `sim_tests/` are automatically skipped when the
`node_agent/build/node_agent` binary is absent — they are not failures.

## Key design invariants

- **No changes to MeshCore source.** The `MeshCore/` submodule is compiled
  as-is. All platform adaptation goes in `node_agent/` shims.
- **node_agent wire protocol is newline-delimited JSON.** One JSON object
  per line on stdin and stdout. Packet payloads are lowercase hex.
- **Topology JSON is backward-compatible.** New optional fields must default
  to the existing behaviour when absent. Do not rename or remove existing
  fields without a migration plan.
- **Python 3.9+ compatibility.** Avoid syntax or stdlib features that require
  3.10+. In particular: use `Optional[X]` not `X | None` in dataclass field
  annotations (though `X | None` is fine in function signatures with
  `from __future__ import annotations`).
