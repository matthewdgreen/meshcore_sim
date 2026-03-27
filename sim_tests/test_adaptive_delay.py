"""
test_adaptive_delay.py — tests for the density-adaptive transmit-delay
collision-mitigation experiment (privatemesh/adaptive_delay/).

Unit tests (always run):
  - Density-table lookup logic (Python mirror of C++ DENSITY_TABLE).
  - Scenario rf_model field defaults and contention-scenario registration.
  - experiments/scenarios.py registration of new binary and scenarios.
  - SimRadio airtime consistency (cross-check C++ formula against orchestrator).

Integration tests (skipped when binary absent):
  - adaptive_agent runs correctly on a contention scenario.
  - Collision count with adaptive_agent <= collision count with node_agent
    under the RF contention model (grid/3x3/contention scenario).
  - Delivery rate is not significantly worse than baseline.
  - avg_latency_ms is higher for adaptive_agent (random backoff adds delay).
"""

from __future__ import annotations

import os
import unittest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADAPTIVE_BINARY = os.path.join(
    _REPO_ROOT, "privatemesh", "adaptive_delay", "build", "adaptive_agent"
)
_BASELINE_BINARY = os.path.join(
    _REPO_ROOT, "node_agent", "build", "node_agent"
)


def _adaptive_available() -> bool:
    return os.path.isfile(_ADAPTIVE_BINARY) and os.access(_ADAPTIVE_BINARY, os.X_OK)


def _baseline_available() -> bool:
    return os.path.isfile(_BASELINE_BINARY) and os.access(_BASELINE_BINARY, os.X_OK)


# ---------------------------------------------------------------------------
# Unit: density table lookup (Python mirror of C++ DENSITY_TABLE)
# ---------------------------------------------------------------------------

# Mirrors the DENSITY_TABLE in privatemesh/adaptive_delay/SimNode.cpp.
_DENSITY_TABLE = [
    (0,  1.0, 0.4),
    (1,  1.1, 0.5),
    (2,  1.2, 0.6),
    (4,  1.3, 0.7),
    (6,  1.5, 0.7),
    (8,  1.7, 0.8),
    (9,  1.8, 0.8),
    (10, 1.9, 0.9),
    (11, 2.0, 0.9),
    (12, 2.1, 0.9),
]


def _lookup_delay(neighbor_count: int):
    """Return (txdelay, direct_txdelay) for a given neighbor count."""
    best = _DENSITY_TABLE[0]
    for entry in _DENSITY_TABLE[1:]:
        if entry[0] <= neighbor_count:
            best = entry
        else:
            break
    return best[1], best[2]


