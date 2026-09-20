"""成品过滤：过滤机台账、运行状态机、助剂投加与合格判定。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from . import filter_decision as decision
from .alarms import AlarmCenter
from .filter_decision import (
    NOMINAL_PACE_HL_H,
    evaluate_reading,
    release_ready,
)
from .models import (
    BatchStage,
    AidDose,
    FilterCertificate,
    FilterReading,
    FilterRun,
    FilterStage,
    FilterUnit,
    FilterVerdict,
)
UNITS = "filter_units"
RUNS = "filter_runs"
READINGS = "filter_readings"
AID_DOSES = "filter_aid_doses"
CERTIFICATES = "filter_certificates"
BATCHES = "batches"

AID_TYPES = ("diatomaceous_earth", "perlite", "pvpp", "cellulose")
ZONE_PRECOAT = "precoat"


class FilterService:
    """按浊度与压差控制助剂投加与过滤节奏，并给出合格结论。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.units = store.collection(UNITS)
        self.runs = store.collection(RUNS)
        self.readings = store.collection(READINGS)
        self.doses = store.collection(AID_DOSES)
        self.certificates = store.collection(CERTIFICATES)
        self.batches = store.collection(BATCHES)

    # ------------------------------------------------------------------ 台账

    def register_unit(
        self,
        brewery_id: str,
        index: int,
        aid_type: str,
        area_m2: float,
    ) -> dict[str, Any]:
        """登记一台过滤机。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_aid = require_text(aid_type, field="aid_type", max_length=40)
        if clean_aid not in AID_TYPES:
            raise ValidationError("助剂类型不支持", aid_type=clean_aid, allowed=list(AID_TYPES))
        area = require_number(area_m2, field="area_m2", minimum=0.1, maximum=500.0)
        code = f"FL-{int(index):02d}"
        for item in self.units.all():
            if item.get("brewery_id") == clean_brewery and item.get("code") == code:
                raise ConflictError("过滤机编号已存在", brewery_id=clean_brewery, code=code)
        now = format_moment(self.clock.now())
        unit = FilterUnit(
            id=new_id("filter"),
            code=code,
            brewery_id=clean_brewery,
            aid_type=clean_aid,
            area_m2=area,
            updated_at=now,
        )
        return self.units.put(unit.id, unit.to_doc())

    def list_units(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        """列出过滤机。"""

        items = self.units.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: str(item.get("code", "")))

    def get_unit(self, unit_id: str) -> dict[str, Any]:
        document = self.units.get(unit_id)
        if document is None:
            raise NotFoundError("过滤机不存在", unit_id=unit_id)
        return document

    # -------------------------------------------------------------- 运行流程

    def start_run(
        self,
        unit_id: str,
        batch_id: str,
        target_volume_l: float,
        operator: str,
        precoat_g: float,
        pace_hl_h: float = NOMINAL_PACE_HL_H,
    ) -> dict[str, Any]:
        """开始一次过滤：建立预涂层，批次进入过滤阶段。"""

        unit = self.get_unit(unit_id)
        batch = self._require_batch(batch_id)
        clean_operator = require_text(operator, field="operator", max_length=60)
        target = require_number(target_volume_l, field="target_volume_l", minimum=10.0, maximum=200_000.0)
        precoat = require_number(precoat_g, field="precoat_g", minimum=1.0, maximum=1_000_000.0)
        pace = self._require_pace(pace_hl_h)
        if unit.get("active_run_id"):
            raise ConflictError("过滤机已有进行中的过滤运行", unit_id=unit_id, run_id=unit.get("active_run_id"))
        if batch.get("stage") != BatchStage.MATURING.value:
            raise SequenceError(
                "只有成熟后的批次才能开始成品过滤",
                batch_id=batch_id,
                stage=batch.get("stage"),
            )

        now = format_moment(self.clock.now())
        run = FilterRun(
            id=new_id("run"),
            unit_id=unit_id,
            batch_id=batch_id,
            brewery_id=str(batch.get("brewery_id")),
            target_volume_l=target,
            stage=FilterStage.PRECOAT.value,
            precoat_g=precoat,
            pace_hl_h=pace,
            operator=clean_operator,
            started_at=now,
            updated_at=now,
        )
        self.runs.put(run.id, run.to_doc())
        dose = AidDose(
            id=new_id("dose"),
            run_id=run.id,
            batch_id=batch_id,
            phase="precoat",
            aid_type=str(unit.get("aid_type")),
            amount_g=precoat,
            operator=clean_operator,
            dosed_at=now,
        )
        self.doses.put(dose.id, dose.to_doc())

        def mutate_unit(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(document, [("active_run_id", run.id), ("updated_at", now)])

        def mutate_batch(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", BatchStage.FILTERING.value),
                    ("filter_run_id", run.id),
                    ("filter_certificate_id", None),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"unit:{unit_id}"):
            self.units.update(unit_id, mutate_unit)
        with self.store.locks.guard(f"batch:{batch_id}"):
            self.batches.update(batch_id, mutate_batch)
        self.store.append_event(
            "filter.run_started",
            {"run_id": run.id, "unit_id": unit_id, "batch_id": batch_id, "precoat_g": precoat},
        )
        return self.status(run.id)

    def confirm_precoat(
        self,
        run_id: str,
        turbidity_ebc: float,
        dp_bar: float,
        operator: str,
    ) -> dict[str, Any]:
        """确认预涂循环浊度合格，转入正式过滤。"""

        run = self._require_run(run_id)
        clean_operator = require_text(operator, field="operator", max_length=60)
        turbidity = self._require_turbidity(turbidity_ebc)
        dp = self._require_dp(dp_bar)
        self._require_stage(run, FilterStage.PRECOAT.value)
        if turbidity > self.settings.filter_turbidity_pass_ebc:
            raise InterlockError(
                "预涂循环浊度尚未合格，继续循环或补涂",
                run_id=run_id,
                turbidity_ebc=turbidity,
                limit_ebc=self.settings.filter_turbidity_pass_ebc,
            )
        if dp >= self.settings.filter_dp_warn_bar:
            raise InterlockError(
                "预涂循环压差异常，检查涂层均匀性",
                run_id=run_id,
                dp_bar=dp,
                limit_bar=self.settings.filter_dp_warn_bar,
            )
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", FilterStage.FILTERING.value),
                    ("precoat_confirmed_at", now),
                    ("last_turbidity_ebc", turbidity),
                    ("last_dp_bar", dp),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"run:{run_id}"):
            updated = self.runs.update(run_id, mutate)
        self._append_reading(updated, turbidity, dp, updated.get("pace_hl_h"), 0.0, ZONE_PRECOAT, False, now)
        self.store.append_event(
            "filter.precoat_confirmed",
            {"run_id": run_id, "turbidity_ebc": turbidity, "dp_bar": dp, "operator": clean_operator},
        )
        return self.status(run_id)

    def sample(
        self,
        run_id: str,
        turbidity_ebc: float,
        dp_bar: float,
        flow_hl_h: float,
        cumulative_l: float,
        operator: str = "auto",
    ) -> dict[str, Any]:
        """接收一次浊度/压差/流量采样，更新状态机并给出控制建议。"""

        run = self._require_run(run_id)
        turbidity = self._require_turbidity(turbidity_ebc)
        dp = self._require_dp(dp_bar)
        flow = require_number(flow_hl_h, field="flow_hl_h", minimum=0.0, maximum=2000.0)
        cumulative = require_number(cumulative_l, field="cumulative_l", minimum=0.0, maximum=500_000.0)
        if cumulative < float(run.get("filtered_l", 0.0)):
            raise ValidationError(
                "累计过滤量不能回退",
                run_id=run_id,
                cumulative_l=cumulative,
                previous_l=run.get("filtered_l"),
            )
        stage = str(run.get("stage"))
        if stage not in (FilterStage.FILTERING.value, FilterStage.DIVERTING.value):
            raise SequenceError(
                "预涂确认后才能记录正式过滤采样",
                run_id=run_id,
                stage=stage,
            )

        already_diverting = stage == FilterStage.DIVERTING.value
        advice = evaluate_reading(
            turbidity,
            dp,
            self.settings,
            already_diverting=already_diverting,
        )
        new_stage = stage
        good_streak = int(run.get("good_streak", 0))
        divert_streak = int(run.get("divert_streak", 0))
        if advice.diverted:
            new_stage = FilterStage.DIVERTING.value
            divert_streak += 1
            good_streak = 0
        else:
            recovered = (
                turbidity <= self.settings.filter_turbidity_pass_ebc
                and dp < self.settings.filter_dp_warn_bar
            )
            good_streak = good_streak + 1 if recovered else 0
            if already_diverting and good_streak >= self.settings.filter_recover_samples:
                new_stage = FilterStage.FILTERING.value
                divert_streak = 0

        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", new_stage),
                    ("filtered_l", cumulative),
                    ("last_turbidity_ebc", turbidity),
                    ("last_dp_bar", dp),
                    ("last_flow_hl_h", flow),
                    ("good_streak", good_streak),
                    ("divert_streak", divert_streak),
                    ("reading_count", int(document.get("reading_count", 0)) + 1),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"run:{run_id}"):
            updated = self.runs.update(run_id, mutate)
        self._append_reading(updated, turbidity, dp, flow, cumulative, advice.zone, advice.diverted, now)

        if advice.alarm_code:
            self.alarms.raise_alarm(
                brewery_id=str(run.get("brewery_id")),
                source=f"filter:{run_id}",
                severity=str(advice.alarm_severity),
                code=str(advice.alarm_code),
                message=str(advice.alarm_message),
                latching=advice.zone == decision.ZONE_BREAKTHROUGH,
                context={
                    "run_id": run_id,
                    "batch_id": run.get("batch_id"),
                    "turbidity_ebc": turbidity,
                    "dp_bar": dp,
                    "zone": advice.zone,
                    "operator": operator,
                },
            )
        if already_diverting and new_stage == FilterStage.FILTERING.value:
            self.alarms.raise_alarm(
                brewery_id=str(run.get("brewery_id")),
                source=f"filter:{run_id}",
                severity="info",
                code="filter_recovered",
                message=f"连续 {self.settings.filter_recover_samples} 次采样合格，过滤恢复正常进料",
                context={"run_id": run_id, "batch_id": run.get("batch_id")},
            )
        return self.status(run_id, advice=advice.as_dict())

    def advise_body_feed(self, run_id: str, interval_l: float | None = None) -> dict[str, Any]:
        """根据当前分区给出主体加料强度与本段液量对应的投加克数。"""

        run = self._require_run(run_id)
        self._require_active(run)
        turbidity = run.get("last_turbidity_ebc")
        dp = run.get("last_dp_bar")
        if turbidity is None or dp is None:
            raise SequenceError("尚无采样数据，无法给出助剂建议", run_id=run_id)
        zone = decision.classify(
            float(turbidity),
            float(dp),
            self.settings,
            already_diverting=run.get("stage") == FilterStage.DIVERTING.value,
        )
        rate = decision.body_feed_advice(zone, self.settings)
        if interval_l is None:
            interval_l = max(float(run.get("filtered_l", 0.0)) - float(run.get("last_body_dose_l", 0.0)), 0.0)
        interval_hl = round(
            require_number(interval_l, field="interval_l", minimum=0.0, maximum=500_000.0) / 100.0,
            3,
        )
        return {
            "run_id": run_id,
            "zone": zone,
            "body_feed_g_hl": rate,
            "interval_l": round(interval_l, 2),
            "dose_g": decision.dose_for_interval(rate, interval_hl),
        }

    def dose_body_aid(
        self,
        run_id: str,
        amount_g: float,
        operator: str,
        basis_g_hl: float | None = None,
    ) -> dict[str, Any]:
        """记录一次主体助剂实际投加。"""

        run = self._require_run(run_id)
        self._require_active(run)
        clean_operator = require_text(operator, field="operator", max_length=60)
        amount = require_number(amount_g, field="amount_g", minimum=1.0, maximum=1_000_000.0)
        basis = None
        if basis_g_hl is not None:
            basis = require_number(
                basis_g_hl,
                field="basis_g_hl",
                minimum=self.settings.filter_aid_min_g_hl,
                maximum=self.settings.filter_aid_max_g_hl,
            )
        now = format_moment(self.clock.now())
        unit = self.get_unit(str(run.get("unit_id")))
        dose = AidDose(
            id=new_id("dose"),
            run_id=run_id,
            batch_id=str(run.get("batch_id")),
            phase="body",
            aid_type=str(unit.get("aid_type")),
            amount_g=amount,
            basis_g_hl=basis,
            operator=clean_operator,
            dosed_at=now,
        )
        self.doses.put(dose.id, dose.to_doc())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("body_aid_g", float(document.get("body_aid_g", 0.0)) + amount),
                    ("last_body_dose_l", float(document.get("filtered_l", 0.0))),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"run:{run_id}"):
            updated = self.runs.update(run_id, mutate)
        self.store.append_event(
            "filter.aid_dosed",
            {"run_id": run_id, "amount_g": amount, "basis_g_hl": basis, "operator": clean_operator},
        )
        return self.status(run_id, last_dose=dose.to_doc())

    def set_pace(self, run_id: str, pace_hl_h: float, operator: str) -> dict[str, Any]:
        """人工设定过滤节奏（hl/h）。"""

        run = self._require_run(run_id)
        self._require_active(run)
        clean_operator = require_text(operator, field="operator", max_length=60)
        pace = self._require_pace(pace_hl_h)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(document, [("pace_hl_h", pace), ("updated_at", now)])

        with self.store.locks.guard(f"run:{run_id}"):
            self.runs.update(run_id, mutate)
        self.store.append_event(
            "filter.pace_set",
            {"run_id": run_id, "pace_hl_h": pace, "operator": clean_operator},
        )
        return self.status(run_id)

    def backwash(self, run_id: str, operator: str) -> dict[str, Any]:
        """记录一次反冲洗（压差到顶时的现场处置）。"""

        run = self._require_run(run_id)
        self._require_active(run)
        clean_operator = require_text(operator, field="operator", max_length=60)
        self.store.append_event(
            "filter.backwash",
            {"run_id": run_id, "batch_id": run.get("batch_id"), "operator": clean_operator},
        )
        self.alarms.raise_alarm(
            brewery_id=str(run.get("brewery_id")),
            source=f"filter:{run_id}",
            severity="info",
            code="filter_backwash",
            message=f"{clean_operator} 执行了过滤机反冲洗",
            context={"run_id": run_id, "batch_id": run.get("batch_id")},
        )
        return self.status(run_id)

    def finish_run(self, run_id: str, operator: str) -> dict[str, Any]:
        """结束过滤：满足放行条件则签发合格凭证，否则拒绝并列出缺项。"""

        run = self._require_run(run_id)
        clean_operator = require_text(operator, field="operator", max_length=60)
        self._require_active(run)
        recent = self._readings_for(run_id, limit=self.settings.filter_release_samples)
        report = release_ready(
            stage=str(run.get("stage")),
            filtered_l=float(run.get("filtered_l", 0.0)),
            target_volume_l=float(run.get("target_volume_l", 0.0)),
            recent_turbidity=[float(item["turbidity_ebc"]) for item in recent],
            recent_dp=[float(item["dp_bar"]) for item in recent],
            good_streak=int(run.get("good_streak", 0)),
            settings=self.settings,
        )
        if not report["ready"]:
            raise InterlockError(
                "过滤尚不满足放行条件",
                run_id=run_id,
                missing=report["missing"],
                checks=report["checks"],
            )
        self._require_no_critical_alarm(run_id)

        now = format_moment(self.clock.now())
        certificate = FilterCertificate(
            id=new_id("fcert"),
            run_id=run_id,
            batch_id=str(run.get("batch_id")),
            unit_id=str(run.get("unit_id")),
            verdict=FilterVerdict.PASS.value,
            final_turbidity_ebc=float(run.get("last_turbidity_ebc", 0.0)),
            final_dp_bar=float(run.get("last_dp_bar", 0.0)),
            filtered_l=float(run.get("filtered_l", 0.0)),
            target_volume_l=float(run.get("target_volume_l", 0.0)),
            body_aid_g=float(run.get("body_aid_g", 0.0)),
            reading_count=int(run.get("reading_count", 0)),
            released_streak=int(run.get("good_streak", 0)),
            issued_at=now,
            operator=clean_operator,
            reason="连续采样浊度与压差合格，液量达标",
        )
        self.certificates.put(certificate.id, certificate.to_doc())

        def mutate_run(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", FilterStage.PASSED.value),
                    ("verdict", FilterVerdict.PASS.value),
                    ("certificate_id", certificate.id),
                    ("ended_reason", certificate.reason),
                    ("ended_at", now),
                    ("updated_at", now),
                ],
            )

        def mutate_unit(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(document, [("active_run_id", None), ("updated_at", now)])

        def mutate_batch(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("filter_certificate_id", certificate.id), ("updated_at", now)]
            )

        with self.store.locks.guard(f"run:{run_id}"):
            updated = self.runs.update(run_id, mutate_run)
        with self.store.locks.guard(f"unit:{run.get('unit_id')}"):
            self.units.update(str(run.get("unit_id")), mutate_unit)
        with self.store.locks.guard(f"batch:{run.get('batch_id')}"):
            self.batches.update(str(run.get("batch_id")), mutate_batch)
        self.alarms.raise_alarm(
            brewery_id=str(run.get("brewery_id")),
            source=f"filter:{run_id}",
            severity="info",
            code="filter_passed",
            message=f"批次 {run.get('batch_id')} 过滤合格，凭证 {certificate.id}",
            context={"run_id": run_id, "certificate_id": certificate.id},
        )
        return self.status(run_id, release=report, certificate=certificate.to_doc())

    def abort_run(self, run_id: str, operator: str, reason: str) -> dict[str, Any]:
        """判返工：结束运行、签发返工结论，批次退回成熟等待重新过滤。"""

        run = self._require_run(run_id)
        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_reason = require_text(reason, field="reason", max_length=200)
        self._require_active(run)
        now = format_moment(self.clock.now())
        certificate = FilterCertificate(
            id=new_id("fcert"),
            run_id=run_id,
            batch_id=str(run.get("batch_id")),
            unit_id=str(run.get("unit_id")),
            verdict=FilterVerdict.REWORK.value,
            final_turbidity_ebc=float(run.get("last_turbidity_ebc") or 0.0),
            final_dp_bar=float(run.get("last_dp_bar") or 0.0),
            filtered_l=float(run.get("filtered_l", 0.0)),
            target_volume_l=float(run.get("target_volume_l", 0.0)),
            body_aid_g=float(run.get("body_aid_g", 0.0)),
            reading_count=int(run.get("reading_count", 0)),
            released_streak=int(run.get("good_streak", 0)),
            issued_at=now,
            operator=clean_operator,
            reason=clean_reason,
        )
        self.certificates.put(certificate.id, certificate.to_doc())

        def mutate_run(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", FilterStage.REWORK.value),
                    ("verdict", FilterVerdict.REWORK.value),
                    ("certificate_id", certificate.id),
                    ("ended_reason", clean_reason),
                    ("ended_at", now),
                    ("updated_at", now),
                ],
            )

        def mutate_unit(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(document, [("active_run_id", None), ("updated_at", now)])

        def mutate_batch(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", BatchStage.MATURING.value),
                    ("filter_run_id", None),
                    ("filter_certificate_id", None),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"run:{run_id}"):
            self.runs.update(run_id, mutate_run)
        with self.store.locks.guard(f"unit:{run.get('unit_id')}"):
            self.units.update(str(run.get("unit_id")), mutate_unit)
        with self.store.locks.guard(f"batch:{run.get('batch_id')}"):
            self.batches.update(str(run.get("batch_id")), mutate_batch)
        self.alarms.raise_alarm(
            brewery_id=str(run.get("brewery_id")),
            source=f"filter:{run_id}",
            severity="critical",
            code="filter_rework",
            message=f"批次 {run.get('batch_id')} 过滤判返工：{clean_reason}",
            context={"run_id": run_id, "certificate_id": certificate.id, "reason": clean_reason},
        )
        return self.status(run_id, certificate=certificate.to_doc())

    # ------------------------------------------------------------------ 查询

    def status(self, run_id: str, **extra: Any) -> dict[str, Any]:
        """返回运行、采样、投加与放行判定的组合视图。"""

        run = self._require_run(run_id)
        readings = self._readings_for(run_id, limit=self.settings.filter_release_samples)
        report = release_ready(
            stage=str(run.get("stage")),
            filtered_l=float(run.get("filtered_l", 0.0)),
            target_volume_l=float(run.get("target_volume_l", 0.0)),
            recent_turbidity=[float(item["turbidity_ebc"]) for item in readings],
            recent_dp=[float(item["dp_bar"]) for item in readings],
            good_streak=int(run.get("good_streak", 0)),
            settings=self.settings,
        )
        view: dict[str, Any] = {
            "run": run,
            "unit": self.get_unit(str(run.get("unit_id"))),
            "readings": readings,
            "doses": [item for item in self.doses.all() if item.get("run_id") == run_id],
            "release": report,
            "certificate": self.certificates.get(str(run["certificate_id"])) if run.get("certificate_id") else None,
        }
        view.update(extra)
        return view

    def run_for_batch(self, batch_id: str) -> dict[str, Any]:
        """返回批次当前（或最近一次）过滤运行视图。"""

        items = [item for item in self.runs.all() if item.get("batch_id") == batch_id]
        if not items:
            raise NotFoundError("批次尚无过滤运行", batch_id=batch_id)
        items.sort(key=lambda item: str(item.get("started_at", "")))
        return self.status(str(items[-1]["id"]))

    def active_runs(self) -> list[dict[str, Any]]:
        """返回进行中的过滤运行。"""

        active = {FilterStage.PRECOAT.value, FilterStage.FILTERING.value, FilterStage.DIVERTING.value}
        return [item for item in self.runs.all() if item.get("stage") in active]

    def certificate_for_batch(self, batch_id: str) -> dict[str, Any] | None:
        items = [item for item in self.certificates.all() if item.get("batch_id") == batch_id]
        if not items:
            return None
        items.sort(key=lambda item: str(item.get("issued_at", "")))
        return items[-1]

    def summary(self) -> dict[str, Any]:
        runs = self.runs.all()
        counts: dict[str, int] = {}
        for item in runs:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "units": self.units.count(),
            "runs": len(runs),
            "by_stage": counts,
            "active_runs": len(self.active_runs()),
            "readings": self.readings.count(),
            "certificates": self.certificates.count(),
        }

    # ------------------------------------------------------------------ 内部

    def _require_run(self, run_id: str) -> dict[str, Any]:
        document = self.runs.get(run_id)
        if document is None:
            raise NotFoundError("过滤运行不存在", run_id=run_id)
        return document

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        document = self.batches.get(batch_id)
        if document is None:
            raise NotFoundError("批次不存在", batch_id=batch_id)
        return document

    @staticmethod
    def _require_stage(run: dict[str, Any], stage: str) -> None:
        if run.get("stage") != stage:
            raise SequenceError(
                "过滤运行当前阶段不允许该操作",
                run_id=run.get("id"),
                stage=run.get("stage"),
                required=stage,
            )

    @staticmethod
    def _require_active(run: dict[str, Any]) -> None:
        if run.get("stage") not in (FilterStage.FILTERING.value, FilterStage.DIVERTING.value):
            raise SequenceError("过滤运行不在进行中", run_id=run.get("id"), stage=run.get("stage"))

    def _require_pace(self, pace_hl_h: float) -> float:
        pace = require_number(
            pace_hl_h,
            field="pace_hl_h",
            minimum=self.settings.filter_pace_min_hl_h,
            maximum=self.settings.filter_pace_max_hl_h,
        )
        return round(pace, 2)

    def _require_turbidity(self, value: float) -> float:
        return require_number(value, field="turbidity_ebc", minimum=0.0, maximum=200.0)

    def _require_dp(self, value: float) -> float:
        return require_number(value, field="dp_bar", minimum=0.0, maximum=20.0)

    def _readings_for(self, run_id: str, limit: int) -> list[dict[str, Any]]:
        items = [item for item in self.readings.all() if item.get("run_id") == run_id]
        items.sort(key=lambda item: str(item.get("taken_at", "")))
        return items[-limit:]

    def _append_reading(
        self,
        run: dict[str, Any],
        turbidity: float,
        dp: float,
        flow: Any,
        cumulative: float,
        zone: str,
        diverted: bool,
        now: str,
    ) -> None:
        reading = FilterReading(
            id=new_id("fread"),
            run_id=str(run["id"]),
            batch_id=str(run.get("batch_id")),
            turbidity_ebc=turbidity,
            dp_bar=dp,
            flow_hl_h=float(flow or 0.0),
            cumulative_l=cumulative,
            stage=str(run.get("stage")),
            zone=zone,
            diverted=diverted,
            taken_at=now,
        )
        self.readings.put(reading.id, reading.to_doc())

    def _require_no_critical_alarm(self, run_id: str) -> None:
        active = self.alarms.list_alarms(status="active", severity="critical")
        blocking = [item for item in active if item.get("source") == f"filter:{run_id}"]
        if blocking:
            raise InterlockError(
                "存在未处理的过滤严重告警，请先确认并解除（如跑浑回流）",
                run_id=run_id,
                alarms=[str(item.get("code")) for item in blocking],
            )
