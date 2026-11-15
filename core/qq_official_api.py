"""QQ Official Bot OpenAPI helper for group recall, mute, unmute, and messaging."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_API_BASE = "https://api.sgroup.qq.com"
TZ_CN = timezone(timedelta(hours=8))


def _get_attr_or_key(obj: Any, *names: str) -> Any:
    """Safely read the first non-empty attribute or dict key from `obj`."""
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict):
            val = obj.get(name)
        else:
            val = getattr(obj, name, None)
        if val not in (None, ""):
            return val
    return None


def extract_group_openid(event: AstrMessageEvent) -> str:
    """Extract QQ Official group_openid from an AstrMessageEvent."""
    raw = getattr(event.message_obj, "raw_message", None)
    gid = event.get_group_id() or getattr(event.message_obj, "group_id", "")
    if not gid and raw is not None:
        gid = _get_attr_or_key(raw, "group_openid", "group_id") or ""
    return str(gid or "").strip()


def extract_member_openid(event: AstrMessageEvent) -> str:
    """Extract QQ Official member_openid from an AstrMessageEvent."""
    uid = event.get_sender_id()
    if not uid:
        sender = getattr(event.message_obj, "sender", None)
        uid = _get_attr_or_key(sender, "user_id", "member_openid")
    if not uid:
        raw = getattr(event.message_obj, "raw_message", None)
        author = _get_attr_or_key(raw, "author")
        uid = _get_attr_or_key(author, "member_openid", "user_openid", "id")
    return str(uid or "").strip()


def extract_message_id(event: AstrMessageEvent) -> str:
    """Extract QQ Official message_id from an AstrMessageEvent."""
    mid = getattr(event.message_obj, "message_id", "")
    if not mid:
        raw = getattr(event.message_obj, "raw_message", None)
        mid = _get_attr_or_key(raw, "id", "message_id", "msg_id") or ""
    return str(mid or "").strip()


def extract_member_role(event: AstrMessageEvent) -> str:
    """Extract sender group role ('owner', 'admin', or 'member') when available."""
    raw = getattr(event.message_obj, "raw_message", None)
    if raw is None:
        return "member"

    author = _get_attr_or_key(raw, "author")
    role = _get_attr_or_key(author, "member_role", "role")
    if role:
        role_lower = str(role).strip().lower()
        if role_lower in ("owner", "admin", "4", "2"):
            return "owner" if role_lower in ("owner", "4") else "admin"

    roles = _get_attr_or_key(author, "roles")
    if isinstance(roles, (list, tuple, set)):
        lowered = {str(r).strip().lower() for r in roles}
        if "owner" in lowered or "4" in lowered:
            return "owner"
        if "admin" in lowered or "2" in lowered:
            return "admin"

    return "member"


class QQOfficialClient:
    """Async QQ Official Bot API client with botpy + direct HTTP fallback."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close underlying HTTP client session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _get_bot_client(self, event: AstrMessageEvent | None, platform: Any) -> Any:
        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                return bot
        if platform is not None:
            get_client = getattr(platform, "get_client", None)
            if callable(get_client):
                try:
                    return get_client()
                except Exception:
                    pass
            return getattr(platform, "client", None)
        return None

    def resolve_credentials(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None = None,
        platform: Any = None,
    ) -> tuple[str, str]:
        """Resolve (appid, secret) from plugin config or AstrBot platform instance."""
        api_cfg = config.get("qq_official_api", {}) or {}
        appid = str(api_cfg.get("qq_official_appid") or "").strip()
        secret = str(api_cfg.get("qq_official_secret") or "").strip()
        if appid and secret:
            return appid, secret

        candidates: list[Any] = []
        if platform is not None:
            candidates.extend(
                [
                    platform,
                    getattr(platform, "config", None),
                    getattr(platform, "platform_config", None),
                    getattr(platform, "settings", None),
                ]
            )
        bot = self._get_bot_client(event, platform)
        if bot is not None:
            candidates.extend([bot, getattr(bot, "robot", None)])
            api_obj = getattr(bot, "api", None)
            if api_obj is not None:
                http_obj = getattr(api_obj, "_http", None)
                candidates.extend(
                    [
                        http_obj,
                        getattr(http_obj, "token", None) if http_obj else None,
                    ]
                )

        for cand in candidates:
            if cand is None:
                continue
            if not appid:
                found_id = _get_attr_or_key(
                    cand, "appid", "app_id", "appId", "bot_id"
                )
                if found_id:
                    appid = str(found_id).strip()
            if not secret:
                found_sec = _get_attr_or_key(
                    cand, "secret", "app_secret", "client_secret", "clientSecret"
                )
                if found_sec:
                    secret = str(found_sec).strip()
            if appid and secret:
                break

        return appid, secret

    async def _get_access_token(
        self, appid: str, secret: str, force_refresh: bool = False
    ) -> str:
        now = time.monotonic()
        cached = self._token_cache.get(appid)
        if not force_refresh and cached and cached[1] > now:
            return cached[0]

        async with self._token_lock:
            now = time.monotonic()
            cached = self._token_cache.get(appid)
            if not force_refresh and cached and cached[1] > now:
                return cached[0]

            session = await self._ensure_session()
            payload = {"appId": appid, "clientSecret": secret}
            async with session.post(TOKEN_URL, json=payload) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400 or "access_token" not in body:
                    raise RuntimeError(
                        f"获取 QQ 官机 access_token 失败 (HTTP {resp.status}): {body}"
                    )
                token = str(body["access_token"])
                try:
                    expires_in = int(body.get("expires_in", 7200))
                except (TypeError, ValueError):
                    expires_in = 7200
                ttl = max(60, expires_in - 120)
                self._token_cache[appid] = (token, time.monotonic() + ttl)
                return token

    async def _try_botpy_request(
        self,
        event: AstrMessageEvent | None,
        platform: Any,
        method: str,
        path_template: str,
        path_params: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bool, Any, str]:
        """Try executing an OpenAPI call via botpy's authenticated HTTP client."""
        bot = self._get_bot_client(event, platform)
        api_obj = getattr(bot, "api", None) if bot is not None else None
        http_obj = getattr(api_obj, "_http", None) if api_obj is not None else None
        if http_obj is None or not hasattr(http_obj, "request"):
            return False, None, "botpy_http_unavailable"

        try:
            from botpy.http import Route  # type: ignore[import-not-found]
        except Exception:
            return False, None, "botpy_route_unavailable"

        try:
            route = Route(method, path_template, **path_params)
            kwargs: dict[str, Any] = {}
            if json_body is not None:
                kwargs["json"] = json_body
            data = await http_obj.request(route, **kwargs)
            return True, data, ""
        except Exception as exc:
            return False, None, str(exc)

    async def _direct_http_request(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bool, Any, str]:
        """Execute a direct HTTP request against QQ Bot OpenAPI."""
        appid, secret = self.resolve_credentials(config, event=event, platform=platform)
        if not appid or not secret:
            return (
                False,
                None,
                "未获取到 QQ 官方机器人 AppID / AppSecret，请在插件高级配置中手动填写。",
            )

        api_cfg = config.get("qq_official_api", {}) or {}
        base_url = str(api_cfg.get("api_base_url") or DEFAULT_API_BASE).rstrip("/")
        url = f"{base_url}{path}"
        session = await self._ensure_session()

        for attempt in range(2):
            try:
                token = await self._get_access_token(
                    appid, secret, force_refresh=(attempt == 1)
                )
            except Exception as exc:
                return False, None, str(exc)

            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            }
            try:
                async with session.request(
                    method, url, headers=headers, json=json_body
                ) as resp:
                    raw_text = await resp.text()
                    data: Any = None
                    if raw_text:
                        try:
                            data = json.loads(raw_text)
                        except json.JSONDecodeError:
                            data = raw_text

                    if resp.status == 401 and attempt == 0:
                        continue
                    if 200 <= resp.status < 300:
                        return True, data, ""
                    return False, data, f"HTTP {resp.status}: {data}"
            except Exception as exc:
                return False, None, str(exc)

        return False, None, "QQ OpenAPI 请求认证失败"

    async def _request(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        method: str,
        path_template: str,
        path_params: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bool, Any, str]:
        """Perform a QQ Bot OpenAPI request, preferring direct credentials if sandbox/custom."""
        api_cfg = config.get("qq_official_api", {}) or {}
        base_url = str(api_cfg.get("api_base_url") or DEFAULT_API_BASE).rstrip("/")
        debug_log = bool(api_cfg.get("debug_log", False))
        concrete_path = path_template.format(**path_params)

        if base_url == DEFAULT_API_BASE:
            ok, data, err = await self._try_botpy_request(
                event, platform, method, path_template, path_params, json_body
            )
            if ok:
                if debug_log:
                    logger.info(
                        f"[AI-Inspection] botpy API 成功: {method} {concrete_path}"
                    )
                return True, data, ""
            if err not in ("botpy_http_unavailable", "botpy_route_unavailable"):
                if debug_log:
                    logger.warning(
                        f"[AI-Inspection] botpy API 返回异常 ({method} {concrete_path}): {err}"
                    )
                # If credentials exist, avoid double-failing on genuine 4xx permission errors
                # unless botpy failed due to token/client issues.
                if "403" in err or "权限" in err or "112" in err:
                    return False, None, err

        ok, data, err = await self._direct_http_request(
            config, event, platform, method, concrete_path, json_body
        )
        if debug_log:
            if ok:
                logger.info(f"[AI-Inspection] OpenAPI 成功: {method} {concrete_path}")
            else:
                logger.warning(
                    f"[AI-Inspection] OpenAPI 失败 ({method} {concrete_path}): {err}"
                )
        return ok, data, err

    async def recall_group_message(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        group_openid: str,
        message_id: str,
    ) -> tuple[bool, str]:
        """Recall a group message via DELETE /v2/groups/{group_openid}/messages/{message_id}."""
        if not group_openid or not message_id:
            return False, "缺少 group_openid 或 message_id，无法撤回"
        ok, _, err = await self._request(
            config=config,
            event=event,
            platform=platform,
            method="DELETE",
            path_template="/v2/groups/{group_openid}/messages/{message_id}",
            path_params={"group_openid": group_openid, "message_id": message_id},
        )
        return ok, err

    async def mute_group_member(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        group_openid: str,
        member_openid: str,
        mute_minutes: int,
    ) -> tuple[bool, str]:
        """Mute a group member via POST /v2/groups/{group_openid}/restrict_chat_setting."""
        if not group_openid or not member_openid:
            return False, "缺少 group_openid 或 member_openid，无法禁言"

        clamped_minutes = max(1, min(43200, int(mute_minutes)))
        expire_dt = datetime.now(TZ_CN) + timedelta(minutes=clamped_minutes)
        mute_expire_at = expire_dt.isoformat(timespec="seconds")

        payload = {
            "members": [
                {
                    "op": "add",
                    "member_openid": member_openid,
                    "mute_expire_at": mute_expire_at,
                }
            ]
        }
        ok, _, err = await self._request(
            config=config,
            event=event,
            platform=platform,
            method="POST",
            path_template="/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": group_openid},
            json_body=payload,
        )
        return ok, err

    async def unmute_group_member(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        group_openid: str,
        member_openid: str,
    ) -> tuple[bool, str]:
        """Unmute a group member via POST /v2/groups/{group_openid}/restrict_chat_setting."""
        if not group_openid or not member_openid:
            return False, "缺少 group_openid 或 member_openid，无法解禁"

        payload = {
            "members": [
                {
                    "op": "del",
                    "member_openid": member_openid,
                }
            ]
        }
        ok, _, err = await self._request(
            config=config,
            event=event,
            platform=platform,
            method="POST",
            path_template="/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": group_openid},
            json_body=payload,
        )
        return ok, err

    async def send_group_text_message(
        self,
        config: dict[str, Any],
        event: AstrMessageEvent | None,
        platform: Any,
        group_openid: str,
        content: str,
        msg_id: str = "",
        msg_seq: int = 1,
    ) -> tuple[bool, str, str]:
        """Send a group text message directly and return (ok, sent_msg_id, error)."""
        if not group_openid or not content:
            return False, "", "缺少 group_openid 或消息内容"
        payload: dict[str, Any] = {
            "content": content,
            "msg_type": 0,
        }
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq

        ok, data, err = await self._request(
            config=config,
            event=event,
            platform=platform,
            method="POST",
            path_template="/v2/groups/{group_openid}/messages",
            path_params={"group_openid": group_openid},
            json_body=payload,
        )
        sent_id = ""
        if ok and isinstance(data, dict):
            sent_id = str(data.get("id") or data.get("msg_id") or "")
        return ok, sent_id, err
