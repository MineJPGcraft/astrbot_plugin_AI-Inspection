# 开发经验与踩坑记录

以下记录均来自本项目在 AstrBot `v4.29.4` + QQ 官方机器人适配过程中实际遇到并验证解决的问题。

---

## 1. `metadata.yaml` 的 `name` 必须是合法 Python 标识符
- **现象**：最初 `metadata.yaml` 中填写 `name: astrbot_plugin_AI-Inspection`，在 AstrBot WebUI 安装时触发报错：
  `PluginManager._validate_importable_name` -> `Exception: metadata 文件中 name 不是合法的模块名称（应为合法 Python 标识符且非关键字）。`
- **原因**：AstrBot 会以 `metadata.yaml` 的 `name` 作为插件目录名和 Python 导入模块名，连字符 `-` 在 Python 中非法。
- **规范**：`name` 应全小写且只用字母、数字和下划线（本项目改为 `astrbot_plugin_ai_inspection`），而 `repo` 字段仍保留实际的 GitHub 仓库 URL（可含 `-`）。

---

## 2. AstrBot `v4.30.0` 前后 Agent / Tool 导入路径变化
- **现象**：文档中推荐 `from astrbot.api import FunctionTool, ToolExecResult, ToolSet` 与 `from astrbot.api.agents import AstrAgentContext, ContextWrapper`，但该导入方式在 `v4.30.0` 才引入，在用户的 `v4.29.4` 环境会报 `ImportError`。
- **解决**：在 [core/moderator.py](../core/moderator.py) 中使用 `try ... except ImportError` 双路径兼容：
  - 优先从 `astrbot.api` / `astrbot.api.agents` 导入；
  - 失败时回退到 `astrbot.core.agent.tool`、`astrbot.core.agent.run_context` 与 `astrbot.core.astr_agent_context`。

---

## 3. 不要只依赖纯文本提示词让 LLM 输出 JSON，优先使用 Function Calling
- **现象**：仅靠 `system_prompt` 要求模型输出 JSON 时，部分模型（或受对话人设影响时）会直接输出自然语言，例如：
  `审核结果：通过\n理由：内容仅为普通问候“你好”，未发现违规内容。`
- **解决**：
  1. 改用 **Function Calling（工具调用）**：定义 `submit_moderation_decision` 工具并用 `enum` 严格限制 `category` 取值范围；
  2. 通过 `inspect.signature` 检测 `llm_generate` / `Provider.text_chat` 的参数支持情况，优先走**单轮**带工具调用（1 次请求直接拿 `tools_call_args`，避免 `tool_loop_agent` 多跑一轮汇总请求增加延迟和 Token 消耗）；
  3. 保留 `<think>...</think>` 剥离、JSON 提取和中文结论正则兜底，确保遇到不支持工具调用的模型也不崩溃、不误刷 `WARN`。

---

## 4. QQ 官机（`qq_official` / `qq_official_webhook`）中 `member_role` 必须从 `raw_message.raw_data` 读取
- **现象**：最初从 `event.message_obj.raw_message.author.member_role` 读取发送者群角色，结果哪怕群主或管理员发送本地敏感词，也始终被识别为普通成员 `"member"`，导致无法豁免且尝试错误处罚。
- **根因**（结合 QQ 官方 `GROUP_MESSAGE_CREATE` 文档与 AstrBot `qqofficial_platform_adapter.py` 源码）：
  - QQ 官机 `GROUP_MESSAGE_CREATE` 事件体中的 `author.member_role` 确实携带了 `"owner"` / `"admin"` / `"member"`；
  - 但 AstrBot 底层依赖的 `qq-botpy` 库在构造 `botpy.message.GroupMessage.author`（`_User`）时**丢弃了 `member_role` 字段**；
  - AstrBot 自身在 `PatchedGroupMessage`（同时服务于 `qqofficial` 和 `qqofficial_webhook`）中通过 `_set_raw_message_fields` 将完整的原始事件字典 `payload["d"]` 保存在 **`raw_message.raw_data`** 上。
- **解决**：[extract_member_role](../core/qq_official_api.py) 优先遍历 `raw_message.raw_data`（并兼容 `_raw`、`data`、`d`、原始字典及 `sender`），读取 `author.member_role`；同时在 `/audit id` 中输出 `member_role` 方便群内一眼验证。

---

## 5. 群主与群管理员在“关闭豁免”和“黑名单”场景下的处置边界
- **问题**：关闭「豁免 QQ 群主与群管理员」或将其加入敏感词/黑名单监控时，若命中违规仍尝试调用撤回或禁言接口，必然因权限不足失败，甚至可能发送不合时宜的群内处罚提示。
- **原则**：
  - **开启豁免**：直接在检测前跳过（不查敏感词、不调 LLM）；
  - **关闭豁免（或黑名单命中）**：正常执行敏感词和 AI 审核，但命中违规后先判断 `extract_member_role(event) in ("owner", "admin")`，若为群主/管理员则**跳过撤回/禁言接口、不发群内警告、不计数累犯**，只保留日志和向 `admin_notify_umos` 推送管理员告警。

---

## 6. QQ 官机与 AstrBot 插件开发细节清单
- **群全量消息开关**：QQ 官机默认只推 `@机器人` 消息，必须由群管理在手机 QQ 机器人资料/群设置中开启「**获取群内全部消息**」（对应开放平台 `GROUP_MESSAGE_CREATE` 事件），插件才能收到普通发言。
- **指令带空格问题**：AstrBot `@filter.command` 默认按空格拆分参数，因此 `/audit test <待测文本>` 不能直接靠单个函数参数接收含空格文本，需要从 `event.message_str` 中截取 `/audit test` 之后的内容。
- **容器时区偏差**：AstrBot 常部署在默认 UTC 时区的 Docker 容器内，分时段审核不能直接依赖宿主机本地时间，需提供 `timezone_offset_hours`（默认 `8` 即北京时间 `UTC+8`），并支持 `22:00-08:00` 这类跨午夜时间段。
- **事件优先级与指令放行**：消息审核监听器设置了 `priority=100` 以便在主 LLM 对话前用 `event.stop_event()` 拦截违规消息；因此必须在审核逻辑最前面无条件放行 `/audit` 指令，否则管理员指令本身会被优先拦截或误审。
- **配置变更与热保存**：在群内执行 `/audit enable` / `/audit disable` 修改 `self.config` 后，需调用 `self.config.save_config()` 才会持久化到 `data/config/<plugin_name>_config.json`。
- **延时空窗消息撤回**：通过 AstrBot `event.send()` 发送提示通常拿不到 QQ 返回的 `message_id`；当配置了 `auto_delete_notice_seconds > 0` 时，直接调用 `POST /v2/groups/{group_openid}/messages`（附带原消息 `msg_id` 作为被动回复）以获取新消息 `id`，再用异步任务延时调用撤回接口。
