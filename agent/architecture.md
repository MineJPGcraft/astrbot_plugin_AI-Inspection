# 系统架构与核心流程

## 1. 配置分组结构（`_conf_schema.json`）

| 分组 Key | 用途 | 关键配置项 |
| --- | --- | --- |
| `enabled` | 插件总开关 | `bool`，默认 `true` |
| `scope` | 群聊监控范围 | `audit_all_groups`、`monitored_umos`（`_special: umo`）、`monitored_group_ids`、`ignored_group_ids` |
| `schedule` | 分时段审核限制 | `enabled`、`active_ranges`（`HH:MM-HH:MM`）、`off_hours_mode`（`keyword_only` / `disable_all`）、`active_weekdays`、`timezone_offset_hours` |
| `llm` | LLM 复用与提示词 | `llm_provider_id`、`fallback_provider_id`（均使用 `_special: select_provider`）、`extra_system_prompt`、`custom_system_prompt`、`audit_image`、`audit_context_count`、`llm_timeout_seconds`、`temperature` |
| `moderation_policy` | 处置与通知策略 | `confidence_threshold`、`enable_recall`、`enable_mute`、`default_mute_minutes`、`stop_event_on_violation`、`notify_in_group`、`notify_include_at`、`notify_template`、`auto_delete_notice_seconds`、`admin_notify_umos` |
| `categories` | 6 类预设违规分类 | `spam_ad`、`nsfw_porn`、`politics_illegal`、`fraud_phishing`、`harassment_abuse`、`spoiler_offtopic`（每类独立配置 `enabled`、`action`、`mute_minutes`、`description`） |
| `custom_categories` | 自定义违规分类 | `template_list`（每项含 `key`、`name`、`enabled`、`action`、`mute_minutes`、`description`） |
| `escalation` | 累犯升级惩罚 | `enabled`、`window_minutes`、`threshold_count`、`escalate_mute_minutes` |
| `whitelist_and_filter` | 黑白名单与预过滤 | `exempt_astrbot_admins`、`exempt_group_admins`、`whitelist_users`、`blacklist_users`、`skip_commands`、`min_text_length`、`ignore_pure_emoji_face`、`keyword_blocklist`、`keyword_category_name`、`keyword_action`、`keyword_mute_minutes`、`keyword_allowlist`、`rate_limit_per_group_per_minute` |
| `qq_official_api` | QQ 官机接口高级配置 | `api_base_url`、`qq_official_appid`、`qq_official_secret`（`secret: true`）、`debug_log` |

---

## 2. 群消息审核管线（`main.py` -> `on_group_message`）

事件过滤器：`@filter.platform_adapter_type(QQOFFICIAL | QQOFFICIAL_WEBHOOK)` 与 `@filter.event_message_type(GROUP_MESSAGE, priority=100)`。

执行顺序如下：

1. **总开关与群范围检查**：校验 `enabled`、`group_openid` 非空及 `_is_group_monitored()`（优先级：`ignored_group_ids` 排除 > `monitored_umos` / `monitored_group_ids` 命中 > `audit_all_groups`）。
2. **指令放行与自身过滤**：
   - `/audit` 开头的管理指令无条件放行；
   - `sender_id == self_id`（机器人自身消息）直接跳过。
3. **黑名单与时段判定**：
   - `is_blacklisted = _is_sender_blacklisted(sender_id)`；
   - `sched = evaluate_schedule(self.config)`；
   - 黑名单用户强制按 `effective_mode = MODE_FULL` 处理；非黑名单用户遵循 `sched.mode`，若为 `MODE_DISABLED` 则直接放行。
   - 非黑名单且开启 `skip_commands` 时跳过 `/` 前缀指令。
4. **消息提取与上下文记录**：
   - `_extract_message_payload()` 解析 `Plain`、`At`、`Image`（非 `MODE_KEYWORD_ONLY` 且开启 `audit_image` 时提取 URL 或通过 `convert_to_file_path()` 转本地路径）、`Face`。
   - 调用 `tracker.record_recent_message()` 记录近期群聊摘要，供多轮上下文审核使用。
5. **预过滤（仅针对非黑名单用户）**：
   - `_is_sender_exempt()`：`exempt_astrbot_admins`（`event.is_admin()`）、`exempt_group_admins`（`extract_member_role(event) in ("owner", "admin")`）、`whitelist_users`；
   - `ignore_pure_emoji_face`、`min_text_length`、`keyword_allowlist`。
   - 黑名单用户仅在消息既无文本也无图片时跳过。
6. **本地极速敏感词拦截（零 LLM 开销、独立处置策略）**：
   - `MessageModerator.check_keyword_blocklist()` 支持普通子串（大小写不敏感）与 `re:` 正则。
   - 命中后通过 `build_keyword_rule()` 构造独立于广告等 AI 分类的 `CategoryRule(key="keyword_blocklist")` 与 `AuditDecision(source="keyword")`，直接按 `keyword_category_name`、`keyword_action`、`keyword_mute_minutes` 进入 `_handle_violation()`。
