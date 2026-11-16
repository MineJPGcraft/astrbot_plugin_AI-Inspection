"""AstrBot plugin: AI group message moderation for QQ Official Bot."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

from .core import (
    ACTION_LABELS,
    ActionExecutionResult,
    AuditDecision,
    CategoryRule,
    MODE_DISABLED,
    MODE_FULL,
    MODE_KEYWORD_ONLY,
    MessageModerator,
    ModerationTracker,
    QQOfficialClient,
    build_active_categories,
    evaluate_schedule,
    extract_group_openid,
    extract_member_openid,
    extract_member_role,
    extract_message_id,
)
from .core.models import (
    ACTION_MUTE_ONLY,
    ACTION_RECALL_AND_MUTE,
    ACTION_RECALL_ONLY,
    ACTION_WARN_ONLY,
)


class AIInspectionPlugin(Star):
    """AI 群消息审核插件（适配 QQ 官方机器人）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}
        self.moderator = MessageModerator()
        self.tracker = ModerationTracker()
        self.qq_client = QQOfficialClient()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def terminate(self) -> None:
        """Clean up background tasks and HTTP sessions on plugin unload."""
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        await self.qq_client.close()

    def _is_debug(self) -> bool:
        return bool((self.config.get("qq_official_api", {}) or {}).get("debug_log", False))

    def _get_platform(self, event: AstrMessageEvent) -> Any:
        try:
            platform_id = event.get_platform_id()
            if platform_id:
                return self.context.get_platform_inst(platform_id)
        except Exception:
            pass
        return None

    def _is_group_monitored(self, event: AstrMessageEvent, group_openid: str) -> bool:
        scope = self.config.get("scope", {}) or {}
        ignored = {str(x).strip() for x in (scope.get("ignored_group_ids") or []) if str(x).strip()}
        if group_openid and group_openid in ignored:
            return False

        monitored_umos = {
            str(x).strip() for x in (scope.get("monitored_umos") or []) if str(x).strip()
        }
        if event.unified_msg_origin in monitored_umos:
            return True

        monitored_ids = {
            str(x).strip() for x in (scope.get("monitored_group_ids") or []) if str(x).strip()
        }
        if group_openid and group_openid in monitored_ids:
            return True

        return bool(scope.get("audit_all_groups", False))

    def _is_sender_blacklisted(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        wf = self.config.get("whitelist_and_filter", {}) or {}
        blacklist = {
            str(x).strip()
            for x in (wf.get("blacklist_users") or [])
            if str(x).strip()
        }
        return sender_id in blacklist

    def _is_sender_exempt(self, event: AstrMessageEvent, sender_id: str) -> bool:
        wf = self.config.get("whitelist_and_filter", {}) or {}
        self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        if self_id and sender_id == self_id:
            return True

        if bool(wf.get("exempt_astrbot_admins", True)) and event.is_admin():
            return True

        if bool(wf.get("exempt_group_admins", True)):
            role = extract_member_role(event)
            if role in ("owner", "admin"):
                return True

        whitelist = {str(x).strip() for x in (wf.get("whitelist_users") or []) if str(x).strip()}
        if sender_id and sender_id in whitelist:
            return True

        return False

    async def _extract_message_payload(
        self, event: AstrMessageEvent, audit_image: bool
    ) -> tuple[str, list[str], bool]:
        """Extract text, image URLs/paths, and whether message is pure emoji/face."""
        text_parts: list[str] = []
        image_urls: list[str] = []
        has_face = False
        has_other = False

        face_cls = getattr(Comp, "Face", None)
        chain = event.get_messages() or []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                if comp.text:
                    text_parts.append(comp.text)
            elif isinstance(comp, Comp.At):
                target = getattr(comp, "name", "") or getattr(comp, "qq", "")
                if target:
                    text_parts.append(f"@{target}")
            elif isinstance(comp, Comp.Image):
                if audit_image:
                    img_ref = await self._resolve_image_ref(comp)
                    if img_ref:
                        image_urls.append(img_ref)
            elif face_cls is not None and isinstance(comp, face_cls):
                has_face = True
            else:
                has_other = True

        text = " ".join(part.strip() for part in text_parts if part.strip()).strip()
        if not text and event.message_str:
            text = event.message_str.strip()

        is_pure_face = has_face and not text and not image_urls and not has_other
        return text, image_urls, is_pure_face

    @staticmethod
    async def _resolve_image_ref(comp: Comp.Image) -> str:
        url = str(getattr(comp, "url", "") or "").strip()
        if url.startswith(("http://", "https://")):
            return url
        file_attr = str(getattr(comp, "file", "") or "").strip()
        if file_attr.startswith(("http://", "https://")):
            return file_attr
        if hasattr(comp, "convert_to_file_path"):
            try:
                path = await comp.convert_to_file_path()
                if path:
                    return str(path)
            except Exception as exc:
                logger.debug(f"[AI-Inspection] 图片路径转换失败: {exc}")
        return file_attr

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """Listen to QQ Official group messages and run AI moderation."""
        if not bool(self.config.get("enabled", True)):
            return

        group_openid = extract_group_openid(event)
        if not group_openid or not self._is_group_monitored(event, group_openid):
            return

        raw_text = (event.message_str or "").strip()
        # Always allow /audit commands through to the command handler
        if raw_text.startswith("/audit"):
            return

        sender_id = extract_member_openid(event)
        self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        if self_id and sender_id == self_id:
            return

        is_blacklisted = self._is_sender_blacklisted(sender_id)
        wf = self.config.get("whitelist_and_filter", {}) or {}
        if not is_blacklisted and bool(wf.get("skip_commands", True)) and raw_text.startswith("/"):
            return

        sched = evaluate_schedule(self.config)
        effective_mode = MODE_FULL if is_blacklisted else sched.mode
        if effective_mode == MODE_DISABLED:
            if self._is_debug():
                logger.info(f"[AI-Inspection] {sched.summary}，跳过群 {group_openid} 消息")
            return

        sender_name = event.get_sender_name() or sender_id
        llm_cfg = self.config.get("llm", {}) or {}
        audit_image = bool(llm_cfg.get("audit_image", True)) and effective_mode != MODE_KEYWORD_ONLY

        text, image_urls, is_pure_face = await self._extract_message_payload(
            event, audit_image=audit_image
        )

        # Record context even for exempt users so recent group chat context stays coherent
        context_count = max(0, int(llm_cfg.get("audit_context_count", 3)))
        recent_context = self.tracker.get_recent_context(group_openid, context_count)
        self.tracker.record_recent_message(
            group_id=group_openid,
            sender_name=sender_name,
            sender_id=sender_id,
            text=text,
            image_count=len(image_urls),
        )

        if is_blacklisted:
            if not text.strip() and not image_urls:
                return
            if sched.mode != MODE_FULL and self._is_debug():
                logger.info(
                    f"[AI-Inspection] 命中重点监控黑名单用户 {sender_name}({sender_id})，"
                    "忽略时段限制强制执行完整审核"
                )
        else:
            if self._is_sender_exempt(event, sender_id):
                return

            if is_pure_face and bool(wf.get("ignore_pure_emoji_face", False)):
                return

            min_len = max(0, int(wf.get("min_text_length", 1)))
            if not image_urls and len(text.strip()) < min_len:
                return

            if self.moderator.check_keyword_allowlist(
                text, wf.get("keyword_allowlist") or []
            ):
                return

        categories = build_active_categories(self.config)

        # Local fast-path blocklist check (zero LLM cost)
        matched_kw = self.moderator.check_keyword_blocklist(
            text, wf.get("keyword_blocklist") or []
        )
        if matched_kw is not None:
            default_mute = max(
                1,
                int(
                    (self.config.get("moderation_policy", {}) or {}).get(
                        "default_mute_minutes", 10
                    )
                ),
            )
            default_rule = (
                categories.get("spam_ad")
                or (next(iter(categories.values())) if categories else None)
                or CategoryRule(
                    key="keyword_blocklist",
                    name="敏感词违规",
                    description="命中本地敏感词",
                    action=ACTION_RECALL_ONLY,
                    mute_minutes=default_mute,
                )
            )
            decision = AuditDecision(
                violated=True,
                category_key=default_rule.key,
                category_name=default_rule.name,
                confidence=1.0,
                #reason=f"命中本地敏感词规则 ({matched_kw})",
                reason=f"命中本地敏感词规则",
                action=default_rule.action,
                mute_minutes=default_rule.mute_minutes,
                source="keyword",
            )
            await self._handle_violation(
                event=event,
                group_openid=group_openid,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                image_count=len(image_urls),
                decision=decision,
            )
            return

        if effective_mode == MODE_KEYWORD_ONLY:
            if self._is_debug():
                logger.info(f"[AI-Inspection] {sched.summary}，跳过 AI 审核")
            return

        if not categories:
            return

        rate_limit = max(0, int(wf.get("rate_limit_per_group_per_minute", 60)))
        if not self.tracker.allow_audit(group_openid, rate_limit):
            if self._is_debug():
                logger.info(
                    f"[AI-Inspection] 群 {group_openid} 触发每分钟审核限流 ({rate_limit})，跳过本条消息"
                )
            return

        self.tracker.bump_checked()
        try:
            decision = await self.moderator.audit_message(
                context=self.context,
                umo=event.unified_msg_origin,
                text=text,
                image_urls=image_urls,
                sender_name=sender_name,
                sender_id=sender_id,
                context_lines=recent_context,
                categories=categories,
                config=self.config,
                event=event,
            )
        except Exception as exc:
            self.tracker.bump_error()
            logger.error(f"[AI-Inspection] 审核消息时调用 LLM 出错: {exc}")
            return

        policy = self.config.get("moderation_policy", {}) or {}
        threshold = float(policy.get("confidence_threshold", 0.75))

        if not decision.violated or decision.confidence < threshold:
            if self._is_debug():
                logger.info(
                    f"[AI-Inspection] 放行群 {group_openid} 消息: violated={decision.violated}, "
                    f"conf={decision.confidence:.2f}, threshold={threshold:.2f}, reason={decision.reason}"
                )
            return

        await self._handle_violation(
            event=event,
            group_openid=group_openid,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            image_count=len(image_urls),
            decision=decision,
        )

    async def _handle_violation(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        sender_id: str,
        sender_name: str,
        text: str,
        image_count: int,
        decision: AuditDecision,
    ) -> None:
        self.tracker.bump_violated(decision.category_key)
        member_role = extract_member_role(event)
        is_group_privileged = member_role in ("owner", "admin")

        platform = self._get_platform(event)
        message_id = extract_message_id(event)

        if is_group_privileged:
            role_label = "群主" if member_role == "owner" else "群管理员"
            exec_res = ActionExecutionResult(
                final_action=ACTION_WARN_ONLY,
                punishment_skipped_reason=f"发送者为 QQ {role_label}，跳过接口处罚，仅记录告警",
            )
            logger.warning(
                f"[AI-Inspection] 检测到群 {group_openid} {role_label} {sender_name}({sender_id}) "
                f"触发违规（仅日志与告警，不调用撤回/禁言接口）: "
                f"分类={decision.category_name}, 置信度={decision.confidence:.2f}, "
                f"原因={decision.reason}"
            )
        else:
            policy = self.config.get("moderation_policy", {}) or {}
            if bool(policy.get("stop_event_on_violation", True)):
                event.stop_event()

            exec_res = await self._execute_actions(
                event=event,
                platform=platform,
                group_openid=group_openid,
                member_openid=sender_id,
                message_id=message_id,
                decision=decision,
            )

            logger.info(
                f"[AI-Inspection] 拦截群 {group_openid} 成员 {sender_name}({sender_id}) 违规消息: "
                f"分类={decision.category_name}, 置信度={decision.confidence:.2f}, "
                f"动作={exec_res.final_action}, 撤回={exec_res.recalled}, 禁言={exec_res.muted}, "
                f"原因={decision.reason}"
            )

        await self._send_notifications(
            event=event,
            platform=platform,
            group_openid=group_openid,
            member_openid=sender_id,
            sender_name=sender_name,
            text=text,
            image_count=image_count,
            decision=decision,
            exec_res=exec_res,
        )

    async def _execute_actions(
        self,
        event: AstrMessageEvent,
        platform: Any,
        group_openid: str,
        member_openid: str,
        message_id: str,
        decision: AuditDecision,
    ) -> ActionExecutionResult:
        policy = self.config.get("moderation_policy", {}) or {}
        enable_recall = bool(policy.get("enable_recall", True))
        enable_mute = bool(policy.get("enable_mute", True))
        default_mute = max(1, int(policy.get("default_mute_minutes", 10)))

        final_action = decision.action
        final_mute_minutes = decision.mute_minutes or default_mute

        esc = self.config.get("escalation", {}) or {}
        escalated = False
        violation_count = 1
        if bool(esc.get("enabled", True)) and member_openid:
            window_minutes = int(esc.get("window_minutes", 60))
            threshold_count = int(esc.get("threshold_count", 3))
            violation_count, should_escalate = (
                self.tracker.record_violation_and_check_escalation(
                    group_id=group_openid,
                    user_id=member_openid,
                    window_minutes=window_minutes,
                    threshold_count=threshold_count,
                )
            )
            if should_escalate:
                escalated = True
                escalate_mute = max(1, int(esc.get("escalate_mute_minutes", 120)))
                final_mute_minutes = max(final_mute_minutes, escalate_mute)
                if final_action in (ACTION_RECALL_ONLY, ACTION_RECALL_AND_MUTE):
                    final_action = ACTION_RECALL_AND_MUTE
                else:
                    final_action = ACTION_MUTE_ONLY

        res = ActionExecutionResult(
            escalated=escalated,
            violation_count=violation_count,
            final_action=final_action,
            final_mute_minutes=final_mute_minutes,
        )

        should_recall = enable_recall and final_action in (
            ACTION_RECALL_ONLY,
            ACTION_RECALL_AND_MUTE,
        )
        should_mute = enable_mute and final_action in (
            ACTION_MUTE_ONLY,
            ACTION_RECALL_AND_MUTE,
        )

        if should_recall:
            ok, err = await self.qq_client.recall_group_message(
                config=self.config,
                event=event,
                platform=platform,
                group_openid=group_openid,
                message_id=message_id,
            )
            res.recalled = ok
            res.recall_error = err
            if ok:
                self.tracker.bump_recalled()
            else:
                logger.warning(
                    f"[AI-Inspection] 撤回消息失败 (group={group_openid}, msg={message_id}): {err}"
                )

        if should_mute:
            ok, err = await self.qq_client.mute_group_member(
                config=self.config,
                event=event,
                platform=platform,
                group_openid=group_openid,
                member_openid=member_openid,
                mute_minutes=final_mute_minutes,
            )
            res.muted = ok
            res.mute_error = err
            if ok:
                self.tracker.bump_muted()
            else:
                logger.warning(
                    f"[AI-Inspection] 禁言成员失败 (group={group_openid}, user={member_openid}): {err}"
                )

        return res

    async def _send_notifications(
        self,
        event: AstrMessageEvent,
        platform: Any,
        group_openid: str,
        member_openid: str,
        sender_name: str,
        text: str,
        image_count: int,
        decision: AuditDecision,
        exec_res: ActionExecutionResult,
    ) -> None:
        policy = self.config.get("moderation_policy", {}) or {}
        action_parts: list[str] = []
        if exec_res.punishment_skipped_reason:
            action_text = f"仅记录告警（{exec_res.punishment_skipped_reason}）"
        else:
            if exec_res.recalled:
                action_parts.append("撤回消息")
            if exec_res.muted:
                action_parts.append(f"禁言 {exec_res.final_mute_minutes} 分钟")
            if not action_parts:
                action_parts.append(ACTION_LABELS.get(exec_res.final_action, "警告提醒"))
            action_text = "并".join(action_parts)
            if exec_res.escalated:
                action_text += f"（累犯第 {exec_res.violation_count} 次升级处罚）"

        if not exec_res.punishment_skipped_reason and bool(
            policy.get("notify_in_group", True)
        ):
            template = str(
                policy.get("notify_template")
                or "⚠️ 检测到违规消息（分类：{category}），已执行{action}。原因：{reason}"
            )
            fmt_values = defaultdict(
                str,
                {
                    "user": sender_name,
                    "category": decision.category_name,
                    "reason": decision.reason,
                    "action": action_text,
                    "mute_minutes": str(exec_res.final_mute_minutes),
                    "confidence": f"{decision.confidence:.2f}",
                },
            )
            notice_text = template.format_map(fmt_values)

            auto_del = max(0, int(policy.get("auto_delete_notice_seconds", 0)))
            include_at = bool(policy.get("notify_include_at", True))
            sent_via_direct = False

            if auto_del > 0:
                prefix = f"<@{member_openid}> " if include_at and member_openid else ""
                ok, sent_id, err = await self.qq_client.send_group_text_message(
                    config=self.config,
                    event=event,
                    platform=platform,
                    group_openid=group_openid,
                    content=f"{prefix}{notice_text}",
                    msg_id=extract_message_id(event),
                    msg_seq=1,
                )
                if ok:
                    sent_via_direct = True
                    if sent_id:
                        task = asyncio.create_task(
                            self._delayed_recall(
                                platform=platform,
                                group_openid=group_openid,
                                message_id=sent_id,
                                delay_seconds=auto_del,
                            )
                        )
                        self._bg_tasks.add(task)
                        task.add_done_callback(self._bg_tasks.discard)
                elif self._is_debug():
                    logger.debug(f"[AI-Inspection] 直接发送群提示失败，回退 event.send: {err}")

            if not sent_via_direct:
                chain: list[Comp.BaseMessageComponent] = []
                if include_at and member_openid:
                    chain.append(Comp.At(qq=member_openid))
                    chain.append(Comp.Plain(" "))
                chain.append(Comp.Plain(notice_text))
                try:
                    await event.send(event.chain_result(chain))
                except Exception as exc:
                    logger.warning(f"[AI-Inspection] 发送群内提醒失败: {exc}")

        admin_umos = [
            str(x).strip()
            for x in (policy.get("admin_notify_umos") or [])
            if str(x).strip()
        ]
        if admin_umos:
            preview = text.strip().replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "..."
            if not preview:
                preview = "(无文本)"
            if image_count > 0:
                preview += f" [图片x{image_count}]"

            recall_status = (
                f"已跳过（{exec_res.punishment_skipped_reason}）"
                if exec_res.punishment_skipped_reason
                else (
                    "成功"
                    if exec_res.recalled
                    else (
                        "未执行/失败: " + exec_res.recall_error
                        if exec_res.recall_error
                        else "未启用"
                    )
                )
            )
            mute_status = (
                f"已跳过（{exec_res.punishment_skipped_reason}）"
                if exec_res.punishment_skipped_reason
                else (
                    "成功"
                    if exec_res.muted
                    else (
                        "未执行/失败: " + exec_res.mute_error
                        if exec_res.mute_error
                        else "未启用"
                    )
                )
            )

            audit_report = (
                "🛡️ AI 群消息审计通知\n"
                f"群 ID: {group_openid}\n"
                f"发送者: {sender_name} ({member_openid})\n"
                f"违规分类: {decision.category_name} ({decision.category_key})\n"
                f"判定置信度: {decision.confidence:.2f} (来源: {decision.source})\n"
                f"判定原因: {decision.reason}\n"
                f"执行结果: {action_text}\n"
                f"撤回状态: {recall_status}\n"
                f"禁言状态: {mute_status}\n"
                f"消息摘要: {preview}"
            )
            for umo in admin_umos:
                try:
                    await self.context.send_message(
                        umo, MessageChain().message(audit_report)
                    )
                except Exception as exc:
                    logger.warning(
                        f"[AI-Inspection] 推送审计通知到 {umo} 失败: {exc}"
                    )

    async def _delayed_recall(
        self,
        platform: Any,
        group_openid: str,
        message_id: str,
        delay_seconds: int,
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            ok, err = await self.qq_client.recall_group_message(
                config=self.config,
                event=None,
                platform=platform,
                group_openid=group_openid,
                message_id=message_id,
            )
            if not ok and self._is_debug():
                logger.debug(f"[AI-Inspection] 自动撤回提醒消息失败: {err}")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug(f"[AI-Inspection] 自动撤回提醒异常: {exc}")

    # -------------------- 管理员指令组 --------------------

    @filter.command_group("audit")
    def audit(self) -> None:
        """AI 群消息审核管理指令组"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前插件状态与本群审核信息"""
        if not event.is_admin():
            return
        group_openid = extract_group_openid(event)
        monitored = self._is_group_monitored(event, group_openid) if group_openid else False
        llm_cfg = self.config.get("llm", {}) or {}
        prov_id = str(llm_cfg.get("llm_provider_id") or "").strip()
        if not prov_id:
            try:
                prov_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception:
                prov_id = "默认会话模型"
        categories = build_active_categories(self.config)
        sched = evaluate_schedule(self.config)
        stats = self.tracker.stats
        yield event.plain_result(
            "🛡️ AI 群消息审核状态\n"
            f"总开关: {'已开启' if self.config.get('enabled', True) else '已关闭'}\n"
            f"当前群监控: {'✅ 已启用' if monitored else '❌ 未启用'}\n"
            f"时段状态: {sched.summary}\n"
            f"使用 LLM: {prov_id or '未配置'}\n"
            f"启用分类数: {len(categories)}\n"
            f"累计审核: {stats['total_checked']} | 拦截违规: {stats['total_violated']} | "
            f"撤回成功: {stats['total_recalled']} | 禁言成功: {stats['total_muted']} | "
            f"异常: {stats['total_errors']}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("id")
    async def cmd_id(self, event: AstrMessageEvent):
        """显示当前群聊与用户的 OpenID / UMO"""
        if not event.is_admin():
            return
        group_openid = extract_group_openid(event) or "(私聊或非群聊)"
        member_openid = extract_member_openid(event) or "(未知)"
        yield event.plain_result(
            f"group_openid: {group_openid}\n"
            f"member_openid: {member_openid}\n"
            f"UMO: {event.unified_msg_origin}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("enable")
    async def cmd_enable(self, event: AstrMessageEvent):
        """在当前群开启 AI 消息审核"""
        if not event.is_admin():
            return
        group_openid = extract_group_openid(event)
        if not group_openid:
            yield event.plain_result("请在需要开启审核的 QQ 群内发送此指令。")
            return

        scope = self.config.setdefault("scope", {})
        monitored = list(scope.get("monitored_group_ids") or [])
        ignored = list(scope.get("ignored_group_ids") or [])
        if group_openid in ignored:
            ignored = [x for x in ignored if str(x).strip() != group_openid]
            scope["ignored_group_ids"] = ignored
        if group_openid not in monitored:
            monitored.append(group_openid)
            scope["monitored_group_ids"] = monitored

        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"✅ 已为当前群 ({group_openid}) 开启 AI 消息审核。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("disable")
    async def cmd_disable(self, event: AstrMessageEvent):
        """在当前群关闭 AI 消息审核"""
        if not event.is_admin():
            return
        group_openid = extract_group_openid(event)
        if not group_openid:
            yield event.plain_result("请在需要关闭审核的 QQ 群内发送此指令。")
            return

        scope = self.config.setdefault("scope", {})
        monitored = [
            x
            for x in (scope.get("monitored_group_ids") or [])
            if str(x).strip() != group_openid
        ]
        scope["monitored_group_ids"] = monitored

        if bool(scope.get("audit_all_groups", False)):
            ignored = list(scope.get("ignored_group_ids") or [])
            if group_openid not in ignored:
                ignored.append(group_openid)
            scope["ignored_group_ids"] = ignored

        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"⏸️ 已为当前群 ({group_openid}) 关闭 AI 消息审核。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("test")
    async def cmd_test(self, event: AstrMessageEvent):
        """试运行审核一段文本（不执行撤回与禁言）：/audit test 待测文本"""
        if not event.is_admin():
            return
        raw = (event.message_str or "").strip()
        sample = ""
        for prefix in ("/audit test", "audit test"):
            idx = raw.find(prefix)
            if idx != -1:
                sample = raw[idx + len(prefix) :].strip()
                break
        if not sample:
            yield event.plain_result("用法：/audit test <需要测试审核的文本内容>")
            return

        categories = build_active_categories(self.config)
        if not categories:
            yield event.plain_result("当前未启用任何审核分类，无法试运行。")
            return

        try:
            decision = await self.moderator.audit_message(
                context=self.context,
                umo=event.unified_msg_origin,
                text=sample,
                image_urls=[],
                sender_name=event.get_sender_name() or "测试用户",
                sender_id=extract_member_openid(event) or "test_user",
                context_lines=[],
                categories=categories,
                config=self.config,
                event=event,
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 试运行审核失败：{exc}")
            return

        action_label = ACTION_LABELS.get(decision.action, decision.action)
        yield event.plain_result(
            "🧪 AI 审核试运行结果\n"
            f"是否违规: {'⚠️ 是' if decision.violated else '✅ 否'}\n"
            f"命中分类: {decision.category_name} ({decision.category_key})\n"
            f"置信度: {decision.confidence:.2f}\n"
            f"计划动作: {action_label}"
            + (f"（{decision.mute_minutes} 分钟）" if "mute" in decision.action else "")
            + f"\n理由: {decision.reason}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @audit.command("unmute")
    async def cmd_unmute(self, event: AstrMessageEvent, member_openid: str = ""):
        """解除当前群指定成员的禁言：/audit unmute <member_openid>"""
        if not event.is_admin():
            return
        group_openid = extract_group_openid(event)
        target_id = member_openid.strip()
        if not group_openid or not target_id:
            yield event.plain_result("用法（在群内发送）：/audit unmute <member_openid>")
            return

        platform = self._get_platform(event)
        ok, err = await self.qq_client.unmute_group_member(
            config=self.config,
            event=event,
            platform=platform,
            group_openid=group_openid,
            member_openid=target_id,
        )
        if ok:
            yield event.plain_result(f"✅ 已请求解除 {target_id} 的禁言。")
        else:
            yield event.plain_result(f"❌ 解除禁言失败：{err}")