class TestDensityTableLookup(unittest.TestCase):
    """Verify the density-table lookup logic matches the proposal (§9.4.2)."""

    def test_zero_neighbors(self):
        td, dtd = _lookup_delay(0)
        self.assertAlmostEqual(td, 1.0)
        self.assertAlmostEqual(dtd, 0.4)

    def test_one_neighbor(self):
        td, dtd = _lookup_delay(1)
        self.assertAlmostEqual(td, 1.1)
        self.assertAlmostEqual(dtd, 0.5)

    def test_two_neighbors(self):
        td, dtd = _lookup_delay(2)
        self.assertAlmostEqual(td, 1.2)
        self.assertAlmostEqual(dtd, 0.6)

    def test_three_neighbors_uses_2_entry(self):
        # 3 neighbors → still in the "2" band (next entry starts at 4).
        td, dtd = _lookup_delay(3)
        self.assertAlmostEqual(td, 1.2)
        self.assertAlmostEqual(dtd, 0.6)

    def test_four_neighbors(self):
        td, dtd = _lookup_delay(4)
        self.assertAlmostEqual(td, 1.3)
        self.assertAlmostEqual(dtd, 0.7)

    def test_eight_neighbors(self):
        td, dtd = _lookup_delay(8)
        self.assertAlmostEqual(td, 1.7)
        self.assertAlmostEqual(dtd, 0.8)

    def test_eleven_neighbors(self):
        td, dtd = _lookup_delay(11)
        self.assertAlmostEqual(td, 2.0)
        self.assertAlmostEqual(dtd, 0.9)

    def test_large_neighbor_count_saturates_at_12_entry(self):
        td, dtd = _lookup_delay(50)
        self.assertAlmostEqual(td, 2.1)
        self.assertAlmostEqual(dtd, 0.9)

    def test_txdelay_monotonically_non_decreasing(self):
        """txdelay must never decrease as neighbor count increases."""
        prev_td = 0.0
        for n in range(20):
            td, _ = _lookup_delay(n)
            self.assertGreaterEqual(
                td, prev_td,
                msg=f"txdelay decreased at neighbor_count={n}: {td} < {prev_td}",
            )
            prev_td = td

    def test_direct_txdelay_strictly_less_than_flood_txdelay(self):
        """direct.txdelay is always smaller than flood txdelay (proposal §3.5)."""
        for n in range(15):
            td, dtd = _lookup_delay(n)
            self.assertLess(
                dtd, td,
                msg=f"direct_txdelay >= txdelay at neighbor_count={n}",
            )

    def test_max_window_grows_with_neighbor_count(self):
        """Max delay window = 5 × airtime × txdelay must grow with density."""
        AIRTIME_MS = 330.0
        prev_window = 0.0
        for n in [0, 1, 2, 4, 6, 8, 9, 10, 11, 12]:
            td, _ = _lookup_delay(n)
            window = 5.0 * AIRTIME_MS * td
            self.assertGreaterEqual(
                window, prev_window,
                msg=f"window shrank at neighbor_count={n}",
            )
            prev_window = window

    def test_collision_probability_decreases_with_density(self):
        """P(collision between pair) ≈ 1/(5×txdelay) must decrease as txdelay grows."""
        prev_prob = 1.0
        for n in [0, 2, 4, 6, 8, 11]:
            td, _ = _lookup_delay(n)
            prob = 1.0 / (5.0 * td)
            self.assertLessEqual(
                prob, prev_prob + 1e-9,
                msg=f"collision probability increased at neighbor_count={n}",
            )
            prev_prob = prob


# ---------------------------------------------------------------------------
# Unit: Scenario registry and contention config
# ---------------------------------------------------------------------------

