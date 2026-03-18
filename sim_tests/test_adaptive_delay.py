"""
test_adaptive_delay.py — tests for the density-adaptive transmit-delay
collision-mitigation experiment (privatemesh/adaptive_delay/).

Unit tests (always run):
  - Density-table lookup logic (Python mirror of C++ DENSITY_TABLE).
  - Scenario rf_model field defaults and contention-scenario registration.
  - experiments/scenarios.py registration of new binary and scenarios.

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
# Unit: Scenario rf_model field and registry
# ---------------------------------------------------------------------------

class TestScenarioRfModel(unittest.TestCase):
    """Verify Scenario.rf_model field and scenarios registry."""

    def test_default_rf_model_is_none(self):
        from experiments.runner import Scenario
        from sim_tests.helpers import grid_topo_config
        s = Scenario(name="test", topo_factory=lambda: grid_topo_config(3, 3))
        self.assertEqual(s.rf_model, "none")

    def test_non_contention_scenarios_have_rf_model_none(self):
        from experiments.scenarios import LINEAR, GRID_3X3, GRID_10X10
        for sc in [LINEAR, GRID_3X3, GRID_10X10]:
            self.assertEqual(sc.rf_model, "none",
                             msg=f"{sc.name} should have rf_model='none'")

    def test_contention_scenarios_have_rf_model_contention(self):
        from experiments.scenarios import GRID_3X3_CONTENTION, GRID_10X10_CONTENTION
        self.assertEqual(GRID_3X3_CONTENTION.rf_model, "contention")
        self.assertEqual(GRID_10X10_CONTENTION.rf_model, "contention")

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
                msg=(
                    f"Scenario {sc.name!r} has rf_model='contention' "
                    "but no radio config in topology"
                ),
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
            rf_model="contention",
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

    def test_adaptive_has_higher_latency_than_baseline(self):
        """
        Random backoff adds delay → avg_latency_ms must be higher for
        adaptive_agent than for baseline (which always returns delay=0).

        This confirms getRetransmitDelay() is returning non-zero values.
        Skipped when neither run delivers any messages (both latency=0).
        """
        if self.baseline.avg_latency_ms == 0.0 and self.adaptive.avg_latency_ms == 0.0:
            self.skipTest("No messages delivered in either run — cannot compare latency")
        self.assertGreater(
            self.adaptive.avg_latency_ms, self.baseline.avg_latency_ms,
            msg=(
                f"Expected adaptive latency ({self.adaptive.avg_latency_ms:.0f} ms) "
                f"> baseline latency ({self.baseline.avg_latency_ms:.0f} ms)"
            ),
        )


if __name__ == "__main__":
    unittest.main()