7. **非审核时段截断**：
   - 若 `effective_mode == MODE_KEYWORD_ONLY`，在此直接返回，不触发限流也不调用 LLM。
8. **AI Function Calling 审核**：
   - 校验单群每分钟限流 `tracker.allow_audit()`。
   - 调用 `MessageModerator.audit_message()` 获取结构化 `AuditDecision`。
   - 若 `decision.violated` 且 `decision.confidence >= confidence_threshold`，进入 `_handle_violation()`。

---

## 3. 违规处置与管理员保护逻辑（`_handle_violation`）

1. **群主/群管理员保护分支**（`extract_member_role(event) in ("owner", "admin")`）：
   - 当关闭 `exempt_group_admins` 或群主/管理员位于 `blacklist_users` 时，消息会经过检测；
   - 若命中违规，因机器人无法在 QQ 群内撤回/禁言群主和管理员，插件**不调用**撤回/禁言接口、**不计入**累犯升级、**不阻断**事件传播、**不发送**群内公开提醒；
   - 仅记录 `[WARN]` 日志，并向 `admin_notify_umos` 推送详细审计告警。
2. **普通成员处置分支**：
   - 若 `stop_event_on_violation=true`，调用 `event.stop_event()` 阻止违规消息继续触发主对话或其它插件；
   - `_execute_actions()` 评估 `escalation` 累犯升级，并按 `enable_recall` / `enable_mute` 调用 `QQOfficialClient`；
   - `_send_notifications()` 按模板发送群内提醒（若 `auto_delete_notice_seconds > 0`，通过直接调用 QQ 发送接口拿到 `sent_id` 后起后台任务延时撤回），并推送管理员审计报告。

---

## 4. LLM 工具调用策略（`core/moderator.py`）

`SubmitModerationDecisionTool` 定义 `submit_moderation_decision` 工具，参数 Schema 中的 `category` 动态绑定 `enum: ["safe", *active_category_keys]`。

`_invoke_llm_for_decision()` 按以下优先级调用：
1. **Path 1**：若 `context.llm_generate` 签名含 `tools` 或 `func_tool`，单轮请求并直接从 `LLMResponse.tools_call_args` 提取参数。
2. **Path 2**：否则解析出 `Provider` 实例调用 `prov.text_chat(..., func_tool=tool_set)`，同样单轮完成并支持多模态 `image_urls`。
3. **Path 3**：若单轮接口不支持工具入参且 `event` 存在，回退 `context.tool_loop_agent(..., max_steps=2)` 并在工具 `call()` 内捕获结果。
4. **降级兜底**：
   - 主提供商失败时尝试 `fallback_provider_id`；
   - 携带图片调用失败时自动降级为纯文本重试；
   - 对不支持 Function Calling 的老模型保留 `<think>` 剥离、JSON 提取及中文判定结论正则兜底解析。

---

## 5. QQ 官方机器人接口（`core/qq_official_api.py`）

- **通道策略**：生产环境域名（`https://api.sgroup.qq.com`）下优先复用 `botpy` 客户端 `api._http.request(Route(...))`；当 `botpy` 客户端不可用（如部分 Webhook 场景）或配置了沙箱地址时，自动读取平台适配器/插件配置中的 `AppID` 与 `AppSecret`，通过 `https://bots.qq.com/app/getAppAccessToken` 获取并缓存 Token（带 `asyncio.Lock` 与 401 自动刷新重试）直连 REST API。
- **使用接口**：
  - 撤回消息：`DELETE /v2/groups/{group_openid}/messages/{message_id}`
  - 禁言/解禁：`POST /v2/groups/{group_openid}/restrict_chat_setting`（`op: "add" | "del"`，`mute_expire_at` 为 RFC3339 格式，最长 30 天即 `43200` 分钟）
  - 群文本发送：`POST /v2/groups/{group_openid}/messages`（用于需要延时自撤回提示的场景）

---

## 6. 管理员指令（`/audit`）

| 指令 | 作用 |
| --- | --- |
| `/audit status` | 查看总开关、当前群监控状态、时段状态、使用的 LLM 与统计计数 |
| `/audit id` | 查看当前 `group_openid`、`member_openid`、`member_role` 与 `UMO` |
| `/audit enable` | 将当前群加入 `monitored_group_ids` 并持久化保存配置 |
| `/audit disable` | 将当前群移出 `monitored_group_ids`（或加入 `ignored_group_ids`）并保存 |
| `/audit test <文本>` | 试运行 AI 审核指定文本（不执行撤回/禁言） |
| `/audit unmute <member_openid>` | 在当前群解除指定成员禁言 |
