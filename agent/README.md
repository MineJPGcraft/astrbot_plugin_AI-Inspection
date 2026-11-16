# `astrbot_plugin_AI-Inspection` 项目知识库

本目录用于记录 `astrbot_plugin_AI-Inspection`（插件标识 `astrbot_plugin_ai_inspection`）的项目架构、配置设计、核心流程以及开发过程中总结的 AstrBot / QQ 官方机器人踩坑经验，方便后续维护与 AI Agent 快速接手开发。

---

## 文档导航

- [architecture.md](./architecture.md)：模块职责、消息审核管线、Function Calling 调用链、时段与黑白名单优先级、QQ 官机接口实现细节。
- [qq-official-api.md](./qq-official-api.md)：QQ 官方机器人接口文档（认证 Token、`GROUP_MESSAGE_CREATE` 载荷与 `member_role`、群撤回/禁言/解禁/发消息接口规范与限制）。
- [dev-experience.md](./dev-experience.md)：开发与联调过程中踩过的坑、根本原因与已验证的解决方案。

---

## 项目基本信息

| 项目项 | 值 |
| --- | --- |
| 仓库名称 | `astrbot_plugin_AI-Inspection` |
| 插件模块名 (`metadata.yaml` `name`) | `astrbot_plugin_ai_inspection` |
| 展示名 (`display_name`) | `AI 群消息审核 (QQ官机)` |
| 最低 AstrBot 版本 | `>=4.5.7`（已兼容 `< v4.30.0` 如 `v4.29.4` 与 `>= v4.30.0`） |
| 支持平台 | `qq_official`（WebSocket）、`qq_official_webhook`（Webhook） |
| 主入口类 | `AIInspectionPlugin(Star)`（位于 `main.py`） |

---

## 目录与文件说明

| 路径 | 职责 |
| --- | --- |
| `metadata.yaml` | 插件元数据（注意 `name` 必须为合法 Python 标识符） |
| `_conf_schema.json` | AstrBot WebUI 可视化配置 Schema |
| `main.py` | 消息事件监听（`priority=100`）、审核管线调度、违规处置与 `/audit` 管理员指令组 |
| `core/__init__.py` | `core` 包统一导出 |
| `core/models.py` | 分类规则模型 `CategoryRule`、判定结论 `AuditDecision`、执行结果 `ActionExecutionResult` 与规则合并逻辑 |
| `core/moderator.py` | 复用 AstrBot LLM 的审核引擎，基于 `SubmitModerationDecisionTool` Function Calling 与文本兜底解析 |
| `core/qq_official_api.py` | QQ 官方机器人 OpenAPI 封装（凭证提取、Token 缓存、撤回/禁言/解禁/发送、`member_role` 提取） |
| `core/schedule.py` | 分时段审核控制（支持多时段、跨午夜、星期限制与时区偏移） |
| `core/tracker.py` | 运行期状态跟踪（单群限流滑窗、近期上下文缓存、累犯时间窗升级、统计计数） |
| `requirements.txt` | 运行依赖（`aiohttp>=3.8.0`） |
| `docs/dev/` | AstrBot 官方插件开发参考文档 |
