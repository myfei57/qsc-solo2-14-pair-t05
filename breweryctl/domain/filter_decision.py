"""成品过滤控制决策：按浊度与压差决定助剂投加与过滤节奏。

本模块只做纯计算，不读写存储，便于用单元测试把各工况钉死。
状态机与落盘由 :class:`breweryctl.domain.filter.FilterService` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import Settings

# 运行分区，粒度从好到坏。
ZONE_NORMAL = "normal"
ZONE_TURBID = "turbid"          # 浊度偏高，需要加强助剂、降速观察
ZONE_HIGH_DP = "high_dp"        # 压差进入高位，需要降速、考虑反冲洗
ZONE_BREAKTHROUGH = "breakthrough"  # 浊度穿透，必须回流

# 标称过滤节奏（hl/h）与各分区的比例。
NOMINAL_PACE_HL_H = 80.0
PACE_RATIO_TURBID = 0.6
PACE_RATIO_HIGH_DP = 0.5
PACE_RATIO_BREAKTHROUGH = 0.35

# 助剂主体投加基线与各工况修正（g/hl）。
BODY_FEED_BASE_G_HL = 50.0
BODY_FEED_TURBID_G_HL = 40.0
BODY_FEED_HIGH_DP_G_HL = 20.0


@dataclass(frozen=True)
class ReadingDecision:
    """一次采样对应的控制建议。"""

    zone: str
    diverted: bool
    target_pace_hl_h: float
    body_feed_g_hl: float
    actions: list[str] = field(default_factory=list)
    alarm_code: str | None = None
    alarm_severity: str | None = None
    alarm_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "diverted": self.diverted,
            "target_pace_hl_h": self.target_pace_hl_h,
            "body_feed_g_hl": self.body_feed_g_hl,
            "actions": list(self.actions),
            "alarm_code": self.alarm_code,
            "alarm_severity": self.alarm_severity,
            "alarm_message": self.alarm_message,
        }


def classify(
    turbidity_ebc: float,
    dp_bar: float,
    settings: Settings,
    *,
    already_diverting: bool = False,
) -> str:
    """按当前浊度与压差划分运行分区。"""

    if turbidity_ebc >= settings.filter_turbidity_break_ebc:
        return ZONE_BREAKTHROUGH
    if dp_bar >= settings.filter_dp_limit_bar:
        return ZONE_HIGH_DP
    if already_diverting or turbidity_ebc >= settings.filter_turbidity_warn_ebc:
        return ZONE_TURBID
    return ZONE_NORMAL


def body_feed_advice(zone: str, settings: Settings) -> float:
    """给出主体加料强度建议（g/hl），并夹紧在配置区间内。"""

    rate = BODY_FEED_BASE_G_HL
    if zone == ZONE_TURBID:
        rate += BODY_FEED_TURBID_G_HL
    elif zone == ZONE_HIGH_DP:
        rate += BODY_FEED_HIGH_DP_G_HL
    elif zone == ZONE_BREAKTHROUGH:
        rate += BODY_FEED_TURBID_G_HL + BODY_FEED_HIGH_DP_G_HL
    return _clamp(rate, settings.filter_aid_min_g_hl, settings.filter_aid_max_g_hl)


def pace_advice(zone: str, settings: Settings) -> float:
    """给出目标过滤节奏（hl/h），并夹紧在配置区间内。"""

    if zone == ZONE_TURBID:
        ratio = PACE_RATIO_TURBID
    elif zone == ZONE_HIGH_DP:
        ratio = PACE_RATIO_HIGH_DP
    elif zone == ZONE_BREAKTHROUGH:
        ratio = PACE_RATIO_BREAKTHROUGH
    else:
        ratio = 1.0
    target = NOMINAL_PACE_HL_H * ratio
    return round(_clamp(target, settings.filter_pace_min_hl_h, settings.filter_pace_max_hl_h), 2)


def evaluate_reading(
    turbidity_ebc: float,
    dp_bar: float,
    settings: Settings,
    *,
    already_diverting: bool = False,
) -> ReadingDecision:
    """把一次采样转成完整的控制建议（分区、节奏、助剂、动作、告警）。"""

    zone = classify(
        turbidity_ebc,
        dp_bar,
        settings,
        already_diverting=already_diverting,
    )
    diverted = zone == ZONE_BREAKTHROUGH
    target_pace = pace_advice(zone, settings)
    feed = body_feed_advice(zone, settings)
    actions: list[str] = []
    alarm_code: str | None = None
    alarm_severity: str | None = None
    alarm_message: str | None = None

    if zone == ZONE_NORMAL:
        actions.append("保持当前节奏与主体加料")
    elif zone == ZONE_TURBID:
        actions.append(f"降低过滤节奏到 {target_pace:.0f} hl/h")
        actions.append(f"提高主体加料至 {feed:.0f} g/hl，密切观察浊度")
        alarm_code = "filter_turbidity_high"
        alarm_severity = "warning"
        alarm_message = f"过滤浊度 {turbidity_ebc:.2f} EBC 偏高，已降速并加强助剂"
    elif zone == ZONE_HIGH_DP:
        actions.append(f"降低过滤节奏到 {target_pace:.0f} hl/h，减小压差爬升")
        actions.append("准备反冲洗；主体加料按建议调整，避免堵死")
        alarm_code = "filter_dp_high"
        alarm_severity = "warning"
        alarm_message = f"压差 {dp_bar:.2f} bar 已达上限，建议反冲洗并降速"
    else:
        actions.append("立即切换回流，浊度合格前禁止进清酒罐")
        actions.append(f"降低过滤节奏到 {target_pace:.0f} hl/h 并按 {feed:.0f} g/hl 补加助剂")
        alarm_code = "filter_breakthrough"
        alarm_severity = "critical"
        alarm_message = (
            f"浊度 {turbidity_ebc:.2f} EBC 超过穿透阈值，酒液已回流，防止跑浑进罐"
        )

    # 压差预警但尚未到上限，也给一条提示动作。
    if zone != ZONE_HIGH_DP and dp_bar >= settings.filter_dp_warn_bar:
        actions.append(f"压差 {dp_bar:.2f} bar 进入预警区，关注爬升速率")

    return ReadingDecision(
        zone=zone,
        diverted=diverted,
        target_pace_hl_h=target_pace,
        body_feed_g_hl=round(feed, 1),
        actions=actions,
        alarm_code=alarm_code,
        alarm_severity=alarm_severity,
        alarm_message=alarm_message,
    )


def release_ready(
    *,
    stage: str,
    filtered_l: float,
    target_volume_l: float,
    recent_turbidity: list[float],
    recent_dp: list[float],
    good_streak: int,
    settings: Settings,
) -> dict[str, Any]:
    """判定是否满足结束过滤并放行的全部条件。

    需要：处于正常过滤、累计液量达标、最近连续若干次采样浊度与压差都合格。
    """

    from .models import FilterStage

    volume_ok = filtered_l >= target_volume_l * settings.filter_volume_ratio
    need = settings.filter_release_samples
    recent_turbidity = recent_turbidity[-need:]
    recent_dp = recent_dp[-need:]
    samples_ok = (
        len(recent_turbidity) >= need
        and all(value <= settings.filter_turbidity_pass_ebc for value in recent_turbidity)
        and all(value < settings.filter_dp_warn_bar for value in recent_dp)
    )
    streak_ok = good_streak >= need
    stage_ok = stage == FilterStage.FILTERING.value
    ready = stage_ok and volume_ok and samples_ok and streak_ok
    checks = {
        "stage": stage_ok,
        "volume": volume_ok,
        "recent_samples": samples_ok,
        "good_streak": streak_ok,
    }
    missing = [name for name, ok in checks.items() if not ok]
    return {
        "ready": ready,
        "checks": checks,
        "missing": missing,
        "required_samples": need,
        "filtered_l": round(filtered_l, 2),
        "required_volume_l": round(target_volume_l * settings.filter_volume_ratio, 2),
    }


def dose_for_interval(body_feed_g_hl: float, interval_hl: float) -> float:
    """按加料强度与区间液量计算本次投加克数。"""

    # 1 hl = 100 L，body feed 以 g/hl 给出。
    return round(body_feed_g_hl * interval_hl, 1)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
