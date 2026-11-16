# QQ 官方机器人相关接口与数据结构参考

本文整理了本项目在 **QQ 开放平台 API v2** 与 **AstrBot (`qq_official` / `qq_official_webhook`)** 中实际使用的接口规范、事件载荷结构与调用方式。

---

## 1. 认证与基础域名

- **正式环境 API Base**：`https://api.sgroup.qq.com`
- **沙箱环境 API Base**：`https://sandbox.api.sgroup.qq.com`
- **获取 AccessToken**：
  - 接口：`POST https://bots.qq.com/app/getAppAccessToken`（亦兼容 `https://api.bot.qq.com/app/getAppAccessToken`）
  - 请求体：
    ```json
    {
      "appId": "<AppID>",
      "clientSecret": "<AppSecret>"
    }
    ```
  - 响应：
    ```json
    {
      "access_token": "<ACCESS_TOKEN>",
      "expires_in": "7200"
    }
    ```
  - 请求业务接口时的鉴权请求头：
    ```http
    Authorization: QQBot <ACCESS_TOKEN>
    Content-Type: application/json
    ```
- **项目实现细节**（见 `core/qq_official_api.py` -> `QQOfficialClient`）：
  - 在正式环境域名下优先复用 `botpy` 客户端 `bot.api._http.request(Route(...))`；
  - 若 `botpy` 客户端不可用（如部分 Webhook 场景）或配置了自定义/沙箱地址，则从平台实例/插件配置读取 `AppID` 与 `AppSecret`，缓存 `access_token`（提前 120 秒过期刷新，带 `asyncio.Lock` 防并发重复请求），遇 `HTTP 401` 时自动强制刷新重试一次。

---

## 2. 群消息接收事件：`GROUP_MESSAGE_CREATE`

- **官方说明**：当机器人在群内开启「接收所有消息 / 获取群内全部消息」后，群聊中的每一条消息（不限于 `@机器人`）都会推送此事件。
- **Intent**：`GROUP_AND_C2C_EVENT (1<<25)`
- **官方事件文档**：`https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html`

### 2.1 Event Payload 主体字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `string` | 消息 ID，可用于被动回复和撤回 |
| `author` | `User` | 发送者信息对象 |
| `content` | `string` | 消息文本内容；`@某成员` 会替换为 `<@member_openid>`，`@全体` 为 `<@all>` |
| `group_openid` | `string` | 群唯一标识（OpenID） |
| `timestamp` | `string` | 消息发送时间（RFC3339 格式） |
| `message_type` | `integer` | 消息内容类型（`0`=普通文本，`3`=结构化卡片，`101`=并行消息，`102`=聊天记录，`103`=引用消息） |
| `message_scene` | `MessageScene` | 消息场景上下文（`source`、`ext`） |
| `attachments` | `[]MessageAttachment` | 消息附件（图片/语音/视频/文件等） |
| `mentions` | `[]User` | 消息中 `@` 的用户列表 |
| `ark_data` | `ARKData` | 结构化卡片消息数据 |
| `msg_elements` | `[]MsgElement` | 消息元素列表 |

### 2.2 `User`（`author`）对象字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `string` | 用户唯一标识（OpenID） |
| `username` | `string` | 用户昵称 |
| `bot` | `boolean` | 是否为机器人 |
| `union_openid` | `string` | 跨应用统一用户 OpenID（可能为空） |
| `union_user_account` | `string` | 跨应用统一用户账号（可能为空） |
| `user_openid` | `string` | 用户 OpenID（单聊场景） |
| `member_openid` | `string` | 群成员 OpenID（群聊场景） |
| **`member_role`** | **`string`** | **群内角色：`member`=普通成员、`admin`=管理员、`owner`=群主** |

> **AstrBot 适配重要提示**：  
> `qq_official` 与 `qq_official_webhook` 均将 `event.message_obj.raw_message` 构造为 `PatchedGroupMessage`（继承自 `botpy.message.GroupMessage`）。由于原版 `botpy` 会丢弃 `member_role`，AstrBot 将完整原始载荷字典保存在 **`raw_message.raw_data`** 中，因此读取 `author.member_role` 必须优先检查 `raw_message.raw_data`。

---

## 3. 群聊消息撤回接口

- **HTTP 接口**：`DELETE /v2/groups/{group_openid}/messages/{message_id}`
- **路径参数**：
  - `group_openid` (`string`)：群 OpenID
  - `message_id` (`string`)：待撤回消息 ID
- **权限与限制**：
  - 机器人撤回自己发送的消息通常需在发送后 **2 分钟内**；
  - 撤回其他群成员消息需要机器人在群内具备**群管理员身份**，且应用在 QQ 开放平台已获得对应群管理撤回权限，否则平台会返回权限错误（如 `HTTP 403` / 相关错误码）。
- **项目调用封装**：`QQOfficialClient.recall_group_message()`

---

## 4. 群成员禁言 / 解禁接口

- **查询群禁言状态**：`GET /v2/groups/{group_openid}/restrict_chat_setting`（文档频限 30 QPM）
- **设置 / 解除群成员禁言**：`POST /v2/groups/{group_openid}/restrict_chat_setting`（文档频限 60 QPM）
- **请求体（Request Body）**：
  - 添加禁言：
    ```json
    {
      "members": [
        {
          "op": "add",
          "member_openid": "EC58D87F598C8294A533B9D458DAAF33",
          "mute_expire_at": "2026-11-16T15:00:00+08:00"
        }
      ]
    }
    ```
  - 解除禁言：
    ```json
    {
      "members": [
        {
          "op": "del",
          "member_openid": "EC58D87F598C8294A533B9D458DAAF33"
        }
      ]
    }
    ```
- **字段说明**：
  - `members` (`Array`)：单次调用最多支持操作 **20 个成员**；
  - `op` (`string`)：`"add"` 添加禁言，`"del"` 解除禁言；
  - `member_openid` (`string`)：目标群成员的 `member_openid`；
  - `mute_expire_at` (`string`)：禁言到期时间，**RFC3339 格式**（如 `+08:00`），`op="add"` 时必填，单次最大禁言时长为 **30 天（`43200` 分钟）**；`op="del"` 时可省略。
- **权限要求**：机器人必须在目标 QQ 群具备**群管理员身份**且开放平台应用拥有群管理接口权限，且无法对群主或群管理员生效。
- **项目调用封装**：`QQOfficialClient.mute_group_member()`、`QQOfficialClient.unmute_group_member()`

---

## 5. 发送群聊消息接口（用于延时自动撤回的提醒消息）

- **HTTP 接口**：`POST /v2/groups/{group_openid}/messages`（文档频限 100 QPS）
- **请求体示例（纯文本被动回复）**：
  ```json
  {
    "content": "<@member_openid> ⚠️ 检测到违规消息...",
    "msg_type": 0,
    "msg_id": "<触发事件的原消息id>",
    "msg_seq": 1
  }
  ```
- **注意事项**：
  - `msg_id` 用于被动回复（5 分钟内有效，最多回复 5 次）；
  - 当配置了 `auto_delete_notice_seconds > 0` 时，插件直接调用本接口从响应 JSON 中读取新消息的 `id` / `msg_id`，再启动后台定时任务调用 `recall_group_message()` 自动撤回提醒消息；若直接发送失败则回退到 `event.send()`。
- **项目调用封装**：`QQOfficialClient.send_group_text_message()`
