"""Time-window schedule evaluation for AI moderation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MODE_FULL = "full"
MODE_KEYWORD_ONLY = "keyword_only"
MODE_DISABLED = "disabled"

_RANGE_PATTERN = re.compile(
    r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～至]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$"
)


@dataclass(slots=True)
class ScheduleStatus:
    """Resolved moderation mode for the current timestamp."""

    schedule_enabled: bool
    in_active_window: bool
    mode: str
    summary: str


def parse_time_range(spec: str) -> tuple[int, int] | None:
    """Parse 'HH:MM-HH:MM' into (start_minute, end_minute) in [0, 1440]."""
    match = _RANGE_PATTERN.match(str(spec or ""))
    if not match:
        return None

    sh, sm, eh, em = (int(g) for g in match.groups())
    if not (0 <= sh <= 23 and 0 <= sm <= 59):
        return None
    if not ((0 <= eh <= 23 and 0 <= em <= 59) or (eh == 24 and em == 0)):
        return None

    return sh * 60 + sm, eh * 60 + em


def _is_minute_in_range(minute_of_day: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= minute_of_day < end
    # Cross-midnight range, e.g. 22:00-08:00
    return minute_of_day >= start or minute_of_day < end


def _parse_weekdays(raw_weekdays: Any) -> set[int]:
    allowed: set[int] = set()
    if not isinstance(raw_weekdays, list):
        return allowed
    for item in raw_weekdays:
        try:
            day = int(str(item).strip())
        except ValueError:
            continue
        if 1 <= day <= 7:
            allowed.add(day)
    return allowed


def evaluate_schedule(
    config: dict[str, Any], now: datetime | None = None
) -> ScheduleStatus:
    """Determine whether AI auditing is active right now and the fallback mode."""
    sched = config.get("schedule", {}) or {}
    if not bool(sched.get("enabled", False)):
        return ScheduleStatus(
            schedule_enabled=False,
            in_active_window=True,
            mode=MODE_FULL,
            summary="全天候 AI 审核",
        )

    try:
        tz_offset = int(sched.get("timezone_offset_hours", 8))
    except (TypeError, ValueError):
        tz_offset = 8
    tz_offset = max(-12, min(14, tz_offset))
    tz = timezone(timedelta(hours=tz_offset))

    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    minute_of_day = current.hour * 60 + current.minute

    allowed_weekdays = _parse_weekdays(sched.get("active_weekdays"))
    weekday_ok = not allowed_weekdays or current.isoweekday() in allowed_weekdays

    raw_ranges = sched.get("active_ranges") or []
    parsed_ranges: list[tuple[int, int]] = []
    if isinstance(raw_ranges, list):
        for item in raw_ranges:
            parsed = parse_time_range(str(item))
            if parsed is not None:
                parsed_ranges.append(parsed)

    in_window = weekday_ok and any(
        _is_minute_in_range(minute_of_day, s, e) for s, e in parsed_ranges
    )

    if in_window:
        return ScheduleStatus(
            schedule_enabled=True,
            in_active_window=True,
            mode=MODE_FULL,
            summary=f"分时段审核中（当前 {current.strftime('%H:%M')} 处于 AI 审核时段）",
        )

    off_mode_raw = str(sched.get("off_hours_mode") or "keyword_only").strip()
    if off_mode_raw == "disable_all":
        return ScheduleStatus(
            schedule_enabled=True,
            in_active_window=False,
            mode=MODE_DISABLED,
            summary=f"非审核时段（当前 {current.strftime('%H:%M')}，已彻底关闭审核）",
        )

    return ScheduleStatus(
        schedule_enabled=True,
        in_active_window=False,
        mode=MODE_KEYWORD_ONLY,
        summary=f"非审核时段（当前 {current.strftime('%H:%M')}，仅保留本地关键词屏蔽）",
    )
