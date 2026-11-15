"""Runtime tracking for rate limits, context history, escalation, and statistics."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class ModerationTracker:
    """In-memory state tracker for group moderation."""

    _rate_windows: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _group_contexts: dict[str, deque[str]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=20))
    )
    _user_violations: dict[tuple[str, str], deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "total_checked": 0,
            "total_violated": 0,
            "total_recalled": 0,
            "total_muted": 0,
            "total_errors": 0,
        }
    )
    category_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def allow_audit(self, group_id: str, limit_per_minute: int) -> bool:
        """Check and consume a rate-limit slot for a group."""
        if limit_per_minute <= 0:
            return True
        now = time.monotonic()
        window = self._rate_windows[group_id]
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit_per_minute:
            return False
        window.append(now)
        return True

    def record_recent_message(
        self,
        group_id: str,
        sender_name: str,
        sender_id: str,
        text: str,
        image_count: int = 0,
    ) -> None:
        """Record a group message summary into the bounded recent context buffer."""
        parts: list[str] = []
        clean_text = text.strip().replace("\n", " ")
        if clean_text:
            if len(clean_text) > 150:
                clean_text = clean_text[:150] + "..."
            parts.append(clean_text)
        if image_count > 0:
            parts.append(f"[图片x{image_count}]")
        if not parts:
            return
        label = sender_name or sender_id or "群成员"
        self._group_contexts[group_id].append(f"{label}: {' '.join(parts)}")

    def get_recent_context(self, group_id: str, count: int) -> list[str]:
        """Return up to `count` recent messages for the specified group."""
        if count <= 0:
            return []
        history = self._group_contexts.get(group_id)
        if not history:
            return []
        return list(history)[-count:]

    def record_violation_and_check_escalation(
        self,
        group_id: str,
        user_id: str,
        window_minutes: int,
        threshold_count: int,
    ) -> tuple[int, bool]:
        """Record a user's violation timestamp and evaluate repeat-offender escalation."""
        now = time.monotonic()
        key = (group_id, user_id)
        history = self._user_violations[key]
        cutoff = now - max(1, window_minutes) * 60.0
        while history and history[0] < cutoff:
            history.popleft()
        history.append(now)
        count = len(history)
        return count, count >= max(2, threshold_count)

    def bump_checked(self) -> None:
        self.stats["total_checked"] += 1

    def bump_violated(self, category_key: str) -> None:
        self.stats["total_violated"] += 1
        self.category_counts[category_key] += 1

    def bump_recalled(self) -> None:
        self.stats["total_recalled"] += 1

    def bump_muted(self) -> None:
        self.stats["total_muted"] += 1

    def bump_error(self) -> None:
        self.stats["total_errors"] += 1

