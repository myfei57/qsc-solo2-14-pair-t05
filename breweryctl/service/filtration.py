"""过滤控制应用服务：接收操作请求、落到领域控制环并写审计。"""

from __future__ import annotations

from typing import Any

from ..core.validators import require_text
from ..domain.audit import AuditLog
from ..domain.filtration import FiltrationController


class FiltrationService:
    """把控制台与仪表的过滤请求转成领域动作。"""

    def __init__(self, filtration: FiltrationController, audit: AuditLog) -> None:
        self.filtration = filtration
        self.audit = audit

    def start_run(
        self,
        brewery_id: str,
        batch_id: str,
        target_volume_l: float,
        actor: str,
    ) -> dict[str, Any]:
        """开立一次过滤运行。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.filtration.start_run(brewery_id, batch_id, target_volume_l)
        self.audit.record(
            str(document.get("brewery_id")),
            str(document.get("batch_id")),
            clean_actor,
            "filtration.run_started",
            {"run_id": document.get("id"), "target_volume_l": document.get("target_volume_l")},
        )
        return document

    def confirm_precoat(self, run_id: str, actor: str) -> dict[str, Any]:
        """确认预涂清澈，转前进流。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.filtration.confirm_precoat(run_id)
        self.audit.record(
            str(document.get("brewery_id")),
            str(document.get("batch_id")),
            clean_actor,
            "filtration.precoat_confirmed",
            {"run_id": run_id},
        )
        return document

    def ingest(
        self,
        run_id: str,
        turbidity_ntu: float,
        dp_bar: float,
        flow_m3h: float,
        actor: str,
    ) -> dict[str, Any]:
        """接收一次采样并返回控制指令。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        decision = self.filtration.ingest(run_id, turbidity_ntu, dp_bar, flow_m3h)
        run = self.filtration.get(run_id)
        self.audit.record(
            str(run.get("brewery_id")),
            str(run.get("batch_id")),
            clean_actor,
            "filtration.sample",
            {
                "run_id": run_id,
                "turbidity_ntu": turbidity_ntu,
                "dp_bar": dp_bar,
                "turbidity_band": decision.get("turbidity_band"),
                "pressure_band": decision.get("pressure_band"),
                "dose_rate_g_m3": decision.get("dose_rate_g_m3"),
                "flow_setpoint_m3h": decision.get("flow_setpoint_m3h"),
                "mode": decision.get("mode"),
            },
        )
        return decision

    def finish(self, run_id: str, actor: str) -> dict[str, Any]:
        """结束过滤并给出合格结论。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        result = self.filtration.finish(run_id, clean_actor)
        run = result["run"]
        verdict = result["verdict"]
        self.audit.record(
            str(run.get("brewery_id")),
            str(run.get("batch_id")),
            clean_actor,
            "filtration.finished",
            {
                "run_id": run_id,
                "passed": verdict.get("passed"),
                "reasons": verdict.get("reasons", []),
                "filtered_volume_l": (verdict.get("stats") or {}).get("filtered_volume_l"),
            },
        )
        return result

    def abort(self, run_id: str, reason: str, actor: str) -> dict[str, Any]:
        """人工中止过滤运行。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.filtration.abort(run_id, reason, clean_actor)
        self.audit.record(
            str(document.get("brewery_id")),
            str(document.get("batch_id")),
            clean_actor,
            "filtration.aborted",
            {"run_id": run_id, "reason": reason},
        )
        return document
