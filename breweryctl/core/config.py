"""运行时配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .validators import require_int, require_number, require_text

ENV_PREFIX = "BREWERYCTL_"


@dataclass(frozen=True)
class Settings:
    """平台启动参数与工艺阈值。"""

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = Path("var/breweryctl")
    fsync: bool = True
    max_active_batches: int = 4
    temp_tolerance_c: float = 0.8
    pitch_temp_max_c: float = 12.0
    cip_certificate_ttl_min: int = 240
    pressure_limit_bar: float = 1.8
    hop_window_slack_min: float = 5.0
    filter_turbidity_pass_ebc: float = 0.8
    filter_turbidity_warn_ebc: float = 1.5
    filter_turbidity_break_ebc: float = 3.0
    filter_dp_warn_bar: float = 1.2
    filter_dp_limit_bar: float = 1.8
    filter_release_samples: int = 5
    filter_recover_samples: int = 3
    filter_pace_min_hl_h: float = 20.0
    filter_pace_max_hl_h: float = 120.0
    filter_aid_min_g_hl: float = 40.0
    filter_aid_max_g_hl: float = 200.0
    filter_volume_ratio: float = 0.95
    log_level: str = "INFO"

    def validate(self) -> "Settings":
        """校验配置取值并返回自身，便于启动时链式调用。"""

        require_text(self.host, field="host", max_length=120)
        require_int(self.port, field="port", minimum=0, maximum=65535)
        require_int(self.max_active_batches, field="max_active_batches", minimum=1, maximum=64)
        require_number(self.temp_tolerance_c, field="temp_tolerance_c", minimum=0.05, maximum=10.0)
        require_number(self.pitch_temp_max_c, field="pitch_temp_max_c", minimum=2.0, maximum=30.0)
        require_int(self.cip_certificate_ttl_min, field="cip_certificate_ttl_min", minimum=5, maximum=2880)
        require_number(self.pressure_limit_bar, field="pressure_limit_bar", minimum=0.1, maximum=10.0)
        require_number(self.hop_window_slack_min, field="hop_window_slack_min", minimum=0.0, maximum=60.0)
        require_number(self.filter_turbidity_pass_ebc, field="filter_turbidity_pass_ebc", minimum=0.05, maximum=10.0)
        require_number(self.filter_turbidity_warn_ebc, field="filter_turbidity_warn_ebc", minimum=0.1, maximum=20.0)
        require_number(self.filter_turbidity_break_ebc, field="filter_turbidity_break_ebc", minimum=0.2, maximum=50.0)
        if not (
            self.filter_turbidity_pass_ebc
            < self.filter_turbidity_warn_ebc
            < self.filter_turbidity_break_ebc
        ):
            raise ValidationError("过滤浊度阈值需满足 pass < warn < break")
        require_number(self.filter_dp_warn_bar, field="filter_dp_warn_bar", minimum=0.1, maximum=10.0)
        require_number(self.filter_dp_limit_bar, field="filter_dp_limit_bar", minimum=0.2, maximum=20.0)
        if self.filter_dp_warn_bar >= self.filter_dp_limit_bar:
            raise ValidationError("过滤压差预警值必须低于上限")
        require_int(self.filter_release_samples, field="filter_release_samples", minimum=1, maximum=50)
        require_int(self.filter_recover_samples, field="filter_recover_samples", minimum=1, maximum=50)
        require_number(self.filter_pace_min_hl_h, field="filter_pace_min_hl_h", minimum=1.0, maximum=500.0)
        require_number(self.filter_pace_max_hl_h, field="filter_pace_max_hl_h", minimum=5.0, maximum=1000.0)
        if self.filter_pace_min_hl_h >= self.filter_pace_max_hl_h:
            raise ValidationError("过滤节奏下限必须低于上限")
        require_number(self.filter_aid_min_g_hl, field="filter_aid_min_g_hl", minimum=0.0, maximum=500.0)
        require_number(self.filter_aid_max_g_hl, field="filter_aid_max_g_hl", minimum=10.0, maximum=1000.0)
        if self.filter_aid_min_g_hl >= self.filter_aid_max_g_hl:
            raise ValidationError("助剂投加强度下限必须低于上限")
        require_number(self.filter_volume_ratio, field="filter_volume_ratio", minimum=0.5, maximum=1.0)
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValidationError("log_level 取值不合法", field="log_level", value=self.log_level)
        return self

    def ensure_layout(self) -> dict[str, str]:
        """创建数据目录并返回关键路径。"""

        root = self.data_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(exist_ok=True)
        return {
            "data_dir": str(root),
            "snapshot": str(root / "state.json"),
            "journal": str(root / "journal.jsonl"),
        }

    def with_overrides(self, **overrides: Any) -> "Settings":
        """返回带命令行覆盖值的新配置对象。"""

        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean).validate()

    def describe(self) -> dict[str, Any]:
        """输出可公开的配置摘要。"""

        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "fsync": self.fsync,
            "max_active_batches": self.max_active_batches,
            "temp_tolerance_c": self.temp_tolerance_c,
            "pitch_temp_max_c": self.pitch_temp_max_c,
            "cip_certificate_ttl_min": self.cip_certificate_ttl_min,
            "pressure_limit_bar": self.pressure_limit_bar,
            "hop_window_slack_min": self.hop_window_slack_min,
            "filter_turbidity_pass_ebc": self.filter_turbidity_pass_ebc,
            "filter_turbidity_warn_ebc": self.filter_turbidity_warn_ebc,
            "filter_turbidity_break_ebc": self.filter_turbidity_break_ebc,
            "filter_dp_warn_bar": self.filter_dp_warn_bar,
            "filter_dp_limit_bar": self.filter_dp_limit_bar,
            "filter_release_samples": self.filter_release_samples,
            "filter_recover_samples": self.filter_recover_samples,
            "filter_pace_min_hl_h": self.filter_pace_min_hl_h,
            "filter_pace_max_hl_h": self.filter_pace_max_hl_h,
            "filter_aid_min_g_hl": self.filter_aid_min_g_hl,
            "filter_aid_max_g_hl": self.filter_aid_max_g_hl,
            "filter_volume_ratio": self.filter_volume_ratio,
            "log_level": self.log_level.upper(),
        }

    @classmethod
    def from_env(cls) -> "Settings":
        """从 ``BREWERYCTL_*`` 环境变量读取配置。"""

        base = cls()
        text_keys = ("host", "log_level")
        int_keys = (
            "port",
            "max_active_batches",
            "cip_certificate_ttl_min",
            "filter_release_samples",
            "filter_recover_samples",
        )
        float_keys = (
            "temp_tolerance_c",
            "pitch_temp_max_c",
            "pressure_limit_bar",
            "hop_window_slack_min",
            "filter_turbidity_pass_ebc",
            "filter_turbidity_warn_ebc",
            "filter_turbidity_break_ebc",
            "filter_dp_warn_bar",
            "filter_dp_limit_bar",
            "filter_pace_min_hl_h",
            "filter_pace_max_hl_h",
            "filter_aid_min_g_hl",
            "filter_aid_max_g_hl",
            "filter_volume_ratio",
        )
        values: dict[str, Any] = {}
        for key in text_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = raw
        for key in int_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = int(raw)
        for key in float_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = float(raw)
        data_dir = os.environ.get(ENV_PREFIX + "DATA_DIR")
        if data_dir:
            values["data_dir"] = Path(data_dir)
        fsync = os.environ.get(ENV_PREFIX + "FSYNC")
        if fsync is not None:
            values["fsync"] = fsync.strip().lower() not in {"0", "false", "no"}
        return base.with_overrides(**values)
