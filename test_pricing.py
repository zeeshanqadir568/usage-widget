"""Tests for the PRICING table, config overrides, and cost maths.

Stdlib ``unittest`` on purpose - the widget itself needs no ``pip install``
and neither should its tests:

    python -m unittest discover -v
"""
import unittest

import usage_sources as us


class PricingOverrideTests(unittest.TestCase):
    """apply_pricing_overrides() must rebuild PRICING from the defaults each
    call, so overrides from one config never leak into the next."""

    def setUp(self):
        # Every test starts from the built-in table, whatever ran before it.
        us.apply_pricing_overrides(None)

    tearDown = setUp

    def test_defaults_present_without_config(self):
        self.assertEqual(us.PRICING["claude-sonnet-5"], (2.0, 10.0))
        self.assertEqual(us.PRICING["claude-opus-5"], (5.0, 25.0))

    def test_override_replaces_default(self):
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": [3.0, 15.0]}})
        self.assertEqual(us.PRICING["claude-sonnet-5"], (3.0, 15.0))

    def test_override_adds_unknown_model(self):
        us.apply_pricing_overrides({"pricing": {"my-model": [1.0, 4.0]}})
        self.assertEqual(us.PRICING["my-model"], (1.0, 4.0))

    def test_later_config_does_not_inherit_earlier_override(self):
        """The leak this reset exists to prevent."""
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": [3.0, 15.0],
                                                "my-model": [1.0, 4.0]}})
        us.apply_pricing_overrides({"providers": {}})           # no pricing block
        self.assertEqual(us.PRICING["claude-sonnet-5"], (2.0, 10.0))
        self.assertNotIn("my-model", us.PRICING)

    def test_keys_are_lowercased(self):
        us.apply_pricing_overrides({"pricing": {"MY-Model": [1.0, 4.0]}})
        self.assertIn("my-model", us.PRICING)
        self.assertEqual(us.price_for("MY-Model-v2"), (1.0, 4.0))

    def test_values_are_coerced_to_float(self):
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": ["3", 15]}})
        self.assertEqual(us.PRICING["claude-sonnet-5"], (3.0, 15.0))

    def test_none_and_empty_config_are_safe(self):
        for cfg in (None, {}, {"pricing": None}, {"pricing": {}}):
            with self.subTest(cfg=cfg):
                us.apply_pricing_overrides(cfg)
                self.assertEqual(us.PRICING["claude-sonnet-5"], (2.0, 10.0))

    def test_malformed_entries_are_skipped_not_fatal(self):
        n = len(us.PRICING)
        us.apply_pricing_overrides({"pricing": {
            "bad-short": [1.0],                 # too few values
            "bad-text": "nonsense",             # not a pair
            "bad-nan": ["x", "y"],              # not numeric
            "claude-sonnet-5": [3.0, 15.0],     # the one good entry
        }})
        self.assertEqual(us.PRICING["claude-sonnet-5"], (3.0, 15.0))
        self.assertEqual(len(us.PRICING), n)    # nothing bad got in
        for bad in ("bad-short", "bad-text", "bad-nan"):
            self.assertNotIn(bad, us.PRICING)

    def test_pricing_object_identity_is_stable(self):
        """Mutated in place, never rebound - ``from usage_sources import
        PRICING`` elsewhere must keep seeing updates."""
        before = us.PRICING
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": [3.0, 15.0]}})
        self.assertIs(us.PRICING, before)

    def test_defaults_snapshot_is_not_mutated(self):
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": [3.0, 15.0]}})
        self.assertEqual(us._PRICING_DEFAULTS["claude-sonnet-5"], (2.0, 10.0))


class PriceLookupTests(unittest.TestCase):

    def setUp(self):
        us.apply_pricing_overrides(None)

    tearDown = setUp

    def test_exact_match(self):
        self.assertEqual(us.price_for("claude-opus-5"), (5.0, 25.0))

    def test_prefix_match(self):
        self.assertEqual(us.price_for("claude-sonnet-5-20260101"), (2.0, 10.0))

    def test_longest_prefix_wins(self):
        # "gpt-5-mini" must beat the shorter "gpt-5".
        self.assertEqual(us.price_for("gpt-5-mini-2026"), (0.25, 2.0))

    def test_case_insensitive(self):
        self.assertEqual(us.price_for("Claude-Sonnet-5"), (2.0, 10.0))

    def test_unknown_and_empty_return_none(self):
        for model in ("llama-3", "", None):
            with self.subTest(model=model):
                self.assertIsNone(us.price_for(model))


class CostTests(unittest.TestCase):

    def setUp(self):
        us.apply_pricing_overrides(None)

    tearDown = setUp

    def test_cost_uses_per_million_rates(self):
        # 1M in @ $2 + 1M out @ $10.
        self.assertAlmostEqual(us.cost_of("claude-sonnet-5", 1e6, 1e6), 12.0)

    def test_unknown_model_costs_zero(self):
        self.assertEqual(us.cost_of("llama-3", 1e6, 1e6), 0.0)

    def test_cost_follows_an_override(self):
        us.apply_pricing_overrides({"pricing": {"claude-sonnet-5": [4.0, 20.0]}})
        self.assertAlmostEqual(us.cost_of("claude-sonnet-5", 1e6, 1e6), 24.0)


if __name__ == "__main__":
    unittest.main()
