"""Data models and category rule helpers for AI-Inspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTION_RECALL_AND_MUTE = "recall_and_mute"
ACTION_RECALL_ONLY = "recall_only"
ACTION_MUTE_ONLY = "mute_only"
ACTION_WARN_ONLY = "warn_only"

VALID_ACTIONS = {
    ACTION_RECALL_AND_MUTE,
    ACTION_RECALL_ONLY,
    ACTION_MUTE_ONLY,
    ACTION_WARN_ONLY,
}

ACTION_LABELS: dict[str, str] = {
    ACTION_RECALL_AND_MUTE: "撤回并禁言",
    ACTION_RECALL_ONLY: "撤回消息",
    ACTION_MUTE_ONLY: "禁言处理",
    ACTION_WARN_ONLY: "警告提醒",
}

PRESET_CATEGORY_DEFAULTS: dict[str, dict[str, Any]] = {
    "spam_ad": {
        "name": "广告引流 / 刷屏推广",
        "enabled": True,
        "action": ACTION_RECALL_AND_MUTE,
        "mute_minutes": 60,
        "description": "未经允许的商业广告、引流二维码、加群/加微推广、刷屏营销、卖号代练推广等。",
    },
    "nsfw_porn": {
        "name": "色情低俗 / 涉黄信息",
        "enabled": True,
        "action": ACTION_RECALL_AND_MUTE,
        "mute_minutes": 1440,
        "description": "色情、露骨性暗示、低俗擦边图片或文字、涉黄资源传播与引导。",
    },
    "politics_illegal": {
        "name": "涉政敏感 / 违法违规",
        "enabled": True,
        "action": ACTION_RECALL_AND_MUTE,
        "mute_minutes": 1440,
        "description": "涉政敏感言论、暴恐血腥、赌博毒品、黑灰产教学、违法犯罪或危害国家安全的内容。",
    },
    "fraud_phishing": {
        "name": "诈骗钓鱼 / 恶意链接",
        "enabled": True,
        "action": ACTION_RECALL_AND_MUTE,
        "mute_minutes": 1440,
        "description": "电信网络诈骗、刷单兼职骗局、盗号钓鱼链接、假冒官方/管理员诱导转账或索要验证码。",
    },
    "harassment_abuse": {
        "name": "人身攻击 / 辱骂引战",
        "enabled": True,
        "action": ACTION_RECALL_ONLY,
        "mute_minutes": 30,
        "description": "恶意辱骂、人身攻击、仇恨歧视、人肉开盒威胁或恶意引战挑衅（不含朋友间无恶意的正常玩笑）。",
    },
    "spoiler_offtopic": {
        "name": "剧透 / 刷屏水群 / 违规话题",
        "enabled": False,
        "action": ACTION_WARN_ONLY,
        "mute_minutes": 10,
        "description": "无意义的恶意刷屏、高频无意义字符轰炸，或在群规禁止的情况下发布剧透内容。",
    },
}


@dataclass(slots=True)
class CategoryRule:
    """Represents one active moderation violation category."""

    key: str
    name: str
    description: str
    enabled: bool = True
    action: str = ACTION_RECALL_ONLY
    mute_minutes: int = 10


@dataclass(slots=True)
class AuditDecision:
    """Structured result returned by the moderation engine."""

    violated: bool
    category_key: str = "safe"
    category_name: str = "正常消息"
    confidence: float = 0.0
    reason: str = ""
    action: str = ACTION_WARN_ONLY
    mute_minutes: int = 0
    source: str = "llm"
    raw_response: str = ""


@dataclass(slots=True)
class ActionExecutionResult:
    """Outcome of executing recall / mute / escalation actions."""

    recalled: bool = False
    recall_error: str = ""
    muted: bool = False
    mute_error: str = ""
    escalated: bool = False
    violation_count: int = 0
    final_action: str = ACTION_WARN_ONLY
    final_mute_minutes: int = 0


def _normalize_action(raw_action: Any, fallback: str = ACTION_RECALL_ONLY) -> str:
    action = str(raw_action or "").strip()
    return action if action in VALID_ACTIONS else fallback


def _safe_int(value: Any, default: int, minimum: int = 0, maximum: int = 43200) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def build_active_categories(config: dict[str, Any]) -> dict[str, CategoryRule]:
    """Build active violation category rules from preset and custom configs."""
    rules: dict[str, CategoryRule] = {}
    default_mute = _safe_int(
        config.get("moderation_policy", {}).get("default_mute_minutes", 10),
        default=10,
        minimum=1,
    )

    preset_cfg = config.get("categories", {}) or {}
    for key, defaults in PRESET_CATEGORY_DEFAULTS.items():
        item = preset_cfg.get(key, {}) if isinstance(preset_cfg, dict) else {}
        if not isinstance(item, dict):
            item = {}
        enabled = bool(item.get("enabled", defaults["enabled"]))
        if not enabled:
            continue
        rules[key] = CategoryRule(
            key=key,
            name=str(defaults["name"]),
            description=str(item.get("description") or defaults["description"]).strip(),
            enabled=True,
            action=_normalize_action(item.get("action"), defaults["action"]),
            mute_minutes=_safe_int(
                item.get("mute_minutes", defaults["mute_minutes"]),
                default=default_mute,
                minimum=1,
            ),
        )

    custom_list = config.get("custom_categories", []) or []
    if isinstance(custom_list, list):
        for idx, item in enumerate(custom_list, start=1):
            if not isinstance(item, dict):
                continue
            if not bool(item.get("enabled", True)):
                continue
            raw_key = str(item.get("key") or f"custom_rule_{idx}").strip()
            key = raw_key.replace(" ", "_") or f"custom_rule_{idx}"
            name = str(item.get("name") or key).strip()
            desc = str(item.get("description") or name).strip()
            rules[key] = CategoryRule(
                key=key,
                name=name,
                description=desc,
                enabled=True,
                action=_normalize_action(item.get("action"), ACTION_RECALL_AND_MUTE),
                mute_minutes=_safe_int(
                    item.get("mute_minutes", default_mute),
                    default=default_mute,
                    minimum=1,
                ),
            )

    return rules

