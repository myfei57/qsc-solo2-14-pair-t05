"""成品过滤状态机：预涂、跑浑回流、助剂节奏、合格凭证与返工。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import InterlockError, SequenceError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    first_filter_unit,
    first_tank,
    make_app,
    mash_to_filter,
    mature_batch,
    sanitize_tank,
)


class FilterFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing
        self.filtration = self.app.registry.filtration

    def _matured_batch(self) -> tuple[str, str]:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        self.brewing.mark_cooled(batch_id, 10.0, "tester")
        free_tanks = [
            str(item["id"])
            for item in self.app.registry.tanks.list_tanks()
            if item["stage"] == "idle"
        ]
        tank_id = free_tanks[0]
        sanitize_tank(self.app, tank_id)
        self.brewing.transfer_to_tank(batch_id, tank_id, "tester")
        mature_batch(self.app, batch_id, tank_id)
        return batch_id, tank_id

    def _start_run(self, batch_id: str, target_volume_l: float = 1000.0) -> str:
        unit_id = first_filter_unit(self.app)
        view = self.filtration.start_run(unit_id, batch_id, target_volume_l, "tester", 2400.0)
        self.assertEqual("precoat", view["run"]["stage"])
        return str(view["run"]["id"])

    def test_only_matured_batch_can_start_and_precoat_gate(self) -> None:
        fresh = create_batch(self.app)
        unit_id = first_filter_unit(self.app)
        with self.assertRaises(SequenceError):
            self.filtration.start_run(unit_id, fresh, 1000.0, "tester", 2400.0)
        batch_id, _ = self._matured_batch()
        self.assertEqual("maturing", self.brewing.status(batch_id)["batch"]["stage"])
        run_id = self._start_run(batch_id)
        self.assertEqual("filtering", self.brewing.status(batch_id)["batch"]["stage"])
        # 预涂浊度不合格不能转入正式过滤。
        with self.assertRaises(InterlockError):
            self.filtration.confirm_precoat(run_id, 2.5, 0.3, "tester")
        view = self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")
        self.assertEqual("filtering", view["run"]["stage"])

    def test_breakthrough_diverts_and_blocks_finish_until_recovered(self) -> None:
        batch_id, _ = self._matured_batch()
        run_id = self._start_run(batch_id)
        self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")

        view = self.filtration.sample(run_id, 4.0, 0.5, 60.0, 200.0, "tester")
        self.assertEqual("diverting", view["run"]["stage"])
        self.assertTrue(view["readings"][-1]["diverted"])
        # 回流期间无法直接放行。
        with self.assertRaises(InterlockError):
            self.filtration.finish_run(run_id, "tester")

        # 浊度恢复但连续合格次数不够，仍然回流。
        view = self.filtration.sample(run_id, 0.6, 0.5, 60.0, 300.0, "tester")
        self.assertEqual("diverting", view["run"]["stage"])

    def test_full_pass_issues_certificate_and_completes_batch(self) -> None:
        batch_id, _ = self._matured_batch()
        run_id = self._start_run(batch_id, target_volume_l=1000.0)
        self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")

        # 前期有一次浊度偏高，系统降速加料，随后恢复。
        view = self.filtration.sample(run_id, 1.8, 0.6, 80.0, 100.0, "tester")
        self.assertEqual("filtering", view["run"]["stage"])
        advice = self.filtration.advise_body_feed(run_id)
        self.assertGreater(advice["body_feed_g_hl"], 50.0)
        self.filtration.dose_body_aid(run_id, 600.0, "tester", basis_g_hl=advice["body_feed_g_hl"])

        cumulative = 100.0
        for index in range(6):
            cumulative += 150.0
            view = self.filtration.sample(run_id, 0.5, 0.7, 80.0, cumulative, "tester")
        self.assertEqual("filtering", view["run"]["stage"])
        self.assertTrue(view["release"]["ready"], view["release"])

        finished = self.filtration.finish_run(run_id, "tester")
        self.assertEqual("passed", finished["run"]["stage"])
        self.assertEqual("pass", finished["certificate"]["verdict"])
        self.assertEqual(
            finished["certificate"]["id"],
            self.brewing.status(batch_id)["batch"]["filter_certificate_id"],
        )
        # 拿到合格凭证后批次才能完工。
        completed = self.brewing.complete_batch(batch_id, "tester")
        self.assertEqual("completed", completed["batch"]["stage"])

    def test_cannot_finish_without_enough_volume(self) -> None:
        batch_id, _ = self._matured_batch()
        run_id = self._start_run(batch_id)
        self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")
        cumulative = 100.0
        for _ in range(6):
            cumulative += 50.0
            self.filtration.sample(run_id, 0.5, 0.5, 80.0, cumulative, "tester")
        view = self.filtration.run_status(run_id)
        self.assertFalse(view["release"]["ready"])
        self.assertIn("volume", view["release"]["missing"])
        with self.assertRaises(InterlockError):
            self.filtration.finish_run(run_id, "tester")

    def test_abort_returns_batch_to_maturing_for_rework(self) -> None:
        batch_id, _ = self._matured_batch()
        run_id = self._start_run(batch_id)
        self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")
        self.filtration.sample(run_id, 4.5, 0.6, 60.0, 200.0, "tester")
        view = self.filtration.abort_run(run_id, "跑浑无法恢复", "tester")
        self.assertEqual("rework", view["run"]["stage"])
        self.assertEqual("rework", view["certificate"]["verdict"])
        batch = self.brewing.status(batch_id)["batch"]
        self.assertEqual("maturing", batch["stage"])
        self.assertIsNone(batch["filter_run_id"])

        # 返工后可以用同一台过滤机重新开过滤。
        rerun = self._start_run(batch_id)
        self.assertNotEqual(rerun, run_id)

    def test_unit_busy_guard_and_pace_bounds(self) -> None:
        from breweryctl.core.errors import ConflictError, ValidationError

        batch_id, _ = self._matured_batch()
        unit_id = first_filter_unit(self.app)
        self.filtration.start_run(unit_id, batch_id, 1000.0, "tester", 2400.0)

        second_batch, _ = self._matured_batch()
        with self.assertRaises(ConflictError):
            self.filtration.start_run(unit_id, second_batch, 1000.0, "tester", 2400.0)

        run_id = self.filtration.batch_status(batch_id)["run"]["id"]
        self.filtration.confirm_precoat(run_id, 0.6, 0.4, "tester")
        with self.assertRaises(ValidationError):
            self.filtration.set_pace(run_id, 5.0, "tester")


if __name__ == "__main__":
    unittest.main()
