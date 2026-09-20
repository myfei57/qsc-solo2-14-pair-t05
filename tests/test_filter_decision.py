"""过滤决策引擎：分区、节奏、助剂与放行判定。"""

from __future__ import annotations

import unittest

from breweryctl.core.config import Settings
from breweryctl.domain import filter_decision as fd


class FilterDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings().validate()

    def test_classify_zones(self) -> None:
        s = self.settings
        self.assertEqual(fd.ZONE_NORMAL, fd.classify(0.5, 0.4, s))
        self.assertEqual(fd.ZONE_TURBID, fd.classify(2.0, 0.4, s))
        self.assertEqual(fd.ZONE_HIGH_DP, fd.classify(0.5, 1.9, s))
        self.assertEqual(fd.ZONE_BREAKTHROUGH, fd.classify(4.0, 0.4, s))
        # 已在回流时，浊度回落到 warn 以下但仍高于合格线，保持 turbid 不直接恢复。
        self.assertEqual(fd.ZONE_TURBID, fd.classify(1.0, 0.4, s, already_diverting=True))

    def test_turbid_zone_slow_down_and_feed_more(self) -> None:
        decision = fd.evaluate_reading(1.8, 0.5, self.settings)
        self.assertEqual(fd.ZONE_TURBID, decision.zone)
        self.assertLess(decision.target_pace_hl_h, 80.0)
        self.assertGreater(decision.body_feed_g_hl, 50.0)
        self.assertEqual("warning", decision.alarm_severity)
        self.assertFalse(decision.diverted)

    def test_breakthrough_diverts_and_raises_critical(self) -> None:
        decision = fd.evaluate_reading(3.5, 0.6, self.settings)
        self.assertTrue(decision.diverted)
        self.assertEqual("critical", decision.alarm_severity)
        self.assertTrue(any("回流" in action for action in decision.actions))

    def test_body_feed_clamped_to_settings(self) -> None:
        settings = Settings(filter_aid_max_g_hl=55.0).validate()
        rate = fd.body_feed_advice(fd.ZONE_BREAKTHROUGH, settings)
        self.assertEqual(55.0, rate)
        settings = Settings(filter_aid_min_g_hl=120.0).validate()
        rate = fd.body_feed_advice(fd.ZONE_NORMAL, settings)
        self.assertEqual(120.0, rate)

    def test_dose_for_interval(self) -> None:
        # 50 g/hl × 8 hl = 400 g。
        self.assertEqual(400.0, fd.dose_for_interval(50.0, 8.0))

    def test_release_ready_requires_volume_and_streak(self) -> None:
        s = self.settings
        good = [0.5] * s.filter_release_samples
        dp = [0.6] * s.filter_release_samples
        report = fd.release_ready(
            stage="filtering",
            filtered_l=950.0,
            target_volume_l=1000.0,
            recent_turbidity=good,
            recent_dp=dp,
            good_streak=s.filter_release_samples,
            settings=s,
        )
        self.assertTrue(report["ready"], report)

        short = fd.release_ready(
            stage="filtering",
            filtered_l=950.0,
            target_volume_l=1000.0,
            recent_turbidity=[0.5] * 2,
            recent_dp=[0.6] * 2,
            good_streak=2,
            settings=s,
        )
        self.assertFalse(short["ready"])
        self.assertIn("recent_samples", short["missing"])

        diverting = fd.release_ready(
            stage="diverting",
            filtered_l=1000.0,
            target_volume_l=1000.0,
            recent_turbidity=good,
            recent_dp=dp,
            good_streak=s.filter_release_samples,
            settings=s,
        )
        self.assertFalse(diverting["ready"])
        self.assertIn("stage", diverting["missing"])


if __name__ == "__main__":
    unittest.main()
