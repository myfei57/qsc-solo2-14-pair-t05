"""成品酒过滤控制：按浊度与压差调节助剂投加和过滤节奏。

控制对象是一台硅藻土（或板式）过滤机：

- 预涂阶段循环待清，浊度达标后才允许转前进流；
- 前进流期间按出口浊度自动调整助剂连续投加量，按压差与浊度
  阶梯调整过滤流量；
- 浊度冲破跑浑阈值时自动切入循环回路，连续清澈后再恢复前进流；
- 压差触及上限说明滤饼耗尽，自动收停并对本批给出合格结论。
"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, elapsed_minutes, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, NotFoundError, SequenceError
from ..core.ids import new_id
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import FilterMode, FilterRun, FilterSample, FilterStage

RUNS = "filter_runs"
SAMPLES = "filter_samples"

FLOW_LEVELS = 3
FLOW_HOLD_MINUTES = 10.0
RECOVER_CLEAR_SAMPLES = 3
MAX_BREAKTHROUGHS = 3
VOLUME_TOLERANCE = 0.95

ACTIVE_STAGES = (
    FilterStage.PRECOAT.value,
    FilterStage.FILTERING.value,
    FilterStage.RECIRCULATING.value,
)


class FiltrationController:
    """过滤机控制环：采样进、投加与流量指令出、结束给结论。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.runs = store.collection(RUNS)
        self.samples = store.collection(SAMPLES)

    def start_run(self, brewery_id: str, batch_id: str, target_volume_l: float) -> dict[str, Any]:
        """建立一次过滤运行，进入预涂循环。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        volume = require_number(target_volume_l, field="target_volume_l", minimum=10.0, maximum=500_000.0)
        for item in self.runs.all():
            if item.get("batch_id") == clean_batch and item.get("stage") in ACTIVE_STAGES:
                raise ConflictError("该批次已有进行中的过滤运行", batch_id=clean_batch, run_id=item.get("id"))
        now = format_moment(self.clock.now())
        run = FilterRun(
            id=new_id("filt"),
            batch_id=clean_batch,
            brewery_id=clean_brewery,
            target_volume_l=volume,
            dose_rate_g_m3=self.settings.filter_dose_base_g_m3,
            flow_setpoint_m3h=self.settings.filter_flow_nominal_m3h,
            started_at=now,
            updated_at=now,
        )
        document = self.runs.put(run.id, run.to_doc())
        self.store.append_event(
            "filtration.run_started",
            {"run_id": run.id, "batch_id": clean_batch, "target_volume_l": volume},
        )
        return document

    def confirm_precoat(self, run_id: str) -> dict[str, Any]:
        """预涂循环出水清澈后转前进流；浊度不达标时拒绝。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != FilterStage.PRECOAT.value:
                raise SequenceError(
                    "过滤运行不在预涂阶段",
                    run_id=run_id,
                    stage=document.get("stage"),
                )
            latest = self._latest_sample(run_id)
            target = self.settings.filter_turbidity_target_ntu
            if latest is None or float(latest.get("turbidity_ntu", 999.0)) > target:
                raise InterlockError(
                    "预涂循环尚未清澈，禁止转前进流",
                    run_id=run_id,
                    turbidity_ntu=latest.get("turbidity_ntu") if latest else None,
                    target_ntu=target,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FilterStage.FILTERING.value),
                    ("mode", FilterMode.FORWARD.value),
                    ("last_sample_at", now),
                    ("last_flow_change_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"filter:{run_id}"):
            document = self.runs.update(run_id, mutate)
        self.store.append_event("filtration.precoat_confirmed", {"run_id": run_id})
        return document

    def ingest(
        self,
        run_id: str,
        turbidity_ntu: float,
        dp_bar: float,
        flow_m3h: float,
    ) -> dict[str, Any]:
        """接收一次出口采样，返回助剂投加与流量指令。"""

        turbidity = require_number(turbidity_ntu, field="turbidity_ntu", minimum=0.0, maximum=50.0)
        dp = require_number(dp_bar, field="dp_bar", minimum=0.0, maximum=10.0)
        flow = require_number(flow_m3h, field="flow_m3h", minimum=0.0, maximum=300.0)
        run = self.get(run_id)
        if run.get("stage") not in ACTIVE_STAGES:
            raise SequenceError("过滤运行已结束，无法继续采样", run_id=run_id, stage=run.get("stage"))
        now = format_moment(self.clock.now())

        gained_l, dose_kg = self._throughput(run, flow, now)
        decision = self._decide(run, turbidity, dp, now)

        sample = FilterSample(
            id=new_id("fsmp"),
            run_id=run_id,
            turbidity_ntu=turbidity,
            dp_bar=dp,
            flow_m3h=flow,
            stage=str(decision["stage"]),
            mode=str(decision["mode"]),
            taken_at=now,
        )
        self.samples.put(sample.id, sample.to_doc())

        patch: list[tuple[str, Any]] = [
            ("stage", decision["stage"]),
            ("mode", decision["mode"]),
            ("flow_level", decision["flow_level"]),
            ("dose_rate_g_m3", decision["dose_rate_g_m3"]),
            ("flow_setpoint_m3h", decision["flow_setpoint_m3h"]),
            ("breakthrough_events", decision["breakthrough_events"]),
            ("clear_streak", decision["clear_streak"]),
            ("filtered_volume_l", round(float(run.get("filtered_volume_l", 0.0)) + gained_l, 2)),
            ("dose_total_kg", round(float(run.get("dose_total_kg", 0.0)) + dose_kg, 3)),
            ("samples", int(run.get("samples", 0)) + 1),
            ("last_sample_at", now),
            ("updated_at", now),
        ]
        if run.get("stage") != FilterStage.PRECOAT.value:
            patch.append(("max_turbidity_ntu", max(float(run.get("max_turbidity_ntu", 0.0)), turbidity)))
            patch.append(("max_dp_bar", max(float(run.get("max_dp_bar", 0.0)), dp)))
        if decision["flow_changed"]:
            patch.append(("last_flow_change_at", now))
        if decision.get("ended"):
            patch.extend([("finished_at", now), ("end_reason", decision["end_reason"])])

        with self.store.locks.guard(f"filter:{run_id}"):
            document = self.runs.update(run_id, lambda current: merge_documents(current, patch))
        if decision.get("ended"):
            verdict = self.evaluate(run_id)
            final_stage = FilterStage.COMPLETE.value if verdict["passed"] else FilterStage.FAILED.value
            with self.store.locks.guard(f"filter:{run_id}"):
                document = self.runs.update(
                    run_id,
                    lambda current: merge_documents(
                        current,
                        [("stage", final_stage), ("verdict", verdict), ("updated_at", now)],
                    ),
                )
            self.store.append_event(
                "filtration.auto_stopped",
                {"run_id": run_id, "reason": decision["end_reason"], "passed": verdict["passed"]},
            )
        self._raise_alarms(document, decision, turbidity, dp)
        return self._decision_view(document, decision)

    def finish(self, run_id: str, operator: str) -> dict[str, Any]:
        """结束过滤并给出合格结论。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        run = self.get(run_id)
        if run.get("stage") not in (FilterStage.FILTERING.value, FilterStage.RECIRCULATING.value):
            raise SequenceError(
                "过滤运行不在过滤阶段，无法结束",
                run_id=run_id,
                stage=run.get("stage"),
            )
        now = format_moment(self.clock.now())
        verdict = self.evaluate(run_id)
        stage = FilterStage.COMPLETE.value if verdict["passed"] else FilterStage.FAILED.value

        with self.store.locks.guard(f"filter:{run_id}"):
            document = self.runs.update(
                run_id,
                lambda current: merge_documents(
                    current,
                    [
                        ("stage", stage),
                        ("mode", FilterMode.HOLD.value),
                        ("finished_at", now),
                        ("end_reason", f"operator:{clean_operator}"),
                        ("verdict", verdict),
                        ("updated_at", now),
                    ],
                ),
            )
        self.store.append_event(
            "filtration.finished",
            {"run_id": run_id, "passed": verdict["passed"], "operator": clean_operator},
        )
        return {"run": document, "verdict": verdict}

    def abort(self, run_id: str, reason: str, operator: str) -> dict[str, Any]:
        """人工中止过滤运行。"""

        clean_reason = require_text(reason, field="reason", max_length=200)
        clean_operator = require_text(operator, field="operator", max_length=60)
        run = self.get(run_id)
        if run.get("stage") not in ACTIVE_STAGES:
            raise ConflictError("过滤运行已结束", run_id=run_id, stage=run.get("stage"))
        now = format_moment(self.clock.now())

        with self.store.locks.guard(f"filter:{run_id}"):
            document = self.runs.update(
                run_id,
                lambda current: merge_documents(
                    current,
                    [
                        ("stage", FilterStage.ABORTED.value),
                        ("mode", FilterMode.HOLD.value),
                        ("finished_at", now),
                        ("end_reason", f"aborted:{clean_reason}"),
                        ("updated_at", now),
                    ],
                ),
            )
        self.store.append_event(
            "filtration.aborted",
            {"run_id": run_id, "reason": clean_reason, "operator": clean_operator},
        )
        return document

    def get(self, run_id: str) -> dict[str, Any]:
        """读取过滤运行。"""

        document = self.runs.get(run_id)
        if document is None:
            raise NotFoundError("过滤运行不存在", run_id=run_id)
        return document

    def list_runs(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        """列出过滤运行，最新开始在前。"""

        items = self.runs.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: str(item.get("started_at", "")), reverse=True)

    def samples_for(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """返回某次运行的最近采样。"""

        clean_limit = require_int(limit, field="limit", minimum=1, maximum=1000)
        items = [item for item in self.samples.all() if item.get("run_id") == run_id]
        items.sort(key=lambda item: str(item.get("taken_at", "")))
        return items[-clean_limit:]

    def status(self, run_id: str) -> dict[str, Any]:
        """组合运行状态、当前指令与最近采样。"""

        run = self.get(run_id)
        return {
            "run": run,
            "limits": self._limits(),
            "samples": self.samples_for(run_id, limit=20),
        }

    def evaluate(self, run_id: str) -> dict[str, Any]:
        """按前进流采样与体积目标评估合格结论。"""

        run = self.get(run_id)
        forward = [
            item
            for item in self.samples_for(run_id, limit=1000)
            if item.get("mode") == FilterMode.FORWARD.value
        ]
        settings = self.settings
        reasons: list[str] = []
        values = [float(item.get("turbidity_ntu", 0.0)) for item in forward]
        avg_ntu = sum(values) / len(values) if values else None
        peak_ntu = max(values) if values else None
        filtered = float(run.get("filtered_volume_l", 0.0))
        target_volume = float(run.get("target_volume_l", 0.0))

        if not forward:
            reasons.append("没有前进流过滤采样，无法判定成品浊度")
        if filtered < target_volume * VOLUME_TOLERANCE:
            reasons.append(
                f"过滤体积不足：{filtered:.0f} L 未达到目标 {target_volume:.0f} L 的 {VOLUME_TOLERANCE:.0%}"
            )
        if avg_ntu is not None and avg_ntu > settings.filter_turbidity_target_ntu:
            reasons.append(
                f"平均浊度 {avg_ntu:.2f} NTU 超过目标 {settings.filter_turbidity_target_ntu:.2f} NTU"
            )
        if peak_ntu is not None and peak_ntu > settings.filter_turbidity_max_ntu:
            reasons.append(
                f"瞬时浊度 {peak_ntu:.2f} NTU 超过跑浑阈值 {settings.filter_turbidity_max_ntu:.2f} NTU"
            )
        if run.get("stage") == FilterStage.RECIRCULATING.value:
            reasons.append("结束时仍处于跑浑循环，成品未恢复清澈")
        events = int(run.get("breakthrough_events", 0))
        if events > MAX_BREAKTHROUGHS:
            reasons.append(f"跑浑 {events} 次，超过允许的 {MAX_BREAKTHROUGHS} 次，滤床不稳定")

        return {
            "passed": not reasons,
            "reasons": reasons,
            "stats": {
                "forward_samples": len(forward),
                "avg_turbidity_ntu": round(avg_ntu, 3) if avg_ntu is not None else None,
                "max_turbidity_ntu": round(peak_ntu, 3) if peak_ntu is not None else None,
                "max_dp_bar": round(float(run.get("max_dp_bar", 0.0)), 3),
                "filtered_volume_l": round(filtered, 1),
                "target_volume_l": target_volume,
                "dose_total_kg": round(float(run.get("dose_total_kg", 0.0)), 3),
                "breakthrough_events": events,
            },
            "limits": self._limits(),
            "evaluated_at": format_moment(self.clock.now()),
        }

    def summary(self) -> dict[str, Any]:
        """汇总过滤运行情况。"""

        items = self.runs.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        active = len([item for item in items if item.get("stage") in ACTIVE_STAGES])
        finished = [item for item in items if (item.get("verdict") or {}).get("passed") is not None]
        passed = [item for item in finished if item["verdict"]["passed"]]
        return {
            "runs": len(items),
            "active": active,
            "by_stage": counts,
            "evaluated": len(finished),
            "passed": len(passed),
        }

    def _decide(
        self,
        run: dict[str, Any],
        turbidity: float,
        dp: float,
        now: str,
    ) -> dict[str, Any]:
        """核心控制律：浊度定投加、压差定节奏、跑浑切循环。"""

        settings = self.settings
        stage = str(run.get("stage"))
        mode = str(run.get("mode"))
        level = int(run.get("flow_level", FLOW_LEVELS))
        events = int(run.get("breakthrough_events", 0))
        streak = int(run.get("clear_streak", 0))
        flow_changed = False
        actions: list[str] = []

        if turbidity > settings.filter_turbidity_max_ntu:
            t_band = "breakthrough"
        elif turbidity > settings.filter_turbidity_target_ntu:
            t_band = "hazy"
        else:
            t_band = "clear"

        if dp >= settings.filter_dp_limit_bar:
            p_band = "exhausted"
        elif dp >= settings.filter_dp_warn_bar:
            p_band = "high"
        else:
            p_band = "normal"

        dose = self._dose_for(t_band, turbidity)

        if stage == FilterStage.PRECOAT.value:
            return {
                "stage": stage,
                "mode": FilterMode.RECIRCULATE.value,
                "flow_level": level,
                "dose_rate_g_m3": dose,
                "flow_setpoint_m3h": self._flow_for(level),
                "breakthrough_events": events,
                "clear_streak": 0,
                "flow_changed": False,
                "turbidity_band": t_band,
                "pressure_band": p_band,
                "actions": ["precoat_circulating"],
                "ended": False,
            }

        if p_band == "exhausted":
            return {
                "stage": stage,
                "mode": FilterMode.HOLD.value,
                "flow_level": level,
                "dose_rate_g_m3": dose,
                "flow_setpoint_m3h": 0.0,
                "breakthrough_events": events,
                "clear_streak": 0,
                "flow_changed": False,
                "turbidity_band": t_band,
                "pressure_band": p_band,
                "actions": ["dp_limit_auto_stop"],
                "ended": True,
                "end_reason": "dp_limit",
            }

        if t_band == "breakthrough":
            if mode == FilterMode.FORWARD.value:
                events += 1
                actions.append("breakthrough_recirculate")
            stage = FilterStage.RECIRCULATING.value
            mode = FilterMode.RECIRCULATE.value
            streak = 0
            if level != 0:
                level = 0
                flow_changed = True
        elif stage == FilterStage.RECIRCULATING.value:
            if t_band == "clear":
                streak += 1
                if streak >= RECOVER_CLEAR_SAMPLES:
                    stage = FilterStage.FILTERING.value
                    mode = FilterMode.FORWARD.value
                    streak = 0
                    level = 1
                    flow_changed = True
                    actions.append("recovered_forward")
            else:
                streak = 0
        else:
            drop = (1 if t_band == "hazy" else 0) + (1 if p_band == "high" else 0)
            if drop:
                new_level = max(0, level - drop)
                if new_level != level:
                    level = new_level
                    flow_changed = True
                    actions.append("flow_step_down")
            elif level < FLOW_LEVELS and self._hold_elapsed(run, now):
                level += 1
                flow_changed = True
                actions.append("flow_step_up")

        return {
            "stage": stage,
            "mode": mode,
            "flow_level": level,
            "dose_rate_g_m3": dose,
            "flow_setpoint_m3h": self._flow_for(level),
            "breakthrough_events": events,
            "clear_streak": streak,
            "flow_changed": flow_changed,
            "turbidity_band": t_band,
            "pressure_band": p_band,
            "actions": actions,
            "ended": False,
        }

    def _dose_for(self, t_band: str, turbidity: float) -> float:
        """按浊度档位计算助剂连续投加率。"""

        settings = self.settings
        base = settings.filter_dose_base_g_m3
        ceiling = settings.filter_dose_max_g_m3
        if t_band == "clear":
            return round(base, 1)
        if t_band == "breakthrough":
            return round(ceiling, 1)
        span = settings.filter_turbidity_max_ntu - settings.filter_turbidity_target_ntu
        ratio = min(1.0, (turbidity - settings.filter_turbidity_target_ntu) / span)
        return round(base + (ceiling - base) * ratio, 1)

    def _flow_for(self, level: int) -> float:
        """把流量档位换算成流量设定值。"""

        settings = self.settings
        low = settings.filter_flow_min_m3h
        high = settings.filter_flow_nominal_m3h
        clamped = max(0, min(FLOW_LEVELS, level))
        return round(low + (high - low) * clamped / FLOW_LEVELS, 2)

    def _hold_elapsed(self, run: dict[str, Any], now: str) -> bool:
        """流量升档前要求在当前档位保持足够时间。"""

        changed_at = run.get("last_flow_change_at")
        if not changed_at:
            return True
        return elapsed_minutes(str(changed_at), now) >= FLOW_HOLD_MINUTES

    def _throughput(self, run: dict[str, Any], flow_m3h: float, now: str) -> tuple[float, float]:
        """按上一段间隔的前进流流量累计成品体积与助剂消耗。"""

        if run.get("mode") != FilterMode.FORWARD.value:
            return 0.0, 0.0
        last_at = run.get("last_sample_at")
        if not last_at:
            return 0.0, 0.0
        hours = max(0.0, elapsed_minutes(str(last_at), now) / 60.0)
        volume_l = flow_m3h * hours * 1000.0
        dose_kg = float(run.get("dose_rate_g_m3", 0.0)) * flow_m3h * hours / 1000.0
        return volume_l, dose_kg

    def _raise_alarms(
        self,
        run: dict[str, Any],
        decision: dict[str, Any],
        turbidity: float,
        dp: float,
    ) -> None:
        brewery_id = str(run.get("brewery_id"))
        source = f"filtration:{run.get('id')}"
        if "breakthrough_recirculate" in decision.get("actions", []):
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=source,
                severity="critical",
                code="filter_breakthrough",
                message=(
                    f"浊度 {turbidity:.2f} NTU 冲破跑浑阈值 "
                    f"{self.settings.filter_turbidity_max_ntu:.2f} NTU，已切入循环"
                ),
                latching=True,
                context={"run_id": run.get("id"), "turbidity_ntu": turbidity},
            )
        if "recovered_forward" in decision.get("actions", []):
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=source,
                severity="info",
                code="filter_recovered",
                message="出口浊度恢复清澈，已切回前进流",
                context={"run_id": run.get("id")},
            )
        if decision.get("pressure_band") == "high":
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=source,
                severity="warning",
                code="filter_dp_high",
                message=(
                    f"压差 {dp:.2f} bar 超过告警值 "
                    f"{self.settings.filter_dp_warn_bar:.2f} bar，已降低过滤流量"
                ),
                context={"run_id": run.get("id"), "dp_bar": dp},
            )
        if decision.get("ended") and decision.get("end_reason") == "dp_limit":
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=source,
                severity="warning",
                code="filter_dp_exhausted",
                message=(
                    f"压差 {dp:.2f} bar 触及上限 "
                    f"{self.settings.filter_dp_limit_bar:.2f} bar，滤饼耗尽，过滤自动收停"
                ),
                context={"run_id": run.get("id"), "dp_bar": dp},
            )

    def _decision_view(self, run: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        view = {
            "run_id": run.get("id"),
            "stage": run.get("stage"),
            "mode": run.get("mode"),
            "turbidity_band": decision["turbidity_band"],
            "pressure_band": decision["pressure_band"],
            "dose_rate_g_m3": run.get("dose_rate_g_m3"),
            "flow_setpoint_m3h": run.get("flow_setpoint_m3h"),
            "flow_level": run.get("flow_level"),
            "filtered_volume_l": run.get("filtered_volume_l"),
            "dose_total_kg": run.get("dose_total_kg"),
            "breakthrough_events": run.get("breakthrough_events"),
            "actions": decision.get("actions", []),
            "ended": bool(decision.get("ended")),
            "verdict": run.get("verdict"),
        }
        return view

    def _latest_sample(self, run_id: str) -> dict[str, Any] | None:
        items = self.samples_for(run_id, limit=1)
        return items[0] if items else None

    def _limits(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "turbidity_target_ntu": settings.filter_turbidity_target_ntu,
            "turbidity_max_ntu": settings.filter_turbidity_max_ntu,
            "dp_warn_bar": settings.filter_dp_warn_bar,
            "dp_limit_bar": settings.filter_dp_limit_bar,
            "dose_base_g_m3": settings.filter_dose_base_g_m3,
            "dose_max_g_m3": settings.filter_dose_max_g_m3,
            "flow_nominal_m3h": settings.filter_flow_nominal_m3h,
            "flow_min_m3h": settings.filter_flow_min_m3h,
            "max_breakthroughs": MAX_BREAKTHROUGHS,
            "volume_tolerance": VOLUME_TOLERANCE,
        }