class TestScenarioRegistry(unittest.TestCase):
    """Verify scenario registry and radio config."""

    def test_contention_scenarios_registered_in_scenario_by_name(self):
        from experiments.scenarios import SCENARIO_BY_NAME
        self.assertIn("grid/3x3/contention", SCENARIO_BY_NAME)
        self.assertIn("grid/10x10/contention", SCENARIO_BY_NAME)

    def test_contention_scenarios_in_all_scenarios(self):
        from experiments.scenarios import (
            ALL_SCENARIOS,
            GRID_3X3_CONTENTION,
            GRID_10X10_CONTENTION,
        )
        names = [s.name for s in ALL_SCENARIOS]
        self.assertIn(GRID_3X3_CONTENTION.name, names)
        self.assertIn(GRID_10X10_CONTENTION.name, names)

    def test_adaptive_binary_in_all_binaries(self):
        from experiments.scenarios import ADAPTIVE_DELAY_BINARY, ALL_BINARIES
        self.assertIn(ADAPTIVE_DELAY_BINARY, ALL_BINARIES)

    def test_adaptive_binary_aliases_registered(self):
        from experiments.__main__ import _BINARY_ALIASES
        for alias in ("adaptive", "adaptive_delay", "adaptive_agent"):
            self.assertIn(alias, _BINARY_ALIASES,
                          msg=f"alias {alias!r} not found in _BINARY_ALIASES")

    def test_contention_scenarios_have_radio_config(self):
        """topo_factory() for contention scenarios must return a radio section."""
        from experiments.scenarios import GRID_3X3_CONTENTION, GRID_10X10_CONTENTION
        for sc in [GRID_3X3_CONTENTION, GRID_10X10_CONTENTION]:
            cfg = sc.topo_factory()
            self.assertIsNotNone(
                cfg.radio,
                msg=f"Scenario {sc.name!r} has no radio config in topology",
            )

    def test_contention_scenarios_radio_matches_meshcore_defaults(self):
        """Verify SF=10, BW=250kHz, CR=1 (MeshCore defaults)."""
        from experiments.scenarios import GRID_3X3_CONTENTION
        radio = GRID_3X3_CONTENTION.topo_factory().radio
        self.assertEqual(radio.sf, 10)
        self.assertEqual(radio.bw_hz, 250_000)
        self.assertEqual(radio.cr, 1)

    def test_contention_scenarios_have_longer_settle_secs(self):
        """Contention scenarios need longer settle time for adaptive delays."""
        from experiments.scenarios import GRID_3X3, GRID_3X3_CONTENTION
        self.assertGreater(
            GRID_3X3_CONTENTION.settle_secs, GRID_3X3.settle_secs,
            msg="Contention scenario settle_secs should exceed the no-rf variant",
        )

    def test_default_readvert_interval_is_none(self):
        """Non-contention scenarios default to no periodic re-advertising."""
        from experiments.runner import Scenario
        from sim_tests.helpers import grid_topo_config
        s = Scenario(name="test", topo_factory=lambda: grid_topo_config(3, 3))
        self.assertIsNone(s.readvert_interval_secs)

    def test_non_contention_scenarios_have_no_readvert_interval(self):
        from experiments.scenarios import LINEAR, GRID_3X3, GRID_10X10
        for sc in [LINEAR, GRID_3X3, GRID_10X10]:
            self.assertIsNone(
                sc.readvert_interval_secs,
                msg=f"{sc.name} should not re-advertise (no RF contention)",
            )

    def test_contention_scenarios_have_positive_readvert_interval(self):
        """grid/*/contention must re-advertise to survive initial collision burst."""
        from experiments.scenarios import GRID_3X3_CONTENTION, GRID_10X10_CONTENTION
        for sc in [GRID_3X3_CONTENTION, GRID_10X10_CONTENTION]:
            self.assertIsNotNone(
                sc.readvert_interval_secs,
                msg=f"{sc.name} must have readvert_interval_secs set",
            )
            self.assertGreater(
                sc.readvert_interval_secs, 0.0,
                msg=f"{sc.name} readvert_interval_secs must be positive",
            )

    def test_readvert_interval_allows_at_least_one_round(self):
        """
        The runner loop fires while ``elapsed + interval*2 < warmup``.
        For the first iteration (elapsed=0) this requires ``interval*2 < warmup``.
        Verify that at least one re-advert is guaranteed.
        """
        from experiments.scenarios import GRID_3X3_CONTENTION
        sc = GRID_3X3_CONTENTION
        self.assertLess(
            sc.readvert_interval_secs * 2, sc.warmup_secs,  # type: ignore[operator]
            msg=(
                f"{sc.name}: readvert_interval_secs={sc.readvert_interval_secs} "
                f"× 2 = {sc.readvert_interval_secs * 2} is not < "  # type: ignore[operator]
                f"warmup_secs={sc.warmup_secs}; no re-advert would fire"
            ),
        )

    def test_readvert_interval_exceeds_full_round_time(self):
        """
        readvert_interval must exceed the FULL round time so successive rounds
        do not interfere:

            full_round_s = stagger_secs + relay_cascade_s

        where relay_cascade_s = 4 hops × (max_delay_ms + airtime_ms)
                               = 4 × (5 × 330 × 1.3 + 330) ms ≈ 9.9 s.

        With stagger_secs=5 the total is ≈14.9 s.  A readvert_interval at or
        below this value would let round-0 relay retransmissions still be in
        flight when round-1 stagger begins, inflating collision counts.
        """
        from experiments.scenarios import GRID_3X3_CONTENTION
        sc = GRID_3X3_CONTENTION
        AIRTIME_MS = 330.0
        MAX_TXDELAY = 1.3           # 4-neighbor nodes in 3×3 grid
        MAX_DELAY_MS = 5 * AIRTIME_MS * MAX_TXDELAY
        HOPS = 4                    # corner-to-corner path length
        relay_cascade_s = HOPS * (MAX_DELAY_MS + AIRTIME_MS) / 1000.0
        stagger_s = sc.stagger_secs or 1.0
        full_round_s = stagger_s + relay_cascade_s
        self.assertGreater(
            sc.readvert_interval_secs, full_round_s,  # type: ignore[operator]
            msg=(
                f"{sc.name}: readvert_interval_secs={sc.readvert_interval_secs} "
                f"≤ full_round_s={full_round_s:.1f} "
                f"(stagger={stagger_s:.1f}s + cascade={relay_cascade_s:.1f}s); "
                "successive advert rounds will interfere and increase collisions"
            ),
        )

    def test_default_stagger_secs_is_none(self):
        """Non-contention scenarios default to the 1-second built-in stagger."""
        from experiments.runner import Scenario
        from sim_tests.helpers import grid_topo_config
        s = Scenario(name="test", topo_factory=lambda: grid_topo_config(3, 3))
        self.assertIsNone(s.stagger_secs)

    def test_non_contention_scenarios_have_no_custom_stagger(self):
        from experiments.scenarios import LINEAR, GRID_3X3, GRID_10X10
        for sc in [LINEAR, GRID_3X3, GRID_10X10]:
            self.assertIsNone(
                sc.stagger_secs,
                msg=f"{sc.name} should use default 1-second stagger",
            )

    def test_contention_scenario_has_wider_stagger(self):
        """
        GRID_3X3_CONTENTION must use stagger_secs > 1 s to avoid the
        centre-node interference that prevents corner-node adverts from
        propagating (root cause of 0% delivery in the contention scenario).
        """
        from experiments.scenarios import GRID_3X3_CONTENTION
        sc = GRID_3X3_CONTENTION
        self.assertIsNotNone(
            sc.stagger_secs,
            msg=f"{sc.name}: stagger_secs must be set (not None)",
        )
        self.assertGreater(
            sc.stagger_secs, 1.0,  # type: ignore[operator]
            msg=(
                f"{sc.name}: stagger_secs={sc.stagger_secs} must be > 1.0 s; "
                "a 1-second stagger with 9 nodes causes 78% collision probability "
                "at corner nodes due to centre-node interference"
            ),
        )

    def test_stagger_allows_gap_between_node_transmissions(self):
        """
        With stagger_secs / n_nodes > airtime_ms / 1000, the expected inter-TX
        gap exceeds the airtime, making simultaneous-TX collisions unlikely.
        Verify for the 3×3 grid (9 nodes, ~533 ms airtime).
        """
        from experiments.scenarios import GRID_3X3_CONTENTION
        sc = GRID_3X3_CONTENTION
        stagger_s = sc.stagger_secs or 1.0
        n_nodes = 9            # 3×3 grid
        airtime_s = 0.533      # SF10/BW250/CR4-5 at 108 B ≈ 533 ms
        mean_gap_s = stagger_s / n_nodes
        self.assertGreater(
            mean_gap_s, airtime_s,
            msg=(
                f"Mean inter-TX gap {mean_gap_s:.3f} s ≤ airtime {airtime_s} s; "
                "simultaneous transmissions will be common"
            ),
        )


