"""Tests for subscription_days_left() - the "days left in the billing period"
helper.  Stdlib unittest, no pip install:

    python -m unittest discover -v
"""
import datetime
import unittest
from unittest import mock

import usage_sources as us


def _on(date_str):
    """Pin `datetime.date.today()` to a fixed day for a deterministic test."""
    fixed = datetime.date.fromisoformat(date_str)

    class _Date(datetime.date):
        @classmethod
        def today(cls):
            return fixed

    return mock.patch.object(us.datetime, "date", _Date)


class RenewsDayTests(unittest.TestCase):
    def test_day_within_month_is_exact(self):
        with _on("2026-09-07"):
            self.assertEqual(
                us.subscription_days_left({"renews_day": 28})["renews_on"],
                "28 Sep 2026")

    def test_day_29_30_are_not_clamped_to_28(self):
        with _on("2026-09-07"):
            self.assertEqual(
                us.subscription_days_left({"renews_day": 29})["renews_on"],
                "29 Sep 2026")
            self.assertEqual(
                us.subscription_days_left({"renews_day": 30})["renews_on"],
                "30 Sep 2026")

    def test_day_31_lands_on_month_end_not_the_28th(self):
        with _on("2026-09-07"):                       # September has 30 days
            self.assertEqual(
                us.subscription_days_left({"renews_day": 31})["renews_on"],
                "30 Sep 2026")

    def test_day_31_in_february(self):
        with _on("2026-02-05"):                       # 2026 Feb has 28 days
            self.assertEqual(
                us.subscription_days_left({"renews_day": 31})["renews_on"],
                "28 Feb 2026")
        with _on("2028-02-05"):                       # 2028 is a leap year
            self.assertEqual(
                us.subscription_days_left({"renews_day": 31})["renews_on"],
                "29 Feb 2028")

    def test_todays_day_rolls_to_next_month(self):
        with _on("2026-09-15"):
            r = us.subscription_days_left({"renews_day": 15})
            self.assertEqual(r["renews_on"], "15 Oct 2026")
            self.assertEqual(r["days_left"], 30)


class MalformedInputTests(unittest.TestCase):
    def test_non_numeric_renews_day_returns_none(self):
        self.assertIsNone(us.subscription_days_left({"renews_day": "x"}))

    def test_garbage_renews_date_returns_none(self):
        self.assertIsNone(us.subscription_days_left({"renews": "not-a-date"}))

    def test_missing_and_empty_return_none(self):
        self.assertIsNone(us.subscription_days_left({}))
        self.assertIsNone(us.subscription_days_left(None))
        self.assertIsNone(us.subscription_days_left({"renews_day": None}))

    def test_out_of_range_day_is_pinned_not_crashed(self):
        with _on("2026-09-07"):
            self.assertEqual(                          # 99 -> month end
                us.subscription_days_left({"renews_day": 99})["renews_on"],
                "30 Sep 2026")
            self.assertEqual(                          # 0 -> day 1, already
                us.subscription_days_left({"renews_day": 0})["renews_on"],
                "01 Oct 2026")                         # past, so rolls a month


class RenewsDateTests(unittest.TestCase):
    def test_iso_date_rolls_forward_monthly_with_month_length(self):
        with _on("2026-09-07"):
            r = us.subscription_days_left({"renews": "2026-01-31"})
            self.assertEqual(r["renews_on"], "30 Sep 2026")   # not 28

    def test_future_date_passes_through(self):
        with _on("2026-09-07"):
            r = us.subscription_days_left({"renews": "2026-09-20"})
            self.assertEqual(r["renews_on"], "20 Sep 2026")
            self.assertEqual(r["days_left"], 13)


if __name__ == "__main__":
    unittest.main()
