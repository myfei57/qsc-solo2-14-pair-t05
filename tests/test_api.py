"""控制台 HTTP 接口：路由、错误映射与页面分发。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from .helpers import (
    StepClock,
    create_batch,
    first_filter_unit,
    first_tank,
    make_app,
    mash_to_filter,
    mature_batch,
    sanitize_tank,
    boil_to_cooling,
)


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock())
        self.app.server.start()
        host, port = self.app.server.address
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.app.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.app.server.stop()
        self.thread.join(timeout=5)
        self.app.close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_state_and_pages(self) -> None:
        status, overview = self.call("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("banner", overview)
        status, pages = self.call("GET", "/api/pages")
        self.assertEqual(5, len(pages["pages"]))
        self.assertGreaterEqual(len(pages["routes"]), 60)

    def test_sequence_error_maps_to_conflict(self) -> None:
        batch_id = create_batch(self.app)
        status, payload = self.call(
            "POST", f"/api/batches/{batch_id}/charge", {"grain_kg": 220, "actor": "api"}
        )
        self.assertEqual(409, status)
        self.assertEqual("sequence_violation", payload["error"])

    def test_unknown_route_returns_not_found(self) -> None:
        status, payload = self.call("GET", "/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_static_pages_are_served(self) -> None:
        with urllib.request.urlopen(self.base + "/mash", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("糖化控制", html)
        with urllib.request.urlopen(self.base + "/filter", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("成品过滤", html)
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        self.assertIn("initMashPage", script)
        self.assertIn("initFilterPage", script)

    def _matured_batch_id(self) -> str:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        self.app.registry.brewing.mark_cooled(batch_id, 10.0, "tester")
        free = [item for item in self.app.registry.tanks.list_tanks() if item["stage"] == "idle"][0]
        tank_id = str(free["id"])
        sanitize_tank(self.app, tank_id)
        self.app.registry.brewing.transfer_to_tank(batch_id, tank_id, "tester")
        mature_batch(self.app, batch_id, tank_id)
        return batch_id

    def test_filtration_breakthrough_then_pass_over_http(self) -> None:
        batch_id = self._matured_batch_id()
        unit_id = first_filter_unit(self.app)
        status, payload = self.call(
            "POST",
            "/api/filtration/runs",
            {
                "unit_id": unit_id,
                "batch_id": batch_id,
                "target_volume_l": 1000.0,
                "precoat_g": 2400.0,
                "actor": "api",
            },
        )
        self.assertEqual(200, status, payload)
        run_id = payload["run"]["id"]
        status, payload = self.call(
            "POST",
            f"/api/filtration/runs/{run_id}/precoat",
            {"turbidity_ebc": 0.6, "dp_bar": 0.4, "actor": "api"},
        )
        self.assertEqual(200, status, payload)

        # 跑浑：浊度超过穿透阈值，自动回流。
        status, payload = self.call(
            "POST",
            f"/api/filtration/runs/{run_id}/samples",
            {"turbidity_ebc": 4.0, "dp_bar": 0.5, "flow_hl_h": 60.0, "cumulative_l": 200.0},
        )
        self.assertEqual(200, status, payload)
        self.assertEqual("diverting", payload["run"]["stage"])
        status, payload = self.call("POST", f"/api/filtration/runs/{run_id}/finish", {"actor": "api"})
        self.assertEqual(409, status)

        # 连续 3 次合格采样后恢复进料。
        cumulative = 300.0
        for _ in range(3):
            status, payload = self.call(
                "POST",
                f"/api/filtration/runs/{run_id}/samples",
                {"turbidity_ebc": 0.5, "dp_bar": 0.5, "flow_hl_h": 80.0, "cumulative_l": cumulative},
            )
            cumulative += 100.0
        self.assertEqual("filtering", payload["run"]["stage"])

        # 继续采样满足放行连续次数与液量后结束。
        cumulative = 700.0
        for _ in range(5):
            status, payload = self.call(
                "POST",
                f"/api/filtration/runs/{run_id}/samples",
                {"turbidity_ebc": 0.5, "dp_bar": 0.6, "flow_hl_h": 80.0, "cumulative_l": cumulative},
            )
            cumulative += 80.0
        self.assertTrue(payload["release"]["ready"], payload["release"])
        # 跑浑产生的严重告警需要操作员确认后才能放行。
        status, alarms = self.call("GET", "/api/alarms?status=active&severity=critical")
        breakthrough = next(item for item in alarms["alarms"] if item["code"] == "filter_breakthrough")
        self.call("POST", f"/api/alarms/{breakthrough['id']}/ack", {"operator": "api"})
        status, payload = self.call("POST", f"/api/filtration/runs/{run_id}/finish", {"actor": "api"})
        self.assertEqual(200, status, payload)
        self.assertEqual("pass", payload["certificate"]["verdict"])
        status, cert = self.call("GET", f"/api/filtration/batches/{batch_id}/certificate")
        self.assertTrue(cert["valid"])