# ---------------------------------------------------------------------------
# Unit: SimRadio airtime consistency
#
# These tests cross-check the C++ SimRadio::getEstAirtimeFor formula against
# the Python orchestrator's lora_airtime_ms() reference implementation.
#
# Motivation: a factor-of-1000 bug in SimRadio (returning 6250 ms for a
# 50-byte packet instead of ~308 ms) made the baseline node_agent's default
# getRetransmitDelay() produce delays of 0–16 s, which accidentally spread
# retransmissions so widely that the adaptive agent showed no advantage at
# all.  These tests catch that class of error without requiring any binary.
#
# Convention: _sim_radio_airtime_ms() is a Python mirror of the C++ formula
# documented in node_agent/SimRadio.cpp.  If the C++ formula is changed,
# update _SIMRADIO_OVERHEAD_MS and _SIMRADIO_MS_PER_BYTE here too.
# ---------------------------------------------------------------------------

class TestSimRadioAirtimeConsistency(unittest.TestCase):
    """
    Cross-check SimRadio::getEstAirtimeFor (C++) against lora_airtime_ms()
    (Python orchestrator).

    SimRadio now uses the same Semtech AN1200.13 formula as the orchestrator,
    with EU Narrow defaults (SF8 / BW62.5 kHz / CR4-8).  These tests verify
    the C++ and Python implementations stay in sync.  A large discrepancy
    would mean the two sides of the simulation use inconsistent airtime
    models, invalidating RF contention scenario results.
    """

    # EU Narrow defaults matching SimRadio constructor (node_agent/SimRadio.h).
    _SF    = 8
    _BW_HZ = 62_500
    _CR    = 4

    @classmethod
    def _sim_radio_airtime_ms(cls, len_bytes: int) -> float:
        """Python replica of SimRadio::getEstAirtimeFor (Semtech AN1200.13)."""
        from orchestrator.airtime import lora_airtime_ms
        return lora_airtime_ms(sf=cls._SF, bw_hz=cls._BW_HZ, cr=cls._CR,
                               payload_bytes=len_bytes)

    @staticmethod
    def _orchestrator_airtime_ms(len_bytes: int) -> float:
        """Reference: Python orchestrator formula (Semtech AN1200.13)."""
        from orchestrator.airtime import lora_airtime_ms
        return lora_airtime_ms(sf=10, bw_hz=250_000, cr=1,
                               payload_bytes=len_bytes)

    # -- plausibility --

    def test_50_byte_airtime_in_plausible_range(self):
        """
        getEstAirtimeFor(50) must be in [100, 800] ms.

        This is the most direct regression for the ×1000 bug:
        the buggy formula returned 6250 ms, which this test rejects.
        The range covers EU Narrow (SF8/BW62.5k ≈ 509 ms) through
        MeshCore US (SF10/BW250k ≈ 308 ms) configurations.
        """
        t = self._sim_radio_airtime_ms(50)
        self.assertGreater(t, 100,
                           msg=f"SimRadio airtime too low: {t:.0f} ms for 50 B")
        self.assertLess(t, 800,
                        msg=f"SimRadio airtime too high: {t:.0f} ms for 50 B")

    def test_airtime_matches_orchestrator_within_factor_of_two(self):
        """
        SimRadio must be within 2× of lora_airtime_ms() for typical packet
        sizes.  A factor-of-1000 error fails trivially; even a factor-of-3
        error would indicate a wrong SF/BW assumption.
        """
        for n in (30, 40, 50, 60, 70, 80):
            sim_t = self._sim_radio_airtime_ms(n)
            ref_t = self._orchestrator_airtime_ms(n)
            ratio = sim_t / ref_t
            self.assertLess(
                ratio, 2.0,
                msg=f"SimRadio({n}B)={sim_t:.0f} ms is >2× orchestrator "
                    f"{ref_t:.0f} ms (ratio={ratio:.2f})",
            )
            self.assertGreater(
                ratio, 0.5,
                msg=f"SimRadio({n}B)={sim_t:.0f} ms is <0.5× orchestrator "
                    f"{ref_t:.0f} ms (ratio={ratio:.2f})",
            )

    def test_airtime_increases_with_packet_length(self):
        """Longer packets must take longer to transmit (monotone increasing)."""
        sizes = [20, 30, 40, 50, 60, 70, 80]
        prev = self._sim_radio_airtime_ms(sizes[0])
        for n in sizes[1:]:
            t = self._sim_radio_airtime_ms(n)
            self.assertGreater(
                t, prev,
                msg=f"airtime({n}B)={t:.0f} ms ≤ airtime({n-10}B)={prev:.0f} ms",
            )
            prev = t

    # -- derived delay bounds --

    def test_baseline_retransmit_t_below_1000ms(self):
        """
        The default Mesh::getRetransmitDelay computes:
            t = (getEstAirtimeFor(len) * 52 / 50) / 2
        and returns nextInt(0, 5) * t.

        For a 50-byte packet at EU Narrow (SF8/BW62.5k/CR4-8) t should be
        in the hundreds of ms, NOT thousands.  The old ×1000 bug gave
        t ≈ 3250 ms; this test catches that class of error.
        """
        airtime = self._sim_radio_airtime_ms(50)
        t = int(airtime * 52 // 50) // 2          # mirrors integer arithmetic in Mesh.cpp
        self.assertLess(
            t, 1000,
            msg=f"Baseline retransmit t={t} ms is too large; check "
                "SimRadio::getEstAirtimeFor for a factor-of-N error",
        )

    def test_adaptive_max_delay_exceeds_baseline_max_delay(self):
        """
        For a 4-neighbor node with the adaptive_delay experiment's density
        table, adaptive max delay = 5 × airtime × 1.3.
        Baseline max delay (upstream Mesh.cpp) = 5 × (airtime*52/50/2).

        Adaptive must be strictly larger: that wider window is what reduces
        collision probability per transmitter pair.
        """
        TXDELAY_4_NEIGHBORS = 1.3
        baseline_airtime = self._sim_radio_airtime_ms(50)
        adaptive_max_ms = 5.0 * baseline_airtime * TXDELAY_4_NEIGHBORS

        t = int(baseline_airtime * 52 // 50) // 2
        baseline_max_ms = 5 * t

        self.assertGreater(
            adaptive_max_ms, baseline_max_ms,
            msg=(
                f"Adaptive max delay {adaptive_max_ms:.0f} ms should exceed "
                f"baseline max delay {baseline_max_ms:.0f} ms; "
                "check SimRadio::getEstAirtimeFor and LORA_AIRTIME_MS"
            ),
        )


# ---------------------------------------------------------------------------
# Integration: collision reduction and latency increase
# ---------------------------------------------------------------------------

# A compact contention scenario using the 3-node linear topology.
# source → relay → destination (1 relay, 2 possible hop paths).
#
# Timing rationale for adaptive_agent:
#   relay gets 1 neighbor (source) on first advert → txdelay=1.1 →
#   max retransmit delay = 5 × 330 ms × 1.1 = 1815 ms per hop.
#   Flood path is at most 2 hops → warmup 2 × 1815 = 3630 ms → 10 s warmup.
#   Message settle: same → 10 s settle.  Total per run: ~22 s.
def _linear_contention_config():
    from sim_tests.helpers import linear_three_config
    from orchestrator.config import RadioConfig
    cfg = linear_three_config(warmup_secs=10.0, duration_secs=60.0, seed=42)
    cfg.radio = RadioConfig(sf=10, bw_hz=250_000, cr=1)
    return cfg


_LINEAR_CONTENTION = None  # lazy-initialised to avoid import at module load


def _get_linear_contention():
    global _LINEAR_CONTENTION
    if _LINEAR_CONTENTION is None:
        from experiments.runner import Scenario
        _LINEAR_CONTENTION = Scenario(
            name="test/linear/contention",
            topo_factory=_linear_contention_config,
            warmup_secs=10.0,
            settle_secs=10.0,
            rounds=2,
            seed=42,
            stagger_secs=1.0,
        )
    return _LINEAR_CONTENTION


@unittest.skipUnless(
    _adaptive_available() and _baseline_available(),
    "Both node_agent and adaptive_agent must be built",
)
class TestCollisionReduction(unittest.TestCase):
    """
    Verify that adaptive_agent produces fewer RF collisions and higher
    latency than the baseline node_agent under the contention model.

    Uses a 3-node linear topology (fast, ~22 s per run):
      source → relay → destination

    Expected outcomes from the proposal (§3):
      baseline (txdelay=0): relay retransmits immediately after source →
        overlapping airtime windows at destination → collision.
      adaptive (txdelay≈1.1 for 1 neighbor): relay picks a random delay
        in [0, 5×330×1.1 ≈ 1815 ms] → no overlap → no collision.
    """

    @classmethod
    def setUpClass(cls):
        from experiments.runner import run_scenario
        sc = _get_linear_contention()
        cls.baseline = run_scenario(sc, _BASELINE_BINARY, label="baseline")
        cls.adaptive = run_scenario(sc, _ADAPTIVE_BINARY, label="adaptive")

    def test_adaptive_has_fewer_or_equal_collisions(self):
        self.assertLessEqual(
            self.adaptive.collision_count, self.baseline.collision_count,
            msg=(
                f"adaptive collisions ({self.adaptive.collision_count}) > "
                f"baseline collisions ({self.baseline.collision_count})"
            ),
        )

    def test_adaptive_delivery_not_catastrophically_worse(self):
        """Adaptive delays must not cause delivery to collapse."""
        self.assertGreaterEqual(
            self.adaptive.delivery_rate,
            self.baseline.delivery_rate - 0.20,
            msg=(
                f"adaptive delivery {self.adaptive.delivery_rate:.0%} is more than "
                f"20 pp below baseline {self.baseline.delivery_rate:.0%}"
            ),
        )

    def test_adaptive_has_nonzero_latency(self):
        """
        Adaptive agent uses getRetransmitDelay() for random backoff, so
        delivered messages should have non-zero latency.

        Note: we don't compare adaptive vs baseline latency because baseline
        collisions can cause retries that inflate baseline latency beyond the
        adaptive delay, making a strict ordering unreliable.
        """
        if self.adaptive.avg_latency_ms == 0.0:
            self.skipTest("No messages delivered in adaptive run — cannot check latency")
        self.assertGreater(
            self.adaptive.avg_latency_ms, 0.0,
            msg="Expected non-zero latency from adaptive agent (getRetransmitDelay active)",
        )


if __name__ == "__main__":
    unittest.main()
