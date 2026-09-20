"""成品过滤控制：投加、节奏、跑浑保护与合格结论。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, InterlockError, NotFoundError, SequenceError

from .helpers import StepClock, make_app


def brewery_id(app) -> str:
    return str(app.registry.tanks.list_tanks()[0]["brewery_id"])


class FiltrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.filtration = self.app.registry.filtration
        self.service = self.app.registry.filter_service
        self.brewery = brewery_id(self.app)

    def tearDown(self) -> None:
        self.app.close()

    def _open_run(self, target_volume_l: float = 1000.0) -> str:
        run = self.service.start_run(self.brewery, "batch-1", target_volume_l, "tester")
        run_id = str(run["id"])
        self.service.ingest(run_id, 0.3, 0.4, 10.0, "tester")
        self.service.confirm_precoat(run_id, "tester")
        return run_id

    def _filter(self, run_id: str, samples: int, turbidity: float, dp: float, flow: float = 10.0) -> dict:
        decision = {}
        for _ in range(samples):
            self.clock.advance(6)
            decision = self.service.ingest(run_id, turbidity, dp, flow, "tester")
        return decision

    def test_precoat_requires_clear_sample(self) -> None:
        run = self.service.start_run(self.brewery, "batch-1", 1000.0, "tester")
        run_id = str(run["id"])
        with self.assertRaises(InterlockError):
            self.service.confirm_precoat(run_id, "tester")
        self.service.ingest(run_id, 2.0, 0.4, 10.0, "tester")
        with self.assertRaises(InterlockError):
            self.service.confirm_precoat(run_id, "tester")
        self.service.ingest(run_id, 0.3, 0.4, 10.0, "tester")
        confirmed = self.service.confirm_precoat(run_id, "tester")
        self.assertEqual("filtering", confirmed["stage"])
        self.assertEqual("forward", confirmed["mode"])

    def test_clean_run_passes_and_accumulates_volume(self) -> None:
        run_id = self._open_run(target_volume_l=1000.0)
        first = self._filter(run_id, 1, 0.4, 1.0)
        self.assertEqual(1000.0, first["filtered_volume_l"])
        decision = self._filter(run_id, 2, 0.4, 1.0)
        self.assertEqual(3000.0, decision["filtered_volume_l"])
        self.assertEqual(80.0, decision["dose_rate_g_m3"])
        self.assertEqual(10.0, decision["flow_setpoint_m3h"])
        result = self.service.finish(run_id, "tester")
        verdict = result["verdict"]
        self.assertTrue(verdict["passed"])
        self.assertEqual([], verdict["reasons"])
        self.assertEqual("complete", result["run"]["stage"])
        self.assertEqual(0.4, verdict["stats"]["avg_turbidity_ntu"])

    def test_hazy_turbidity_raises_dose_and_drops_flow(self) -> None:
        run_id = self._open_run()
        decision = self._filter(run_id, 1, 0.8, 1.0)
        self.assertEqual("hazy", decision["turbidity_band"])
        self.assertEqual(152.0, decision["dose_rate_g_m3"])
        self.assertEqual(2, decision["flow_level"])
        self.assertEqual(8.0, decision["flow_setpoint_m3h"])

    def test_flow_steps_back_up_after_hold(self) -> None:
        run_id = self._open_run()
        decision = self._filter(run_id, 1, 0.8, 1.0)
        self.assertEqual(2, decision["flow_level"])
        decision = self._filter(run_id, 1, 0.4, 1.0)
        self.assertEqual(2, decision["flow_level"])
        self.clock.advance(4)
        decision = self.service.ingest(run_id, 0.4, 1.0, 8.0, "tester")
        self.assertEqual(3, decision["flow_level"])
        self.assertEqual(10.0, decision["flow_setpoint_m3h"])

    def test_breakthrough_recirculates_then_recovers(self) -> None:
        run_id = self._open_run()
        decision = self._filter(run_id, 1, 1.6, 1.0)
        self.assertEqual("breakthrough", decision["turbidity_band"])
        self.assertEqual("recirculating", decision["stage"])
        self.assertEqual("recirculate", decision["mode"])
        self.assertEqual(0, decision["flow_level"])
        self.assertEqual(200.0, decision["dose_rate_g_m3"])
        self.assertEqual(1, decision["breakthrough_events"])
        alarms = self.app.registry.alarms.list_alarms(status="active")
        self.assertTrue(any(item["code"] == "filter_breakthrough" for item in alarms))
        self._filter(run_id, 2, 0.3, 1.0)
        self.assertEqual("recirculating", str(self.filtration.get(run_id)["stage"]))
        decision = self._filter(run_id, 1, 0.3, 1.0)
        self.assertEqual("filtering", decision["stage"])
        self.assertEqual("forward", decision["mode"])
        self.assertEqual(1, decision["flow_level"])

    def test_dp_warn_drops_flow_and_raises_alarm(self) -> None:
        run_id = self._open_run()
        decision = self._filter(run_id, 1, 0.4, 2.5)
        self.assertEqual("high", decision["pressure_band"])
        self.assertEqual(2, decision["flow_level"])
        alarms = self.app.registry.alarms.list_alarms(status="active")
        self.assertTrue(any(item["code"] == "filter_dp_high" for item in alarms))

    def test_dp_limit_auto_stops_and_fails_short_batch(self) -> None:
        run_id = self._open_run(target_volume_l=10000.0)
        decision = self._filter(run_id, 1, 0.4, 3.2)
        self.assertTrue(decision["ended"])
        self.assertEqual("failed", decision["stage"])
        self.assertIsNotNone(decision["verdict"])
        self.assertFalse(decision["verdict"]["passed"])
        self.assertTrue(any("过滤体积不足" in reason for reason in decision["verdict"]["reasons"]))
        with self.assertRaises(SequenceError):
            self.service.ingest(run_id, 0.4, 1.0, 10.0, "tester")

    def test_dp_limit_after_enough_volume_completes(self) -> None:
        run_id = self._open_run(target_volume_l=1000.0)
        self._filter(run_id, 2, 0.4, 1.0)
        decision = self._filter(run_id, 1, 0.4, 3.1)
        self.assertTrue(decision["ended"])
        self.assertEqual("complete", decision["stage"])
        self.assertTrue(decision["verdict"]["passed"])

    def test_hazy_average_fails_verdict(self) -> None:
        run_id = self._open_run(target_volume_l=1000.0)
        self._filter(run_id, 3, 0.8, 1.0)
        result = self.service.finish(run_id, "tester")
        self.assertFalse(result["verdict"]["passed"])
        self.assertTrue(any("平均浊度" in reason for reason in result["verdict"]["reasons"]))
        self.assertEqual("failed", result["run"]["stage"])

    def test_finish_while_recirculating_fails(self) -> None:
        run_id = self._open_run(target_volume_l=1000.0)
        self._filter(run_id, 2, 0.4, 1.0)
        self._filter(run_id, 1, 1.4, 1.0)
        result = self.service.finish(run_id, "tester")
        self.assertFalse(result["verdict"]["passed"])
        self.assertTrue(any("跑浑循环" in reason for reason in result["verdict"]["reasons"]))

    def test_sequence_and_conflict_guards(self) -> None:
        run = self.service.start_run(self.brewery, "batch-1", 1000.0, "tester")
        run_id = str(run["id"])
        with self.assertRaises(SequenceError):
            self.service.finish(run_id, "tester")
        with self.assertRaises(ConflictError):
            self.service.start_run(self.brewery, "batch-1", 1000.0, "tester")
        with self.assertRaises(NotFoundError):
            self.service.ingest("filt-missing", 0.4, 1.0, 10.0, "tester")
        aborted = self.service.abort(run_id, "计划调整", "tester")
        self.assertEqual("aborted", aborted["stage"])
        restarted = self.service.start_run(self.brewery, "batch-1", 1000.0, "tester")
        self.assertEqual("precoat", restarted["stage"])

    def test_api_flow(self) -> None:
        status, payload = self.app.router.handle(
            "POST",
            "/api/filtration/runs",
            {},
            {
                "brewery_id": self.brewery,
                "batch_id": "batch-api",
                "target_volume_l": 1000.0,
                "actor": "api",
            },
        )
        self.assertEqual(200, status)
        run_id = payload["run"]["id"]
        status, payload = self.app.router.handle(
            "POST", f"/api/filtration/runs/{run_id}/sample",
            {},
            {"turbidity_ntu": 0.3, "dp_bar": 0.4, "flow_m3h": 10.0, "actor": "api"},
        )
        self.assertEqual(200, status)
        self.assertEqual("precoat_circulating", payload["actions"][0])
        status, _ = self.app.router.handle(
            "POST", f"/api/filtration/runs/{run_id}/precoat", {}, {"actor": "api"}
        )
        self.assertEqual(200, status)
        self.clock.advance(6)
        status, payload = self.app.router.handle(
            "POST", f"/api/filtration/runs/{run_id}/sample",
            {},
            {"turbidity_ntu": 0.4, "dp_bar": 1.0, "flow_m3h": 10.0, "actor": "api"},
        )
        self.assertEqual(200, status)
        self.assertEqual("clear", payload["turbidity_band"])
        self.clock.advance(6)
        self.app.router.handle(
            "POST", f"/api/filtration/runs/{run_id}/sample",
            {},
            {"turbidity_ntu": 0.4, "dp_bar": 1.0, "flow_m3h": 10.0, "actor": "api"},
        )
        status, payload = self.app.router.handle(
            "POST", f"/api/filtration/runs/{run_id}/finish", {}, {"actor": "api"}
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["verdict"]["passed"])
        status, payload = self.app.router.handle("GET", "/api/filtration/runs", {}, {})
        self.assertEqual(200, status)
        self.assertEqual(1, payload["summary"]["passed"])


if __name__ == "__main__":
    unittest.main()
