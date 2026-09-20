"""成品过滤应用服务：把操作员请求落到过滤领域组件并写审计。"""

from __future__ import annotations

from typing import Any

from ..core.validators import require_number, require_text
from ..domain.audit import AuditLog
from ..domain.filter import FilterService


class FiltrationService:
    """封装过滤机台账、运行控制与合格凭证的服务入口。"""

    def __init__(self, filtration: FilterService, audit: AuditLog) -> None:
        self.filtration = filtration
        self.audit = audit

    def register_unit(
        self,
        brewery_id: str,
        index: int,
        aid_type: str,
        area_m2: float,
        actor: str,
    ) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        unit = self.filtration.register_unit(brewery_id, index, aid_type, area_m2)
        self.audit.record(
            brewery_id,
            None,
            clean_actor,
            "filter.unit_registered",
            {"unit_id": unit["id"], "code": unit["code"], "aid_type": unit["aid_type"]},
        )
        return unit

    def start_run(
        self,
        unit_id: str,
        batch_id: str,
        target_volume_l: float,
        actor: str,
        precoat_g: float,
        pace_hl_h: float | None = None,
    ) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.start_run(
            unit_id,
            batch_id,
            target_volume_l,
            clean_actor,
            precoat_g,
            pace_hl_h=pace_hl_h or 80.0,
        )
        self._audit(view, clean_actor, "filter.run_started", {"unit_id": unit_id, "precoat_g": precoat_g})
        return view

    def confirm_precoat(self, run_id: str, turbidity_ebc: float, dp_bar: float, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.confirm_precoat(run_id, turbidity_ebc, dp_bar, clean_actor)
        self._audit(view, clean_actor, "filter.precoat_confirmed", {"turbidity_ebc": turbidity_ebc})
        return view

    def sample(
        self,
        run_id: str,
        turbidity_ebc: float,
        dp_bar: float,
        flow_hl_h: float,
        cumulative_l: float,
        actor: str = "auto",
    ) -> dict[str, Any]:
        view = self.filtration.sample(
            run_id, turbidity_ebc, dp_bar, flow_hl_h, cumulative_l, operator=actor
        )
        self._audit(view, actor, "filter.sample", {"turbidity_ebc": turbidity_ebc, "dp_bar": dp_bar})
        return view

    def advise_body_feed(self, run_id: str, interval_l: float | None = None) -> dict[str, Any]:
        return self.filtration.advise_body_feed(run_id, interval_l=interval_l)

    def dose_body_aid(
        self,
        run_id: str,
        amount_g: float,
        actor: str,
        basis_g_hl: float | None = None,
    ) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        amount = require_number(amount_g, field="amount_g", minimum=1.0)
        view = self.filtration.dose_body_aid(run_id, amount, clean_actor, basis_g_hl=basis_g_hl)
        self._audit(view, clean_actor, "filter.aid_dosed", {"amount_g": amount, "basis_g_hl": basis_g_hl})
        return view

    def set_pace(self, run_id: str, pace_hl_h: float, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.set_pace(run_id, pace_hl_h, clean_actor)
        self._audit(view, clean_actor, "filter.pace_set", {"pace_hl_h": pace_hl_h})
        return view

    def backwash(self, run_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.backwash(run_id, clean_actor)
        self._audit(view, clean_actor, "filter.backwash", {})
        return view

    def finish_run(self, run_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.finish_run(run_id, clean_actor)
        self._audit(view, clean_actor, "filter.passed", {"certificate_id": view["certificate"]["id"]})
        return view

    def abort_run(self, run_id: str, reason: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        view = self.filtration.abort_run(run_id, clean_actor, reason)
        self._audit(view, clean_actor, "filter.rework", {"reason": reason})
        return view

    def run_status(self, run_id: str) -> dict[str, Any]:
        return self.filtration.status(run_id)

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        return self.filtration.run_for_batch(batch_id)

    def certificate_for_batch(self, batch_id: str) -> dict[str, Any]:
        certificate = self.filtration.certificate_for_batch(batch_id)
        if certificate is None:
            return {"batch_id": batch_id, "valid": False}
        return {"batch_id": batch_id, "valid": certificate["verdict"] == "pass", "certificate": certificate}

    def summary(self) -> dict[str, Any]:
        return self.filtration.summary()

    def _audit(self, view: dict[str, Any], actor: str, action: str, detail: dict[str, Any]) -> None:
        run = view["run"]
        self.audit.record(str(run.get("brewery_id")), str(run.get("batch_id")), actor, action, detail)
