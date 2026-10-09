#!/usr/bin/env python3
"""Zimmerstack Telegram bot and uptime monitor.

Core PMS commands talk directly to Telegram and Zimmerstack HTTP endpoints.
Light free-text room questions are routed to the same direct PMS commands.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("zimmerstack-bot")
TELEGRAM_LIMIT = 4096
SENSITIVE_KEYS = {
    "password", "passcode", "token", "secret", "authorization", "cookie",
    "phone", "mobile", "email", "address", "aadhaar", "aadhar", "pan",
    "passport", "document", "idproof", "id_proof", "passwordhash",
}

MENU_ACTIONS = {
    "menu_rooms": "rooms",
    "menu_inhouse": "inhouse",
    "menu_upcoming": "upcoming",
    "menu_status": "status",
    "menu_session": "session",
    "menu_edit": "edit",
    "menu_home": "home",
}

# Telegram callback_data max 64 bytes. Keep write tokens short.
# wy:c:<roomId>  confirm mark available (clean)
# wn:c:<roomId>  cancel
# wy:i:<bookingRoomId>:<roomId>  confirm check-in
# wn:i:<bookingRoomId>
# wy:o:<bookingRoomId>  confirm check-out
# wn:o:<bookingRoomId>
# wy:a:<bookingRoomId>:<roomId>  confirm assign
# wn:a:<bookingRoomId>
# wy:b:<roomId>:<yyyymmdd>  confirm block/hold room for one night
# wn:b:<roomId>
# wy:u:<roomBlockId>:<roomId>:<yyyymmdd>  confirm remove room hold
# wn:u:<roomBlockId>
# edit_act:clean|ci|co|assign|home
# edit_clean:<roomId>
# edit_ci:<bookingRoomId>:<roomId>
# edit_co:<bookingRoomId>
# edit_as:<bookingRoomId>  → then room pick
# edit_assign:<bookingRoomId>:<roomId>


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int_set(value: str) -> set[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]
    hermes_chat_ids: set[int]
    alert_chat_id: int | None
    frontend_url: str
    backend_health_url: str
    check_interval_seconds: int
    request_timeout_seconds: int
    failure_threshold: int
    state_file: Path
    rooms_file: Path
    auth_file: Path
    pms_api_base_url: str
    pms_bearer_token: str
    pms_api_key: str
    pms_cookie: str
    pms_property_id: str
    pms_username: str
    pms_password: str
    pms_block_pin: str
    openrouter_api_key: str
    openrouter_model: str
    redact_pii: bool
    audit_file: Path
    booking_drafts_file: Path
    booking_default_price: int
    booking_default_adults: int
    booking_default_children: int
    booking_default_source: str
    booking_default_room_plan: str
    endpoint_rooms: str
    endpoint_inhouse: str
    endpoint_upcoming: str
    endpoint_arrivals: str
    endpoint_departures: str
    endpoint_dues: str

    @classmethod
    def from_env(cls) -> "Config":
        allowed = parse_int_set(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
        allowed_chats = parse_int_set(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        hermes_chats = parse_int_set(os.getenv("HERMES_CHAT_IDS", ""))
        alert_raw = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
        state_file = Path(os.getenv("STATE_FILE", "/var/lib/zimmerstack-bot/state.json"))
        rooms_default = state_file.parent / "rooms.json"
        auth_default = state_file.parent / "auth.json"
        audit_default = state_file.parent / "audit.jsonl"
        drafts_default = state_file.parent / "booking-drafts.json"
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=allowed,
            allowed_chat_ids=allowed_chats,
            hermes_chat_ids=hermes_chats,
            alert_chat_id=int(alert_raw) if alert_raw else (next(iter(allowed)) if len(allowed) == 1 else None),
            frontend_url=os.getenv("FRONTEND_URL", "https://pms.zimmerstack.com/login").strip(),
            backend_health_url=os.getenv("BACKEND_HEALTH_URL", "https://api.zimmerstack.com/api/v1/health").strip(),
            check_interval_seconds=max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))),
            request_timeout_seconds=max(2, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12"))),
            failure_threshold=max(1, int(os.getenv("FAILURE_THRESHOLD", "3"))),
            state_file=state_file,
            rooms_file=Path(os.getenv("ROOMS_FILE", str(rooms_default))),
            auth_file=Path(os.getenv("AUTH_FILE", str(auth_default))),
            pms_api_base_url=os.getenv("PMS_API_BASE_URL", "https://api.zimmerstack.com/api/v1").rstrip("/"),
            pms_bearer_token=os.getenv("PMS_BEARER_TOKEN", "").strip(),
            pms_api_key=os.getenv("PMS_API_KEY", "").strip(),
            pms_cookie=os.getenv("PMS_COOKIE", "").strip(),
            pms_property_id=os.getenv("PMS_PROPERTY_ID", "").strip(),
            pms_username=(os.getenv("PMS_USERNAME") or os.getenv("PMS_EMAIL") or "").strip(),
            pms_password=os.getenv("PMS_PASSWORD", "").strip(),
            pms_block_pin=(
                os.getenv("PMS_BLOCK_PIN")
                or os.getenv("PMS_ROOM_BLOCK_PIN")
                or os.getenv("PMS_PASSCODE")
                or ""
            ).strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
            redact_pii=env_bool("REDACT_PII", True),
            audit_file=Path(os.getenv("AUDIT_FILE", str(audit_default))),
            booking_drafts_file=Path(os.getenv("BOOKING_DRAFTS_FILE", str(drafts_default))),
            booking_default_price=max(0, int(os.getenv("PMS_BOOKING_DEFAULT_PRICE", "0") or "0")),
            booking_default_adults=max(1, int(os.getenv("PMS_BOOKING_DEFAULT_ADULTS", "1") or "1")),
            booking_default_children=max(0, int(os.getenv("PMS_BOOKING_DEFAULT_CHILDREN", "0") or "0")),
            booking_default_source=os.getenv("PMS_BOOKING_DEFAULT_SOURCE", "DIRECT").strip() or "DIRECT",
            booking_default_room_plan=os.getenv("PMS_BOOKING_DEFAULT_ROOM_PLAN", "EP").strip() or "EP",
            endpoint_rooms=os.getenv("PMS_ENDPOINT_ROOMS", "/room/property").strip(),
            endpoint_inhouse=os.getenv("PMS_ENDPOINT_INHOUSE", "/bookingroom/inhouse").strip(),
            endpoint_upcoming=os.getenv("PMS_ENDPOINT_UPCOMING", "/booking/upcoming").strip(),
            endpoint_arrivals=os.getenv("PMS_ENDPOINT_ARRIVALS", "").strip(),
            endpoint_departures=os.getenv("PMS_ENDPOINT_DEPARTURES", "").strip(),
            endpoint_dues=os.getenv("PMS_ENDPOINT_DUES", "").strip(),
        )


@dataclass
class AuthState:
    bearer_token: str = ""
    cookie: str = ""
    path: Path = field(default_factory=lambda: Path("/var/lib/zimmerstack-bot/auth.json"))
    updated_at: str = ""

    @classmethod
    def bootstrap(cls, config: Config) -> "AuthState":
        auth = cls(
            bearer_token=config.pms_bearer_token,
            cookie=config.pms_cookie,
            path=config.auth_file,
        )
        auth.load()
        return auth

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if data.get("bearer_token"):
            self.bearer_token = str(data["bearer_token"]).strip()
        if data.get("cookie"):
            self.cookie = str(data["cookie"]).strip()
        self.updated_at = str(data.get("updated_at") or "")

    def save(self) -> None:
        self.updated_at = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bearer_token": self.bearer_token,
            "cookie": self.cookie,
            "updated_at": self.updated_at,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o640)
        except OSError:
            pass

    def apply_token(self, token: str, set_cookie_header: str = "") -> None:
        token = token.strip()
        if not token:
            raise ValueError("Empty PMS token")
        self.bearer_token = token
        cookie_parts: dict[str, str] = {}
        for part in (self.cookie or "").split(";"):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                cookie_parts[key.strip()] = value.strip()
        cookie_parts["accessToken"] = token
        if set_cookie_header:
            # Take first segment of each Set-Cookie style value if passed joined.
            for raw in set_cookie_header.split(","):
                segment = raw.split(";", 1)[0].strip()
                if "=" in segment:
                    key, value = segment.split("=", 1)
                    if key.strip().lower() in {"ss_session", "accesstoken", "zs_device"}:
                        cookie_parts[key.strip()] = value.strip()
        self.cookie = "; ".join(f"{k}={v}" for k, v in cookie_parts.items() if v)
        self.save()


@dataclass
class Probe:
    name: str
    ok: bool
    status: int | None
    elapsed_ms: int
    detail: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 12,
) -> tuple[int, bytes, dict[str, str], int]:
    started = time.monotonic()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return response.status, body, dict(response.headers.items()), round((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, body, dict(exc.headers.items()), round((time.monotonic() - started) * 1000)


def probe_frontend(config: Config) -> Probe:
    try:
        status, body, _, elapsed = http_request(
            config.frontend_url,
            headers={"User-Agent": "ZimmerstackMonitor/1.0"},
            timeout=config.request_timeout_seconds,
        )
        text = body[:200_000].decode("utf-8", errors="ignore").lower()
        ok = 200 <= status < 400 and ("zimmerstack" in text or "front office" in text or "sign in" in text)
        return Probe("Frontend", ok, status, elapsed, "page marker found" if ok else "unexpected response")
    except Exception as exc:
        return Probe("Frontend", False, None, 0, f"{type(exc).__name__}: {exc}")


def probe_backend(config: Config) -> Probe:
    try:
        status, body, _, elapsed = http_request(
            config.backend_health_url,
            headers={"Accept": "application/json", "User-Agent": "ZimmerstackMonitor/1.0"},
            timeout=config.request_timeout_seconds,
        )
        payload = json.loads(body.decode("utf-8"))
        ok = 200 <= status < 300 and payload.get("ok") is True
        return Probe("Backend API", ok, status, elapsed, "ok=true" if ok else "health JSON did not contain ok=true")
    except Exception as exc:
        return Probe("Backend API", False, None, 0, f"{type(exc).__name__}: {exc}")


def run_health(config: Config) -> list[Probe]:
    return [probe_frontend(config), probe_backend(config)]


def status_text(probes: list[Probe]) -> str:
    lines = ["Zimmerstack status"]
    for probe in probes:
        icon = "UP" if probe.ok else "DOWN"
        status = str(probe.status) if probe.status is not None else "no response"
        lines.append(f"{probe.name}: {icon} | HTTP {status} | {probe.elapsed_ms} ms | {probe.detail}")
    return "\n".join(lines)


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"status": "unknown", "failures": 0, "incident_started_at": None, "session": "unknown"}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def telegram_call(config: Config, method: str, payload: dict[str, Any]) -> Any:
    if not config.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{config.telegram_token}/{method}"
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    status, body, _, _ = http_request(
        url,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=encoded,
        timeout=max(config.request_timeout_seconds, 35),
    )
    response = json.loads(body.decode("utf-8"))
    if status >= 400 or not response.get("ok"):
        raise RuntimeError(f"Telegram {method} failed with HTTP {status}: {response.get('description', 'unknown error')}")
    return response.get("result")


def inline_keyboard() -> str:
    """Buttons attached under a bot message.

    Telegram cannot set real button colors — emoji is the supported way to
    color-code actions in both reply and inline keyboards.
    """
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "📋 Rooms", "callback_data": "menu_rooms"},
                    {"text": "🏠 In-house", "callback_data": "menu_inhouse"},
                ],
                [
                    {"text": "📅 Upcoming", "callback_data": "menu_upcoming"},
                    {"text": "✏️ Edit", "callback_data": "menu_edit"},
                ],
                [
                    {"text": "💚 Status", "callback_data": "menu_status"},
                    {"text": "🔑 Session", "callback_data": "menu_session"},
                ],
                [
                    {"text": "☰ Menu", "callback_data": "menu_home"},
                ],
            ]
        }
    )


def remove_keyboard() -> str:
    """Remove Telegram's persistent bottom keyboard for group-friendly slash commands."""
    return json.dumps({"remove_keyboard": True})


def confirm_keyboard(yes_data: str, no_data: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": yes_data},
                    {"text": "❌ Cancel", "callback_data": no_data},
                ]
            ]
        }
    )


def normalize_button_label(text: str) -> str:
    """Strip leading emoji / symbols so '✏️ Edit' matches 'edit'."""
    value = text.strip().lower()
    while value and not value[0].isalnum() and value[0] not in {"/"}:
        value = value[1:].lstrip()
    return value


def status_emoji(status: str) -> str:
    s = (status or "").upper()
    if s == "DIRTY":
        return "🔴"
    if s in {"AVAILABLE", "CLEAN", "VACANT"}:
        return "🟢"
    if s in {"CHECKEDIN", "OCCUPIED", "INHOUSE"}:
        return "🔵"
    if s in {"OOO", "BLOCKED", "MAINTENANCE", "OWNER_HOLD", "HOLD", "FULL"}:
        return "⚫"
    return "🟡"


def chunk_buttons(buttons: list[dict[str, str]], per_row: int = 2) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for i in range(0, len(buttons), per_row):
        rows.append(buttons[i : i + per_row])
    return rows


# Back-compat alias
def main_keyboard() -> str:
    return inline_keyboard()


BUTTON_TEXT_ACTIONS = {
    "rooms": "rooms",
    "in-house": "inhouse",
    "inhouse": "inhouse",
    "upcoming": "upcoming",
    "edit": "edit",
    "status": "status",
    "session": "session",
    "menu": "home",
    "help": "help",
}


def send_message(
    config: Config,
    chat_id: int,
    text: str,
    *,
    with_menu: bool = True,
    reply_markup: str | None = None,
) -> None:
    for start in range(0, len(text), TELEGRAM_LIMIT - 100):
        chunk = text[start : start + TELEGRAM_LIMIT - 100]
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": chunk,
            "disable_web_page_preview": "true",
        }
        if reply_markup and start == 0:
            payload["reply_markup"] = reply_markup
        elif with_menu and start == 0:
            # Team groups prefer slash commands and inline buttons over the bottom reply keyboard.
            payload["reply_markup"] = remove_keyboard()
        telegram_call(config, "sendMessage", payload)
        # Also attach inline buttons under the first chunk for one-tap loading UX.
        if with_menu and reply_markup is None and start == 0:
            try:
                telegram_call(
                    config,
                    "sendMessage",
                    {
                        "chat_id": str(chat_id),
                        "text": "Quick tap:",
                        "reply_markup": inline_keyboard(),
                        "disable_web_page_preview": "true",
                    },
                )
            except Exception:
                LOG.exception("Failed to send inline keyboard")


def edit_message(config: Config, chat_id: int, message_id: int, text: str, *, with_menu: bool = True) -> None:
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "text": text[: TELEGRAM_LIMIT - 100],
        "disable_web_page_preview": "true",
    }
    if with_menu:
        payload["reply_markup"] = inline_keyboard()
    try:
        telegram_call(config, "editMessageText", payload)
    except RuntimeError as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


def answer_callback(config: Config, callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
        payload["show_alert"] = "false"
    telegram_call(config, "answerCallbackQuery", payload)


def redact(value: Any, enabled: bool) -> Any:
    if not enabled:
        return value
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            cleaned[key] = "[REDACTED]" if any(part in normalized for part in SENSITIVE_KEYS) else redact(item, enabled)
        return cleaned
    if isinstance(value, list):
        return [redact(item, enabled) for item in value]
    return value


def format_payload(title: str, payload: Any) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(rendered) > 3400:
        rendered = rendered[:3400] + "\n... truncated"
    return f"{title}\n{rendered}"


def pms_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def short_date(value: Any) -> str:
    if not value or value == "N/A":
        return "-"
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def india_today() -> datetime.date:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).date()


def compact_ymd(value: str) -> str:
    return value.replace("-", "")


def expand_compact_ymd(value: str) -> str:
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError(f"Bad date token: {value}")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def add_days_ymd(value: str, days: int) -> str:
    base = datetime.strptime(value, "%Y-%m-%d").date()
    return (base + timedelta(days=days)).isoformat()


def room_type_name(room: dict[str, Any]) -> str:
    room_type = room.get("room_type")
    if isinstance(room_type, dict) and room_type.get("name"):
        return str(room_type["name"])
    return str(room.get("roomType") or "-")


def format_pms_rooms(payload: Any, blocks_by_room: dict[int, dict[str, Any]] | None = None) -> str:
    rooms = pms_data(payload)
    if not isinstance(rooms, list):
        return format_payload("Rooms", payload)
    blocks_by_room = blocks_by_room or {}
    lines = [f"Lily's rooms · {len(rooms)} total", ""]
    for room in rooms:
        if not isinstance(room, dict) or room.get("deleted"):
            continue
        number = room.get("roomNumber") or "?"
        status = str(room.get("roomStatus") or "?").upper()
        block = None
        try:
            block = blocks_by_room.get(int(room.get("id") or 0))
        except (TypeError, ValueError):
            block = None
        if block:
            status = f"{room_block_label(block)} / FULL"
        rtype = room_type_name(room)
        booking = room.get("BookingRoom") or []
        guest = ""
        if isinstance(booking, list) and booking and isinstance(booking[0], dict):
            guest = booking[0].get("guestName") or ""
        elif room.get("occupiedBy"):
            guest = str(room.get("occupiedBy"))
        line = f"{number} · {rtype} · {status}"
        if block:
            line += f" · {short_date(block.get('startDate'))}→{short_date(block.get('endDate'))}"
        if guest:
            line += f" · {guest}"
        lines.append(line)
    return "\n".join(lines) if len(lines) > 2 else "No rooms found."


def format_pms_stays(title: str, payload: Any) -> str:
    rows = pms_data(payload)
    if not isinstance(rows, list):
        return format_payload(title, payload)
    lines = [f"{title} · {len(rows)}", ""]
    if not rows:
        lines.append("None right now.")
        return "\n".join(lines)
    for row in rows:
        if not isinstance(row, dict):
            continue
        guest = " ".join(
            part
            for part in [str(row.get("guestName") or "").strip(), str(row.get("guestLastName") or "").strip()]
            if part and part != "N/A"
        ) or "Guest"
        status = str(row.get("bookingStatus") or "?").upper()
        source = str(row.get("source") or "-")
        amount = row.get("totalAmount")
        paid = row.get("totalPaid")
        money = ""
        if amount is not None:
            money = f" · ₹{amount}"
            if paid not in (None, 0, "0"):
                money += f" paid ₹{paid}"
        booking_rooms = row.get("BookingRoom") or []
        if isinstance(booking_rooms, list) and booking_rooms:
            for br in booking_rooms:
                if not isinstance(br, dict):
                    continue
                room_no = br.get("roomNumber") if br.get("roomNumber") not in (None, "", "N/A") else "unassigned"
                rtype = room_type_name(br) if br.get("room_type") or br.get("roomType") else str(br.get("roomType") or "-")
                if rtype in {"NOT_AVAILABLE", "-"} and isinstance(br.get("room_type"), dict):
                    rtype = br["room_type"].get("name") or rtype
                nights = br.get("totalNight")
                check_in = short_date(br.get("checkInDate"))
                check_out = short_date(br.get("checkOutDate"))
                lines.append(
                    f"{guest} · {room_no} · {rtype}\n"
                    f"  {check_in} → {check_out}"
                    + (f" · {nights}n" if nights else "")
                    + f" · {status} · {source}{money}"
                )
        else:
            lines.append(f"{guest} · {status} · {source}{money}")
    return "\n".join(lines)


def pms_url(config: Config, endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("This command is not configured yet; map its PMS endpoint first.")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        url = endpoint
    else:
        url = f"{config.pms_api_base_url}/{endpoint.lstrip('/')}"
    if config.pms_property_id:
        separator = "&" if "?" in url else "?"
        url += separator + urllib.parse.urlencode({"propertyId": config.pms_property_id})
    return url


def pms_headers(config: Config, auth: AuthState) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "ZimmerstackTelegramBot/1.0",
        "Origin": "https://pms.zimmerstack.com",
        "Referer": "https://pms.zimmerstack.com/",
    }
    if auth.bearer_token:
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
    if config.pms_api_key:
        headers["X-API-Key"] = config.pms_api_key
    if auth.cookie:
        headers["Cookie"] = auth.cookie
    if config.pms_property_id:
        headers["X-Property-Id"] = config.pms_property_id
    return headers


def has_pms_auth(auth: AuthState, config: Config) -> bool:
    return bool(auth.bearer_token or config.pms_api_key or auth.cookie)


def pms_get(config: Config, auth: AuthState, endpoint: str) -> Any:
    return pms_request(config, auth, "GET", endpoint)


def pms_request(
    config: Config,
    auth: AuthState,
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    *,
    add_property_query: bool = True,
) -> Any:
    if add_property_query:
        url = pms_url(config, endpoint)
    else:
        endpoint = endpoint.strip()
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        else:
            url = f"{config.pms_api_base_url}/{endpoint.lstrip('/')}"
    headers = pms_headers(config, auth)
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    status, body, _, _ = http_request(
        url,
        method=method,
        headers=headers,
        data=data,
        timeout=config.request_timeout_seconds,
    )
    text = body.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise RuntimeError(f"PMS API returned HTTP {status}: {text[:300]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"response": text[:3000]}


def is_passcode_error(exc: Exception) -> bool:
    text = str(exc)
    return "HTTP 403" in text and ("PASSCODE" in text.upper() or "passcode" in text.lower())


def append_audit(config: Config, entry: dict[str, Any]) -> None:
    config.audit_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": utc_now(), **entry}, ensure_ascii=False, default=str)
    with config.audit_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def room_number_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return digits or text.lower()


def fetch_pms_rooms(config: Config, auth: AuthState) -> list[dict[str, Any]]:
    ensure_session(config, auth)
    payload = pms_get(config, auth, config.endpoint_rooms)
    rooms = pms_data(payload)
    if not isinstance(rooms, list):
        return []
    return [room for room in rooms if isinstance(room, dict) and not room.get("deleted")]


def fetch_room_blocks(config: Config, auth: AuthState, start_ymd: str, end_ymd: str) -> list[dict[str, Any]]:
    ensure_session(config, auth)
    query = urllib.parse.urlencode({"start": start_ymd, "end": end_ymd})
    payload = pms_request(config, auth, "GET", f"/booking-board/blocks?{query}", add_property_query=False)
    blocks = pms_data(payload)
    if not isinstance(blocks, list):
        return []
    return [block for block in blocks if isinstance(block, dict)]


def block_room_id(block: dict[str, Any]) -> int | None:
    value = block.get("roomId")
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def block_identifier(block: dict[str, Any]) -> int | None:
    for key in ("roomBlockId", "id"):
        value = block.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def room_block_label(block: dict[str, Any]) -> str:
    btype = str(block.get("blockType") or "HOLD").upper()
    if btype == "OWNER_HOLD":
        return "HOLD"
    if btype == "OUTOFORDER":
        return "OUT OF ORDER"
    return btype


def active_blocks_by_room(config: Config, auth: AuthState, start_ymd: str, end_ymd: str) -> dict[int, dict[str, Any]]:
    blocks: dict[int, dict[str, Any]] = {}
    for block in fetch_room_blocks(config, auth, start_ymd, end_ymd):
        rid = block_room_id(block)
        if rid is not None:
            blocks[rid] = block
    return blocks


def find_room_block(config: Config, auth: AuthState, room_id: int, start_ymd: str, end_ymd: str) -> dict[str, Any] | None:
    for block in fetch_room_blocks(config, auth, start_ymd, end_ymd):
        if block_room_id(block) == room_id:
            return block
    return None


def find_pms_room(rooms: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    q_key = room_number_key(q)
    for room in rooms:
        rid = str(room.get("id") or "")
        number = str(room.get("roomNumber") or "")
        if q == rid or q == number.lower() or (q_key and q_key == room_number_key(number)):
            return room
    for room in rooms:
        number = str(room.get("roomNumber") or "").lower()
        if q in number:
            return room
    return None


def guest_label(row: dict[str, Any]) -> str:
    return (
        " ".join(
            part
            for part in [str(row.get("guestName") or "").strip(), str(row.get("guestLastName") or "").strip()]
            if part and part != "N/A"
        )
        or "Guest"
    )


def iter_booking_rooms(payload: Any) -> list[dict[str, Any]]:
    rows = pms_data(payload)
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        booking_rooms = row.get("BookingRoom")
        if isinstance(booking_rooms, list) and booking_rooms:
            for br in booking_rooms:
                if not isinstance(br, dict):
                    continue
                item = dict(br)
                item["_guest"] = guest_label(row)
                item["_bookingId"] = row.get("id")
                item["_bookingStatus"] = row.get("bookingStatus")
                out.append(item)
        else:
            # in-house endpoint may already be booking-room shaped
            if row.get("id") and (row.get("checkInDate") or row.get("roomNumber") or row.get("status")):
                item = dict(row)
                item["_guest"] = guest_label(row)
                item["_bookingId"] = row.get("bookingId") or row.get("id")
                item["_bookingStatus"] = row.get("status") or row.get("bookingStatus")
                out.append(item)
    return out


def resolve_checkin_room_id(br: dict[str, Any]) -> int | None:
    for key in ("roomId", "preAssignRoomId", "physicalRoomId"):
        value = br.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def pms_mark_available(config: Config, auth: AuthState, room_id: int) -> Any:
    return pms_request(
        config,
        auth,
        "PATCH",
        f"/roomops/room/{room_id}/available",
        None,
        add_property_query=False,
    )


def pms_checkin(config: Config, auth: AuthState, booking_room_id: int, room_id: int) -> Any:
    return pms_request(
        config,
        auth,
        "POST",
        "/roomops/checkin",
        {"bookingRoomId": booking_room_id, "roomId": room_id},
        add_property_query=False,
    )


def pms_checkout(config: Config, auth: AuthState, booking_room_id: int) -> Any:
    return pms_request(
        config,
        auth,
        "POST",
        "/roomops/checkout",
        {"id": booking_room_id},
        add_property_query=False,
    )


def pms_collect_booking_payment(
    config: Config,
    auth: AuthState,
    booking_id: int,
    amount: float,
    *,
    remarks: str = "Telegram cash collection",
) -> Any:
    payload: dict[str, Any] = {
        "bookingId": booking_id,
        "amount": amount,
        "paymentMode": "CASH",
        "transactionId": "",
        "remarks": remarks,
    }
    return pms_request(config, auth, "POST", "/payment", payload, add_property_query=False)


def pms_assign(config: Config, auth: AuthState, booking_room_id: int, room_id: int) -> Any:
    return pms_request(
        config,
        auth,
        "POST",
        "/booking-board/assign",
        {"bookingRoomId": booking_room_id, "roomId": room_id},
        add_property_query=False,
    )


def pms_block_room(
    config: Config,
    auth: AuthState,
    room_id: int,
    start_ymd: str,
    end_ymd: str,
    *,
    reason: str = "Telegram: full/hold today",
    block_type: str = "OWNER_HOLD",
) -> Any:
    return pms_request(
        config,
        auth,
        "POST",
        "/booking-board/blocks",
        {
            "roomId": room_id,
            "startDate": start_ymd,
            "endDate": end_ymd,
            "blockType": block_type,
            "reason": reason,
            **({"pin": config.pms_block_pin} if config.pms_block_pin else {}),
        },
        add_property_query=False,
    )


def pms_unblock_room(config: Config, auth: AuthState, room_block_id: int) -> Any:
    return pms_request(
        config,
        auth,
        "DELETE",
        f"/booking-board/blocks/{room_block_id}",
        {"pin": config.pms_block_pin} if config.pms_block_pin else {},
        add_property_query=False,
    )


def pms_cancel_booking(config: Config, auth: AuthState, booking_id: int) -> Any:
    payload: dict[str, Any] = {"data": {}}
    if config.pms_block_pin:
        payload["data"]["passcode"] = config.pms_block_pin
    return pms_request(
        config,
        auth,
        "PATCH",
        f"/booking/cancel/{booking_id}",
        payload,
        add_property_query=False,
    )


def booking_room_booking_id(row: dict[str, Any]) -> int | None:
    for key in ("_bookingId", "bookingId"):
        value = row.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def booking_room_id(row: dict[str, Any]) -> int | None:
    value = row.get("id")
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def money_value(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^0-9.-]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def booking_due_amount(row: dict[str, Any]) -> float | None:
    booking = row.get("booking") if isinstance(row.get("booking"), dict) else {}
    candidates = (
        "balance",
        "due",
        "dueAmount",
        "balanceAmount",
        "pendingAmount",
        "amountDue",
        "totalDue",
        "remainingAmount",
    )
    for key in candidates:
        value = money_value(row.get(key))
        if value is not None:
            return max(value, 0.0)
        value = money_value(booking.get(key))
        if value is not None:
            return max(value, 0.0)
    totals = ("totalAmount", "bookingAmount", "grandTotal", "total", "roomTariff", "tariff")
    paid_keys = ("paidAmount", "totalPaid", "amountPaid", "paid")
    total = next((money_value(row.get(k)) for k in totals if money_value(row.get(k)) is not None), None)
    if total is None:
        total = next((money_value(booking.get(k)) for k in totals if money_value(booking.get(k)) is not None), None)
    paid = next((money_value(row.get(k)) for k in paid_keys if money_value(row.get(k)) is not None), None)
    if paid is None:
        paid = next((money_value(booking.get(k)) for k in paid_keys if money_value(booking.get(k)) is not None), 0.0)
    if total is None:
        return None
    return max(total - (paid or 0.0), 0.0)


def format_money(amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"₹{int(round(amount))}"
    return f"₹{amount:.2f}"


def find_inhouse_booking_room(inhouse: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    q_num = first_number_token(q) or q
    q_key = room_number_key(q_num)
    best: dict[str, Any] | None = None
    for br in inhouse:
        brid = str(br.get("id") or "")
        room_number = str(br.get("roomNumber") or "")
        hay = booking_room_query_text(br)
        if q == brid or q_num == brid or (q_key and q_key == room_number_key(room_number)):
            return br
        if q in hay or q_num in hay:
            best = best or br
    return best


def booking_room_query_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "id",
        "_bookingId",
        "bookingId",
        "roomNumber",
        "roomId",
        "preAssignRoomId",
        "physicalRoomId",
        "_guest",
        "guestName",
        "guestLastName",
        "status",
        "_bookingStatus",
    ):
        value = row.get(key)
        if value not in (None, "", "N/A"):
            parts.append(str(value))
    number_key = room_number_key(row.get("roomNumber"))
    if number_key:
        parts.append(number_key)
    return " ".join(parts).lower()


def find_cancel_booking_target(upcoming: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    q_num = first_number_token(q)
    if q_num:
        for row in upcoming:
            bid = booking_room_booking_id(row)
            if bid is not None and str(bid) == q_num:
                return row
        for row in upcoming:
            brid = booking_room_id(row)
            if brid is not None and str(brid) == q_num:
                return row
        q_key = room_number_key(q_num)
        for row in upcoming:
            room_number = str(row.get("roomNumber") or "")
            ids = [row.get("roomId"), row.get("preAssignRoomId"), row.get("physicalRoomId")]
            if q_key and q_key == room_number_key(room_number):
                return row
            if any(str(value) == q_num for value in ids if value not in (None, "", "N/A")):
                return row
    for row in upcoming:
        if q and q in booking_room_query_text(row):
            return row
    return None


def explain_cancel_booking_miss(rooms: list[dict[str, Any]], query: str) -> str:
    room = find_pms_room(rooms, query)
    if room is None:
        return f"Booking not found for: {query}. Send an upcoming booking ID, room number, or guest name."
    number = room.get("roomNumber") or query
    status = str(room.get("roomStatus") or "?").upper()
    if status in {"OCCUPIED", "CHECKEDIN", "INHOUSE"}:
        return (
            f"{number} is currently {status}. This is not an upcoming booking, so booking cancel will not find it. "
            f"To remove the in-house guest, send checkout {room_number_key(number) or query}. To cancel a future booking, send the upcoming booking ID, room number, or guest name."
        )
    return (
        f"Room {number} is {status}, but no upcoming guest booking was found. "
        "To cancel a booking, send an upcoming booking ID, room number, or guest name."
    )


def format_booking_cancel_target(row: dict[str, Any]) -> str:
    guest = row.get("_guest") or "Guest"
    booking_id = booking_room_booking_id(row)
    brid = booking_room_id(row)
    room = row.get("roomNumber") or row.get("preAssignRoomId") or row.get("roomId") or "unassigned"
    status = str(row.get("status") or row.get("_bookingStatus") or "UPCOMING").upper()
    check_in = short_date(row.get("checkInDate") or row.get("checkIn"))
    check_out = short_date(row.get("checkOutDate") or row.get("checkOut"))
    return (
        "Cancel guest booking?\n"
        f"Guest: {guest}\n"
        f"Booking: {booking_id or '-'} · bookingRoom: {brid or '-'}\n"
        f"Room: {room}\n"
        f"Stay: {check_in} → {check_out}\n"
        f"Status: {status}"
    )


def is_booking_cancel_text(text: str) -> bool:
    normalized = text.lower()
    if "/cancelbooking" in normalized or "/cancel-booking" in normalized:
        return True
    if not text_has_any(normalized, {"cancel", "cancle", "delete", "remove", "hata", "hatao"}):
        return False
    if text_has_any(normalized, {"hold", "block", "unblock", "free"}):
        return False
    return text_has_any(normalized, {"booking", "book", "guest", "reservation"})


@dataclass
class BookingDraft:
    draft_id: str
    room_id: int
    room_number: str
    room_type_id: int
    room_type_name: str
    guest_name: str
    guest_last_name: str
    phone: str
    check_in: str
    check_out: str
    tariff: int
    adults: int
    children: int
    source: str
    room_plan: str
    created_at: str = field(default_factory=utc_now)


def booking_help_text(config: Config) -> str:
    today = india_today().isoformat()
    tomorrow = add_days_ymd(today, 1)
    price = config.booking_default_price or "room default"
    return (
        "Send a guest booking form:\n"
        "/book room=102 name=Rahul phone=9876543210 "
        f"checkin={today} checkout={tomorrow} price={price} adults=1\n\n"
        "Short format also works:\n"
        "book 102 Rahul 9876543210 2500\n\n"
        "Defaults: check-in today, check-out tomorrow, source DIRECT, room plan EP, adults 1."
    )


def booking_form_keyboard(room_query: str | None = None) -> str:
    rows = [[{"text": "📋 Rooms", "callback_data": "menu_rooms"}]]
    if room_query:
        today = compact_ymd(india_today().isoformat())
        rows[0].append({"text": "🟠 Hold instead", "callback_data": f"book_hold:{room_query}:{today}"})
    rows.append([{"text": "☰ Menu", "callback_data": "menu_home"}])
    return json.dumps({"inline_keyboard": rows})


def parse_date_token(value: str, *, today: datetime.date | None = None) -> str | None:
    text = value.strip().lower()
    today = today or india_today()
    if text in {"today", "aaj", "aj"}:
        return today.isoformat()
    if text in {"tomorrow", "kal", "tmrw"}:
        return (today + timedelta(days=1)).isoformat()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def split_guest_name(value: str) -> tuple[str, str]:
    parts = [p for p in value.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def room_type_id_from_room(room: dict[str, Any]) -> int | None:
    for key in ("room_typeId", "roomTypeId"):
        value = room.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    room_type = room.get("room_type")
    if isinstance(room_type, dict) and room_type.get("id") not in (None, "", "N/A"):
        try:
            return int(room_type["id"])
        except (TypeError, ValueError):
            return None
    return None


def room_default_tariff(room: dict[str, Any]) -> int:
    room_type = room.get("room_type")
    if isinstance(room_type, dict):
        for key in ("defaultTariff", "basePrice", "floorPrice"):
            try:
                value = int(float(room_type.get(key) or 0))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return 0


BOOKING_KV_KEYS = "room|name|guest|phone|mobile|checkin|checkout|in|out|date|price|tariff|adults|adult|children|child|source|plan"


def parse_key_value_tokens(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = rf"\b({BOOKING_KV_KEYS})\s*=\s*(.+?)(?=\s+\b(?:{BOOKING_KV_KEYS})\s*=|[,\n]|$)"
    for match in re.finditer(pattern, text, re.I):
        key = match.group(1).lower()
        values[key] = match.group(2).strip()
    return values


def parse_booking_request(config: Config, text: str, rooms: list[dict[str, Any]]) -> tuple[BookingDraft | None, str | None]:
    raw = text.strip()
    body = re.sub(r"^/book(?:ing)?(?:@\w+)?\s*", "", raw, flags=re.I).strip()
    body = re.sub(r"\b(booking|book|reserve|guest booking|new booking|banao|banana|kar do|kr do|kardo)\b", " ", body, flags=re.I)
    kv = parse_key_value_tokens(body)
    room_query = kv.get("room") or first_number_token(body)
    if not room_query:
        return None, "Room number missing."
    room = find_pms_room(rooms, room_query)
    if room is None:
        return None, f"Room not found: {room_query}"
    room_type_id = room_type_id_from_room(room)
    if room_type_id is None:
        return None, f"Room type id not found for {room.get('roomNumber') or room_query}."

    phone_match = re.search(r"(?<!\d)(?:\+?91[- ]?)?([6-9]\d{9})(?!\d)", body)
    phone = re.sub(r"\D", "", kv.get("phone") or kv.get("mobile") or (phone_match.group(1) if phone_match else ""))[-10:]

    name_value = kv.get("name") or kv.get("guest") or ""
    if not name_value and phone_match:
        before_phone = body[: phone_match.start()].strip(" ,.-")
        before_phone = re.sub(r"\b(room|price|tariff|checkin|checkout|adults|children|source|plan)\b\s*=\s*\S+", " ", before_phone, flags=re.I)
        before_phone = re.sub(r"\b\d{2,6}\b", " ", before_phone, count=1)
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z.'-]*", before_phone) if w.lower() not in {"book", "booking", "room", "guest", "for", "today", "aaj", "kal", "price", "rs", "in", "out"}]
        if words:
            name_value = " ".join(words[-3:])
    guest_name, guest_last_name = split_guest_name(name_value)

    today = india_today()
    check_in = parse_date_token(kv.get("checkin") or kv.get("in") or kv.get("date") or "aaj", today=today)
    check_out = parse_date_token(kv.get("checkout") or kv.get("out") or "kal", today=today)
    if not check_in or not check_out:
        return None, "I could not understand the date. Use YYYY-MM-DD."
    if check_out <= check_in:
        return None, "Check-out must be after check-in."

    price_text = kv.get("price") or kv.get("tariff") or ""
    price_match = re.search(r"(?:₹|rs\.?|price\s*)?\b(\d{3,6})\b", price_text, re.I) if price_text else None
    if not price_match:
        price_scan = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " ", body)
        candidates = [int(x) for x in re.findall(r"\b\d{3,6}\b", price_scan) if x != room_number_key(room.get("roomNumber")) and x != str(room.get("id"))]
        price = next((x for x in candidates if x >= 1000), 0)
    else:
        price = int(price_match.group(1))
    if price <= 0:
        price = config.booking_default_price or room_default_tariff(room)

    try:
        adults = max(1, int(kv.get("adults") or kv.get("adult") or config.booking_default_adults))
    except ValueError:
        adults = config.booking_default_adults
    try:
        children = max(0, int(kv.get("children") or kv.get("child") or config.booking_default_children))
    except ValueError:
        children = config.booking_default_children

    missing = []
    if not guest_name:
        missing.append("guest name")
    if len(phone) != 10:
        missing.append("10-digit phone")
    if price <= 0:
        missing.append("price")
    if missing:
        return None, "Missing: " + ", ".join(missing)

    return BookingDraft(
        draft_id=secrets.token_urlsafe(6),
        room_id=int(room["id"]),
        room_number=str(room.get("roomNumber") or room_query),
        room_type_id=room_type_id,
        room_type_name=room_type_name(room),
        guest_name=guest_name,
        guest_last_name=guest_last_name,
        phone=phone,
        check_in=check_in,
        check_out=check_out,
        tariff=price,
        adults=adults,
        children=children,
        source=(kv.get("source") or config.booking_default_source).upper(),
        room_plan=(kv.get("plan") or config.booking_default_room_plan).upper(),
    ), None


def format_booking_draft(draft: BookingDraft) -> str:
    name = " ".join(p for p in [draft.guest_name, draft.guest_last_name] if p)
    return (
        "Create guest booking?\n"
        f"Guest: {name}\n"
        f"Phone: {draft.phone}\n"
        f"Room: {draft.room_number} · {draft.room_type_name}\n"
        f"Stay: {draft.check_in} → {draft.check_out}\n"
        f"Price: ₹{draft.tariff} · Adults: {draft.adults} · Children: {draft.children}\n"
        f"Source: {draft.source} · Plan: {draft.room_plan}"
    )


def load_booking_drafts(config: Config) -> dict[str, Any]:
    try:
        data = json.loads(config.booking_drafts_file.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_booking_drafts(config: Config, drafts: dict[str, Any]) -> None:
    config.booking_drafts_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.booking_drafts_file.with_suffix(config.booking_drafts_file.suffix + ".tmp")
    tmp.write_text(json.dumps(drafts, indent=2, ensure_ascii=False, default=str) + "\n")
    tmp.replace(config.booking_drafts_file)
    try:
        os.chmod(config.booking_drafts_file, 0o640)
    except OSError:
        pass


def save_booking_draft(config: Config, draft: BookingDraft) -> None:
    drafts = load_booking_drafts(config)
    drafts[draft.draft_id] = asdict(draft)
    save_booking_drafts(config, drafts)


def pop_booking_draft(config: Config, draft_id: str) -> BookingDraft | None:
    drafts = load_booking_drafts(config)
    data = drafts.pop(draft_id, None)
    if data is None:
        return None
    save_booking_drafts(config, drafts)
    try:
        return BookingDraft(**data)
    except TypeError:
        return None


def pms_create_guest(config: Config, auth: AuthState, draft: BookingDraft) -> int:
    result = pms_request(
        config,
        auth,
        "POST",
        "/guest",
        {
            "salutation": None,
            "name": draft.guest_name,
            "lastName": draft.guest_last_name or None,
            "phoneNumber": draft.phone,
            "idProof": "AADHAR",
            "tariffState": "DYNAMIC",
        },
        add_property_query=False,
    )
    data = pms_data(result)
    if isinstance(data, dict) and data.get("id") not in (None, "", "N/A"):
        return int(data["id"])
    raise RuntimeError("PMS guest create did not return guest id")


def pms_create_booking(config: Config, auth: AuthState, draft: BookingDraft, guest_id: int) -> Any:
    payload = {
        "guestId": str(guest_id),
        **({"propertyId": int(config.pms_property_id)} if str(config.pms_property_id).isdigit() else {}),
        "source": draft.source,
        "salutation": "",
        "guestName": draft.guest_name,
        "guestLastName": draft.guest_last_name,
        "isPractice": False,
        "roomsArray": [
            {
                "tariff": int(draft.tariff),
                "occupancy": max(1, int(draft.adults)),
                "children": max(0, int(draft.children)),
                "checkIn": draft.check_in,
                "checkOut": draft.check_out,
                "roomPlan": draft.room_plan,
                "roomTypeId": draft.room_type_id,
            }
        ],
    }
    return pms_request(config, auth, "POST", "/booking", payload, add_property_query=False)


def extract_booking_id(result: Any) -> int | None:
    if isinstance(result, dict):
        for path in (("booking", "id"), ("data", "booking", "id"), ("data", "id"), ("bookingId",), ("data", "bookingId")):
            value: Any = result
            for key in path:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(key)
            if value not in (None, "", "N/A"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
    return None


def find_booking_room_for_booking(config: Config, auth: AuthState, booking_id: int, room_type_id: int) -> int | None:
    upcoming = iter_booking_rooms(pms_get(config, auth, config.endpoint_upcoming))
    for br in upcoming:
        try:
            if int(br.get("_bookingId") or br.get("bookingId") or 0) != booking_id:
                continue
            if int(br.get("roomTypeId") or 0) == int(room_type_id):
                return int(br["id"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


def pms_create_guest_booking(config: Config, auth: AuthState, draft: BookingDraft) -> tuple[int | None, int | None]:
    ensure_session(config, auth)
    guest_id = pms_create_guest(config, auth, draft)
    result = pms_create_booking(config, auth, draft, guest_id)
    booking_id = extract_booking_id(result)
    booking_room_id = None
    if booking_id is not None:
        booking_room_id = find_booking_room_for_booking(config, auth, booking_id, draft.room_type_id)
        if booking_room_id is not None:
            pms_assign(config, auth, booking_room_id, draft.room_id)
    return booking_id, booking_room_id


def ask_booking_confirm(config: Config, auth: AuthState, chat_id: int, text: str) -> None:
    rooms = fetch_pms_rooms(config, auth)
    draft, error = parse_booking_request(config, text, rooms)
    if draft is None:
        room_query = first_number_token(text)
        send_message(
            config,
            chat_id,
            (error + "\n\n" if error else "") + booking_help_text(config),
            with_menu=False,
            reply_markup=booking_form_keyboard(room_query),
        )
        return
    save_booking_draft(config, draft)
    send_message(
        config,
        chat_id,
        format_booking_draft(draft),
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:g:{draft.draft_id}", f"wn:g:{draft.draft_id}"),
    )


def ask_cancel_booking_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    ensure_session(config, auth)
    upcoming = iter_booking_rooms(pms_get(config, auth, config.endpoint_upcoming))
    target = find_cancel_booking_target(upcoming, query)
    if target is None:
        rooms = fetch_pms_rooms(config, auth)
        send_message(
            config,
            chat_id,
            explain_cancel_booking_miss(rooms, query),
            with_menu=True,
        )
        return
    booking_id = booking_room_booking_id(target)
    if booking_id is None:
        send_message(config, chat_id, "Booking found, but PMS did not return a booking ID. Please send the booking ID from Upcoming.", with_menu=True)
        return
    send_message(
        config,
        chat_id,
        format_booking_cancel_target(target),
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:bc:{booking_id}", f"wn:bc:{booking_id}"),
    )


def edit_home_keyboard(rooms: list[dict[str, Any]], checkins: list[tuple[dict[str, Any], int]], checkouts: list[dict[str, Any]]) -> str:
    """Step 1: pick what to do (emoji = color coding; Telegram has no real colors)."""
    dirty_n = sum(1 for r in rooms if str(r.get("roomStatus") or "").upper() == "DIRTY")
    rows = [
        [
            {"text": f"🟢 Clean room ({dirty_n} dirty)", "callback_data": "edit_act:clean"},
        ],
        [
            {"text": f"🔵 Check-in ({len(checkins)})", "callback_data": "edit_act:ci"},
            {"text": f"🟠 Check-out ({len(checkouts)})", "callback_data": "edit_act:co"},
        ],
        [
            {"text": "🟣 Assign room", "callback_data": "edit_act:assign"},
        ],
        [
            {"text": "📋 Rooms", "callback_data": "menu_rooms"},
            {"text": "☰ Menu", "callback_data": "menu_home"},
        ],
    ]
    return json.dumps({"inline_keyboard": rows})


def edit_clean_room_keyboard(rooms: list[dict[str, Any]]) -> str:
    """Step 2 for clean: pick which room."""
    buttons: list[dict[str, str]] = []
    # Dirty first, then others
    ordered = sorted(
        rooms,
        key=lambda r: (0 if str(r.get("roomStatus") or "").upper() == "DIRTY" else 1, str(r.get("roomNumber") or "")),
    )
    for room in ordered[:12]:
        rid = room.get("id")
        if rid is None:
            continue
        status = str(room.get("roomStatus") or "?").upper()
        num = room.get("roomNumber") or rid
        buttons.append(
            {
                "text": f"{status_emoji(status)} {num}",
                "callback_data": f"edit_clean:{rid}",
            }
        )
    rows = chunk_buttons(buttons, 2)
    rows.append([{"text": "⬅️ Back", "callback_data": "edit_act:home"}, {"text": "❌ Cancel", "callback_data": "wn:x"}])
    return json.dumps({"inline_keyboard": rows})


def edit_checkin_keyboard(checkins: list[tuple[dict[str, Any], int]]) -> str:
    rows: list[list[dict[str, str]]] = []
    if not checkins:
        rows.append([{"text": "No assigned arrivals", "callback_data": "edit_act:home"}])
    for br, room_id in checkins[:8]:
        brid = br.get("id")
        if brid is None:
            continue
        guest = br.get("_guest") or "Guest"
        room_no = br.get("roomNumber") if br.get("roomNumber") not in (None, "", "N/A") else room_id
        rows.append(
            [{"text": f"🔵 {guest} → {room_no}", "callback_data": f"edit_ci:{brid}:{room_id}"}]
        )
    rows.append([{"text": "⬅️ Back", "callback_data": "edit_act:home"}])
    return json.dumps({"inline_keyboard": rows})


def edit_checkout_keyboard(checkouts: list[dict[str, Any]]) -> str:
    rows: list[list[dict[str, str]]] = []
    if not checkouts:
        rows.append([{"text": "No in-house guests", "callback_data": "edit_act:home"}])
    for br in checkouts[:8]:
        brid = br.get("id")
        if brid is None:
            continue
        guest = br.get("_guest") or "Guest"
        room_no = br.get("roomNumber") if br.get("roomNumber") not in (None, "", "N/A") else brid
        rows.append([{"text": f"🟠 {guest} · {room_no}", "callback_data": f"edit_co:{brid}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "edit_act:home"}])
    return json.dumps({"inline_keyboard": rows})


def edit_assign_booking_keyboard(upcoming: list[dict[str, Any]]) -> str:
    rows: list[list[dict[str, str]]] = []
    candidates = [br for br in upcoming if resolve_checkin_room_id(br) is None or br.get("roomId") in (None, "", "N/A")]
    # Also include pre-assigned so staff can re-assign
    if not candidates:
        candidates = upcoming[:8]
    if not candidates:
        rows.append([{"text": "No upcoming stays", "callback_data": "edit_act:home"}])
    for br in candidates[:8]:
        brid = br.get("id")
        if brid is None:
            continue
        guest = br.get("_guest") or "Guest"
        room_no = br.get("roomNumber") if br.get("roomNumber") not in (None, "", "N/A") else "unassigned"
        rows.append([{"text": f"🟣 {guest} · {room_no}", "callback_data": f"edit_as:{brid}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "edit_act:home"}])
    return json.dumps({"inline_keyboard": rows})


def edit_assign_room_keyboard(booking_room_id: int, rooms: list[dict[str, Any]]) -> str:
    buttons: list[dict[str, str]] = []
    for room in rooms[:12]:
        rid = room.get("id")
        if rid is None:
            continue
        status = str(room.get("roomStatus") or "?").upper()
        if status not in {"AVAILABLE", "CLEAN", "VACANT", "DIRTY"}:
            continue
        num = room.get("roomNumber") or rid
        buttons.append(
            {
                "text": f"{status_emoji(status)} {num}",
                "callback_data": f"edit_assign:{booking_room_id}:{rid}",
            }
        )
    rows = chunk_buttons(buttons, 2)
    rows.append([{"text": "⬅️ Back", "callback_data": "edit_act:assign"}])
    return json.dumps({"inline_keyboard": rows})


# Back-compat alias used by older call sites
def edit_menu_keyboard(rooms: list[dict[str, Any]], checkins: list[tuple[dict[str, Any], int]], checkouts: list[dict[str, Any]]) -> str:
    return edit_home_keyboard(rooms, checkins, checkouts)


def format_edit_menu(rooms: list[dict[str, Any]], checkins: list[tuple[dict[str, Any], int]], checkouts: list[dict[str, Any]]) -> str:
    dirty_n = sum(1 for r in rooms if str(r.get("roomStatus") or "").upper() == "DIRTY")
    lines = [
        "✏️ Edit / Update",
        "Choose an action, then pick a room or guest.",
        "Every write needs ✅ Confirm.",
        "",
        f"🟢 Rooms: {len(rooms)} · 🔴 Dirty: {dirty_n}",
        f"🔵 Ready check-in: {len(checkins)}",
        f"🟠 In-house check-out: {len(checkouts)}",
        "",
        "🟢 Clean = mark AVAILABLE",
        "🔵 Check-in · 🟠 Check-out · 🟣 Assign",
        "",
        "Colors are emoji only (Telegram does not support real button colors).",
    ]
    return "\n".join(lines)


def pms_login(config: Config, auth: AuthState) -> str:
    if not config.pms_username or not config.pms_password:
        raise RuntimeError("PMS_USERNAME/PMS_PASSWORD not set on VPS — cannot auto-refresh session")
    url = f"{config.pms_api_base_url}/subuser/login"
    payload = json.dumps({"username": config.pms_username, "password": config.pms_password}).encode("utf-8")
    status, body, headers, _ = http_request(
        url,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://pms.zimmerstack.com",
            "Referer": "https://pms.zimmerstack.com/",
            "User-Agent": "ZimmerstackTelegramBot/1.0",
        },
        data=payload,
        timeout=config.request_timeout_seconds,
    )
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Login returned non-JSON HTTP {status}: {text[:200]}") from exc
    message = str(data.get("message") or data.get("status") or "")
    if message == "2FA_REQUIRED" or data.get("statuscode") == "2FA_REQUIRED":
        raise RuntimeError("Login needs 2FA/OTP — disable 2FA for bot user or refresh cookie manually")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Login failed HTTP {status}: {message or text[:200]}")
    token = None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if isinstance(inner, dict):
        token = inner.get("token") or inner.get("accessToken")
    token = token or data.get("token")
    if not token:
        raise RuntimeError(f"Login OK but no token in response: keys={list(data.keys())}")
    set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    auth.apply_token(str(token), set_cookie)
    LOG.info("PMS session refreshed via login")
    return "Session refreshed via PMS login"


def check_session(config: Config, auth: AuthState, *, refresh: bool = True) -> str:
    if not has_pms_auth(auth, config):
        if refresh and config.pms_username and config.pms_password:
            try:
                detail = pms_login(config, auth)
                return f"Session: RESTORED\n{detail}"
            except Exception as exc:
                return f"Session: MISSING\nAuto-login failed: {exc}"
        return "Session: MISSING\nSet PMS_COOKIE or PMS_USERNAME+PMS_PASSWORD on VPS."
    try:
        payload = pms_get(config, auth, "/subuser/me")
        me = pms_data(payload)
        name = ""
        if isinstance(me, dict):
            name = " ".join(
                part for part in [str(me.get("firstName") or "").strip(), str(me.get("lastName") or "").strip()] if part
            ) or str(me.get("username") or "staff")
            prop = me.get("property") if isinstance(me.get("property"), dict) else {}
            prop_name = prop.get("name") if isinstance(prop, dict) else ""
            prop_id = me.get("propertyId") or (prop.get("id") if isinstance(prop, dict) else "")
            lines = [
                "Session: ACTIVE",
                f"User: {name}",
                f"Property: {prop_name or '-'} ({prop_id or config.pms_property_id or '-'})",
            ]
            if auth.updated_at:
                lines.append(f"Auth file: {auth.updated_at}")
            if config.pms_username:
                lines.append("Auto-refresh: login credentials configured")
            else:
                lines.append("Auto-refresh: OFF (set PMS_USERNAME + PMS_PASSWORD for week-off safety)")
            return "\n".join(lines)
        return "Session: ACTIVE"
    except RuntimeError as exc:
        if "401" not in str(exc) and "Unauthorized" not in str(exc):
            return f"Session: ERROR\n{exc}"
        if refresh and config.pms_username and config.pms_password:
            try:
                detail = pms_login(config, auth)
                return f"Session: RESTORED after expiry\n{detail}"
            except Exception as login_exc:
                return f"Session: EXPIRED\nAuto-login failed: {login_exc}"
        return "Session: EXPIRED\nRe-login on PMS and update cookie, or set PMS_USERNAME+PMS_PASSWORD."


def ensure_session(config: Config, auth: AuthState) -> None:
    """Best-effort refresh before PMS reads."""
    if not has_pms_auth(auth, config):
        if config.pms_username and config.pms_password:
            pms_login(config, auth)
        return
    try:
        pms_get(config, auth, "/subuser/me")
    except RuntimeError as exc:
        if "401" in str(exc) or "Unauthorized" in str(exc):
            if config.pms_username and config.pms_password:
                pms_login(config, auth)
            else:
                raise
        else:
            raise


ROOM_STATUSES = {"vacant", "dirty", "clean", "occupied", "ooo", "booked", "hold"}


def load_rooms(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"property": "Desk board", "rooms": [], "bookings": []}
    if "rooms" not in data or not isinstance(data["rooms"], list):
        data["rooms"] = []
    for room in data["rooms"]:
        room.setdefault("status", "vacant")
        room.setdefault("note", "")
    data.setdefault("bookings", [])
    return data


def save_rooms(path: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def match_room(rooms: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    for room in rooms:
        keys = [
            str(room.get("id") or ""),
            str(room.get("pms_code") or ""),
            str(room.get("pms_label") or ""),
            str(room.get("name") or ""),
        ]
        if any(q == key.lower() for key in keys if key):
            return room
    for room in rooms:
        name = str(room.get("name") or "").lower()
        label = str(room.get("pms_label") or "").lower()
        if q in name or q in label:
            return room
    return None


def format_board(data: dict[str, Any]) -> str:
    lines = [
        data.get("property") or "Desk board",
        f"updated: {data.get('updated_at') or 'n/a'}",
        "source: local desk board (not live Zimmerstack writeback)",
        "",
    ]
    if not data.get("rooms"):
        lines.append("No rooms seeded. Add rooms.json on the VPS.")
        return "\n".join(lines)
    for room in data["rooms"]:
        code = room.get("pms_code") or room.get("id")
        label = room.get("pms_label") or room.get("name")
        status = str(room.get("status") or "vacant").upper()
        note = room.get("note") or ""
        line = f"{code} | {label} | {status}"
        if note:
            line += f" | {note}"
        lines.append(line)
    return "\n".join(lines)


def set_room_status(data: dict[str, Any], query: str, status: str, note: str = "") -> dict[str, Any]:
    room = match_room(data.get("rooms") or [], query)
    if room is None:
        raise ValueError(f"Room not found: {query}")
    status = status.strip().lower()
    if status not in ROOM_STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(sorted(ROOM_STATUSES))}")
    room["status"] = status
    if note:
        room["note"] = note
    room["status_updated_at"] = utc_now()
    return room


def menu_home_text() -> str:
    return (
        "Lily's Desk\n"
        "📋 Rooms · 🏠 In-house · 📅 Upcoming\n"
        "✏️ Edit (Clean / Check-in / Check-out / Assign)\n"
        "💚 Status · 🔑 Session\n"
        "Edit: choose action, then room — ✅ Confirm required.\n"
        "If the keyboard is missing, send /menu."
    )


def command_help() -> str:
    return (
        "Lily's Desk\n"
        "/menu — show buttons\n"
        "Buttons: Rooms, In-house, Upcoming, Edit, Status, Session\n"
        "/status /rooms /inhouse /upcoming /session /edit\n"
        "/clean <room> — mark AVAILABLE (confirm)\n"
        "/checkin <room|guest> — check-in (confirm)\n"
        "/checkout <room|guest> — check-out (confirm)\n"
        "/assign <bookingRoomId> <room> — assign room (confirm)\n"
        "/board /setroom <code> <status> [note] — local notes only\n"
        "/help — this list"
    )


def is_authorized(config: Config, user_id: int, chat_id: int) -> bool:
    if user_id in config.allowed_user_ids:
        return True
    if chat_id in config.allowed_chat_ids:
        return True
    if chat_id in config.hermes_chat_ids:
        return True
    return False


def load_edit_context(config: Config, auth: AuthState) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], int]], list[dict[str, Any]]]:
    rooms = fetch_pms_rooms(config, auth)
    upcoming = iter_booking_rooms(pms_get(config, auth, config.endpoint_upcoming))
    inhouse = iter_booking_rooms(pms_get(config, auth, config.endpoint_inhouse))
    checkins: list[tuple[dict[str, Any], int]] = []
    for br in upcoming:
        status = str(br.get("status") or br.get("_bookingStatus") or "").upper()
        if status == "CHECKEDIN":
            continue
        room_id = resolve_checkin_room_id(br)
        if room_id is not None:
            checkins.append((br, room_id))
    checkouts = [
        br
        for br in inhouse
        if str(br.get("status") or br.get("_bookingStatus") or "").upper() in {"CHECKEDIN", "INHOUSE", "OCCUPIED", ""}
        or br.get("checkedInAt")
    ]
    # Prefer true in-house rows; if empty keep list empty.
    if inhouse:
        checkouts = [
            br
            for br in inhouse
            if not br.get("checkedOutAt")
        ]
    return rooms, checkins, checkouts





def text_has_any(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)


def first_number_token(text: str) -> str | None:
    match = re.search(r"\b\d{2,6}\b", text)
    return match.group(0) if match else None


def two_number_tokens(text: str) -> tuple[str, str] | None:
    matches = re.findall(r"\b\d{2,6}\b", text)
    if len(matches) >= 2:
        return matches[0], matches[1]
    return None


def looks_like_short_booking(text: str) -> bool:
    normalized = text.lower().strip()
    if is_booking_cancel_text(normalized):
        return False
    if text_has_any(normalized, {"hold", "full", "block", "unblock", "free", "clean", "checkout", "checkin", "check-out", "check-in"}):
        return False
    if not first_number_token(normalized):
        return False
    has_phone = re.search(r"(?<!\d)(?:\+?91[- ]?)?[6-9]\d{9}(?!\d)", normalized) is not None
    if not has_phone:
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z.'-]*", normalized) if w not in {"room", "rs", "price", "for"}]
    return bool(words)


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def openrouter_intent(config: Config, text: str) -> dict[str, Any] | None:
    if not config.openrouter_api_key:
        return None
    system = (
        "You classify Telegram messages for a small hotel PMS bot. "
        "Return only one JSON object, no markdown. "
        "Allowed intents: rooms, inhouse, upcoming, menu, clean_room, hold_room_today, "
        "unhold_room_today, guest_booking, cancel_booking, checkin, checkout, collect_payment, assign, unknown. "
        "For write intents include room_query when the user names a room. "
        "For assign include booking_room_id and room_query when present. For collect_payment include room_query when present. "
        "Never invent room numbers or booking ids. Understand English, Hindi, and Hinglish inputs, but keep bot replies in English. Context: "
        "full/hold/reserve today/aaj means hold_room_today; book/booking without guest details means guest_booking form; guest booking with phone/name means guest_booking; "
        "booking cancel/cancel booking/guest cancel/reservation cancel means cancel_booking; cancel/remove/unblock/free/hatao hold means unhold_room_today; saaf/clean/available means clean_room."
    )
    payload = {
        "model": config.openrouter_model or "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text.strip()[:500]},
        ],
        "temperature": 0,
        "max_tokens": 120,
    }
    headers = {
        "Authorization": f"Bearer {config.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/LilysDesktopBot",
        "X-Title": "Lilys Desk Telegram Bot",
    }
    try:
        status, body, _, _ = http_request(
            "https://openrouter.ai/api/v1/chat/completions",
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
            timeout=min(max(4, config.request_timeout_seconds), 12),
        )
    except Exception as exc:
        LOG.warning("OpenRouter intent failed: %s", exc)
        return None
    if status < 200 or status >= 300:
        LOG.warning("OpenRouter intent returned HTTP %s", status)
        return None
    try:
        reply = json.loads(body.decode("utf-8", errors="replace"))
        content = reply["choices"][0]["message"]["content"]
    except Exception as exc:
        LOG.warning("OpenRouter intent parse failed: %s", exc)
        return None
    intent = extract_json_object(str(content))
    if not intent or not isinstance(intent.get("intent"), str):
        return None
    return intent


def openrouter_hermes_reply(config: Config, text: str) -> str | None:
    if not config.openrouter_api_key:
        return None
    system = (
        "You are Hermes, a concise helpful assistant for Lily's Retreats staff. "
        "Answer in clear English only. Keep replies short and practical. "
        "Do not perform PMS writes, bookings, payments, check-ins, or check-outs. "
        "If the user asks for PMS operations, tell them to use the Lily's Desk PMS bot commands such as /rooms, /book, /pay, /checkin, /checkout, or /clean. "
        "Never reveal API keys, passcodes, passwords, tokens, or private credentials."
    )
    payload = {
        "model": config.openrouter_model or "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text.strip()[:1200]},
        ],
        "temperature": 0.3,
        "max_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {config.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/LilysDesktopBot",
        "X-Title": "Hermes Staff Assistant",
    }
    try:
        status, body, _, _ = http_request(
            "https://openrouter.ai/api/v1/chat/completions",
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
            timeout=min(max(6, config.request_timeout_seconds), 20),
        )
    except Exception as exc:
        LOG.warning("OpenRouter Hermes reply failed: %s", exc)
        return None
    if status < 200 or status >= 300:
        LOG.warning("OpenRouter Hermes reply returned HTTP %s", status)
        return None
    try:
        reply = json.loads(body.decode("utf-8", errors="replace"))
        content = str(reply["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        LOG.warning("OpenRouter Hermes reply parse failed: %s", exc)
        return None
    return content or None


def handle_intent(config: Config, auth: AuthState, chat_id: int, intent: dict[str, Any]) -> bool:
    name = str(intent.get("intent") or "").strip().lower()
    room_query = str(intent.get("room_query") or intent.get("room") or "").strip()
    if name == "rooms":
        send_message(config, chat_id, render_action(config, auth, "rooms"), with_menu=True)
        return True
    if name == "inhouse":
        send_message(config, chat_id, render_action(config, auth, "inhouse"), with_menu=True)
        return True
    if name == "upcoming":
        send_message(config, chat_id, render_action(config, auth, "upcoming"), with_menu=True)
        return True
    if name == "menu":
        send_message(config, chat_id, menu_home_text(), with_menu=True)
        return True
    if name == "hold_room_today":
        if not room_query:
            send_message(config, chat_id, "Which room should be held for today? Example: hold 101 for today")
            return True
        ask_block_today_confirm(config, auth, chat_id, room_query)
        return True
    if name == "guest_booking":
        ask_booking_confirm(config, auth, chat_id, str(intent.get("text") or intent.get("raw") or ""))
        return True
    if name == "cancel_booking":
        query = str(intent.get("booking_id") or intent.get("booking_room_id") or room_query or intent.get("text") or intent.get("raw") or "").strip()
        if not query:
            send_message(config, chat_id, "Which guest booking should be cancelled? Example: cancel booking 102, or send the booking ID.")
            return True
        ask_cancel_booking_confirm(config, auth, chat_id, query)
        return True
    if name == "unhold_room_today":
        if not room_query:
            send_message(config, chat_id, "Which room hold should be cancelled? Example: cancel hold 102")
            return True
        ask_unblock_today_confirm(config, auth, chat_id, room_query)
        return True
    if name == "clean_room":
        if not room_query:
            send_message(config, chat_id, "Which room should be marked clean/available? Example: mark room 101 clean")
            return True
        ask_clean_confirm(config, auth, chat_id, room_query)
        return True
    if name == "checkin":
        ask_checkin_confirm(config, auth, chat_id, room_query or str(intent.get("booking_room_id") or ""))
        return True
    if name == "checkout":
        ask_checkout_confirm(config, auth, chat_id, room_query or str(intent.get("booking_room_id") or ""))
        return True
    if name == "collect_payment":
        query = room_query or str(intent.get("booking_room_id") or intent.get("text") or intent.get("raw") or "")
        ask_payment_confirm(config, auth, chat_id, query)
        return True
    if name == "assign":
        booking_room_id = str(intent.get("booking_room_id") or "").strip()
        if booking_room_id and room_query:
            ask_assign_confirm(config, auth, chat_id, booking_room_id, room_query)
            return True
        send_message(config, chat_id, "For assign, send bookingRoomId and room number. Example: assign 12345 to 101")
        return True
    return False


def answer_free_text(config: Config, auth: AuthState, text: str) -> str:
    raw = text.strip()
    normalized = raw.lower()
    room_words = {"room", "rooms", "status", "available", "availability", "vacant", "dirty", "clean", "update"}
    inhouse_words = {"inhouse", "in-house", "occupied", "guest", "guests", "stay", "staying"}
    upcoming_words = {"upcoming", "arrival", "arrivals", "booking", "bookings"}
    unblock_words = {"cancel", "remove", "unblock", "free", "hata", "hatao", "delete"}
    block_words = {"full", "hold", "reserve", "reserved", "unavailable", "block", "blocked"}
    if is_booking_cancel_text(normalized) and first_number_token(normalized):
        return "Guest booking cancellation needs confirmation. Example: cancel booking 102, or send the booking ID."
    if text_has_any(normalized, unblock_words) and first_number_token(normalized):
        return "Room hold cancellation needs confirmation. Example: cancel hold 102."
    if text_has_any(normalized, block_words) and first_number_token(normalized):
        return "Room hold/full update needs confirmation. Example: hold 101 for today."
    if text_has_any(normalized, room_words):
        return render_action(config, auth, "rooms")
    if text_has_any(normalized, inhouse_words):
        return render_action(config, auth, "inhouse")
    if text_has_any(normalized, upcoming_words) or "checkin" in normalized or "check-in" in normalized:
        return render_action(config, auth, "upcoming")
    if text_has_any(normalized, {"hi", "hello", "hey", "menu", "help"}):
        return menu_home_text()
    return (
        "I did not understand. For room status, send 'rooms' or /rooms. "
        "For cleaning, send 'mark room 101 clean'. "
        "For check-in/out, send 'checkin 101' or 'checkout 101'. "
        "For hold/full, send 'hold 101 for today'."
    )



def looks_like_hermes_staff_request(normalized: str) -> bool:
    assistant_words = {
        "draft", "write", "reply", "message", "whatsapp", "polite", "professional",
        "summarize", "summary", "suggest", "wording", "template", "explain", "translate",
        "improve", "rephrase", "guest reply", "guest message", "how to", "what should",
        "request", "complaint", "response", "respond", "send to guest", "guest ko",
    }
    if text_has_any(normalized, assistant_words):
        return True
    if re.match(r"^(?:hi|hey|hello)\s+herm(?:e)?s\b", normalized):
        return True
    return False

def handle_free_text(config: Config, auth: AuthState, user_id: int, chat_id: int, text: str) -> None:
    if not is_authorized(config, user_id, chat_id):
        LOG.warning("Ignored unauthorized Telegram free text user_id=%s chat_id=%s", user_id, chat_id)
        return
    raw = text.strip()
    if not raw:
        return
    normalized = raw.lower()
    try:
        if chat_id in config.hermes_chat_ids:
            reply = openrouter_hermes_reply(config, raw)
            send_message(config, chat_id, reply or "Hermes is not available right now. Please try again in a minute.", with_menu=False)
            return

        # Staff drafting/help requests should go to Hermes before PMS keyword routing.
        # Example: "write a polite reply for early check-in request" contains check-in,
        # but it is not an instruction to check a guest in.
        if looks_like_hermes_staff_request(normalized):
            LOG.info("Routing staff drafting/help request to Hermes chat_id=%s", chat_id)
            if config.openrouter_api_key:
                reply = openrouter_hermes_reply(config, raw)
                send_message(
                    config,
                    chat_id,
                    reply or "Hermes is not available right now. Please try again in a minute.",
                    with_menu=True,
                )
                return

        # Natural write intents still only prepare Telegram confirmation buttons.
        if is_booking_cancel_text(normalized) and first_number_token(normalized):
            query = first_number_token(normalized) or raw
            ask_cancel_booking_confirm(config, auth, chat_id, query)
            return
        if text_has_any(normalized, {"pay", "payment", "collect", "paid", "cash"}) and first_number_token(normalized):
            ask_payment_confirm(config, auth, chat_id, first_number_token(normalized) or raw)
            return
        if (
            "/book" in normalized
            or "/booking" in normalized
            or looks_like_short_booking(normalized)
            or (text_has_any(normalized, {"book", "booking"}) and first_number_token(normalized))
            or (re.search(r"(?<!\d)(?:\+?91[- ]?)?[6-9]\d{9}(?!\d)", normalized) and text_has_any(normalized, {"book", "booking", "reserve", "guest"}))
        ):
            ask_booking_confirm(config, auth, chat_id, raw)
            return
        unblock_words = {"cancel", "remove", "unblock", "free", "hata", "hatao", "delete"}
        block_words = {"full", "hold", "reserve", "reserved", "unavailable", "block", "blocked"}
        if text_has_any(normalized, unblock_words) and first_number_token(normalized):
            room_query = first_number_token(normalized)
            if room_query:
                ask_unblock_today_confirm(config, auth, chat_id, room_query)
                return
            send_message(config, chat_id, "Which room hold should be cancelled? Example: cancel hold 102")
            return
        if text_has_any(normalized, block_words) and first_number_token(normalized):
            room_query = first_number_token(normalized)
            if room_query:
                ask_block_today_confirm(config, auth, chat_id, room_query)
                return
            send_message(config, chat_id, "Which room should be held for today? Example: hold 101 for today")
            return
        if text_has_any(normalized, {"clean", "available", "vacant"}) and text_has_any(normalized, {"room", "status", "update", "clean"}):
            room_query = first_number_token(normalized)
            if room_query:
                ask_clean_confirm(config, auth, chat_id, room_query)
                return
            send_message(config, chat_id, "Which room should be marked clean/available? Example: mark room 101 clean")
            return
        if "checkin" in normalized or "check-in" in normalized or "check in" in normalized:
            query = first_number_token(normalized) or raw
            ask_checkin_confirm(config, auth, chat_id, query)
            return
        if "checkout" in normalized or "check-out" in normalized or "check out" in normalized:
            query = first_number_token(normalized) or raw
            ask_checkout_confirm(config, auth, chat_id, query)
            return
        if "assign" in normalized:
            pair = two_number_tokens(normalized)
            if pair:
                ask_assign_confirm(config, auth, chat_id, pair[0], pair[1])
                return
            send_message(config, chat_id, "For assign, send bookingRoomId and room number. Example: assign 12345 to 101")
            return
        if "dirty" in normalized and first_number_token(normalized):
            send_message(
                config,
                chat_id,
                "Marking a room dirty from Telegram is not configured yet. Use /edit to see available actions.",
            )
            return
        intent = openrouter_intent(config, raw)
        if intent:
            intent.setdefault("text", raw)
            intent.setdefault("raw", raw)
        if intent and handle_intent(config, auth, chat_id, intent):
            return
        if config.openrouter_api_key and chat_id in config.allowed_chat_ids:
            reply = openrouter_hermes_reply(config, raw)
            if reply:
                send_message(config, chat_id, reply, with_menu=True)
                return
        send_message(config, chat_id, answer_free_text(config, auth, raw), with_menu=True)
    except Exception as exc:
        LOG.exception("Free-text PMS helper failed")
        send_message(config, chat_id, f"PMS helper failed: {type(exc).__name__}: {exc}\nUse /rooms or /menu meanwhile.")


def render_action(config: Config, auth: AuthState, action: str) -> str:
    if action == "home":
        return menu_home_text()
    if action == "status":
        return status_text(run_health(config))
    if action == "session":
        return check_session(config, auth, refresh=True)
    if action == "board":
        return format_board(load_rooms(config.rooms_file))
    if action == "edit":
        rooms, checkins, checkouts = load_edit_context(config, auth)
        return format_edit_menu(rooms, checkins, checkouts)
    endpoint_by_action = {
        "rooms": config.endpoint_rooms,
        "inhouse": config.endpoint_inhouse,
        "upcoming": config.endpoint_upcoming,
    }
    if action not in endpoint_by_action:
        return "Unknown action."
    ensure_session(config, auth)
    payload = redact(pms_get(config, auth, endpoint_by_action[action]), config.redact_pii)
    if action == "rooms":
        blocks_by_room: dict[int, dict[str, Any]] = {}
        try:
            start_ymd = india_today().isoformat()
            blocks_by_room = active_blocks_by_room(config, auth, start_ymd, add_days_ymd(start_ymd, 1))
        except Exception:
            LOG.exception("Could not load room blocks for rooms view")
        return format_pms_rooms(payload, blocks_by_room)
    if action == "inhouse":
        return format_pms_stays("In-house", payload)
    return format_pms_stays("Upcoming", payload)


def send_edit_menu(config: Config, auth: AuthState, chat_id: int) -> None:
    rooms, checkins, checkouts = load_edit_context(config, auth)
    send_message(
        config,
        chat_id,
        format_edit_menu(rooms, checkins, checkouts),
        with_menu=False,
        reply_markup=edit_menu_keyboard(rooms, checkins, checkouts),
    )


def ask_clean_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    rooms = fetch_pms_rooms(config, auth)
    room = find_pms_room(rooms, query)
    if room is None:
        send_message(config, chat_id, f"Room not found: {query}")
        return
    rid = int(room["id"])
    status = str(room.get("roomStatus") or "?").upper()
    text = (
        "Mark room clean / available?\n"
        f"Room: {room.get('roomNumber')}\n"
        f"Current status: {status}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:c:{rid}", f"wn:c:{rid}"),
    )


def ask_block_today_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    rooms = fetch_pms_rooms(config, auth)
    room = find_pms_room(rooms, query)
    if room is None:
        send_message(config, chat_id, f"Room not found: {query}")
        return
    rid = int(room["id"])
    number = room.get("roomNumber") or rid
    status = str(room.get("roomStatus") or "?").upper()
    start_ymd = india_today().isoformat()
    end_ymd = add_days_ymd(start_ymd, 1)
    text = (
        "Book / hold this room for today?\n"
        f"Room: {number}\n"
        f"Date: {start_ymd} → {end_ymd}\n"
        f"Current status: {status}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:b:{rid}:{compact_ymd(start_ymd)}", f"wn:b:{rid}"),
    )


def ask_unblock_today_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    rooms = fetch_pms_rooms(config, auth)
    room = find_pms_room(rooms, query)
    if room is None:
        send_message(config, chat_id, f"Room not found: {query}")
        return
    rid = int(room["id"])
    number = room.get("roomNumber") or rid
    start_ymd = india_today().isoformat()
    end_ymd = add_days_ymd(start_ymd, 1)
    block = find_room_block(config, auth, rid, start_ymd, end_ymd)
    if not block:
        send_message(config, chat_id, f"No room hold found for {number} today.", with_menu=True)
        return
    block_id = block_identifier(block)
    if block_id is None:
        send_message(config, chat_id, f"Hold found for {number}, but PMS did not return a removable block id.", with_menu=True)
        return
    text = (
        "Cancel / remove this room hold?\n"
        f"Room: {number}\n"
        f"Date: {short_date(block.get('startDate'))} → {short_date(block.get('endDate'))}\n"
        f"Type: {room_block_label(block)}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:u:{block_id}:{rid}:{compact_ymd(start_ymd)}", f"wn:u:{block_id}"),
    )


def ask_checkin_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    ensure_session(config, auth)
    upcoming = iter_booking_rooms(pms_get(config, auth, config.endpoint_upcoming))
    q = query.strip().lower()
    match: tuple[dict[str, Any], int] | None = None
    for br in upcoming:
        room_id = resolve_checkin_room_id(br)
        if room_id is None:
            continue
        hay = " ".join(
            [
                str(br.get("id") or ""),
                str(br.get("roomNumber") or ""),
                str(br.get("_guest") or ""),
                str(room_id),
                room_number_key(br.get("roomNumber")),
            ]
        ).lower()
        if q == str(br.get("id")) or q == room_number_key(br.get("roomNumber")) or q in hay:
            match = (br, room_id)
            if q == str(br.get("id")) or q == room_number_key(br.get("roomNumber")):
                break
    if match is None:
        send_message(
            config,
            chat_id,
            "No assigned upcoming stay matched.\n"
            "Use /assign <bookingRoomId> <room> first, or open Edit.",
        )
        return
    br, room_id = match
    brid = int(br["id"])
    text = (
        f"Check-in?\n"
        f"Guest: {br.get('_guest')}\n"
        f"Room: {br.get('roomNumber') or room_id} (roomId {room_id})\n"
        f"Stay: {short_date(br.get('checkInDate'))} → {short_date(br.get('checkOutDate'))}\n"
        f"bookingRoomId: {brid}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:i:{brid}:{room_id}", f"wn:i:{brid}"),
    )


def ask_payment_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    ensure_session(config, auth)
    inhouse = iter_booking_rooms(pms_get(config, auth, config.endpoint_inhouse))
    match = find_inhouse_booking_room(inhouse, query)
    if match is None:
        send_message(config, chat_id, "No in-house stay matched for payment. Check /inhouse.")
        return
    brid = booking_room_id(match)
    booking_id = booking_room_booking_id(match)
    if booking_id is None or brid is None:
        send_message(config, chat_id, "Payment target is missing booking id. Check /inhouse and try with bookingRoomId.")
        return
    due = booking_due_amount(match)
    if due is None or due <= 0:
        send_message(config, chat_id, f"No due amount found for bookingRoom {brid}. Try /checkout {brid} or check PMS.")
        return
    cents = int(round(due * 100))
    text = (
        "Collect cash payment?\n"
        f"Guest: {match.get('_guest') or 'Guest'}\n"
        f"Room: {match.get('roomNumber') or '-'}\n"
        f"Booking: {booking_id} · bookingRoom: {brid}\n"
        f"Amount: {format_money(due)}\n"
        "Mode: CASH\n"
        "After payment, send /checkout <room> to check out."
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:p:{booking_id}:{brid}:{cents}", f"wn:p:{brid}"),
    )


def ask_checkout_confirm(config: Config, auth: AuthState, chat_id: int, query: str) -> None:
    ensure_session(config, auth)
    inhouse = iter_booking_rooms(pms_get(config, auth, config.endpoint_inhouse))
    q = query.strip().lower()
    match: dict[str, Any] | None = None
    for br in inhouse:
        hay = " ".join(
            [
                str(br.get("id") or ""),
                str(br.get("roomNumber") or ""),
                str(br.get("_guest") or ""),
                room_number_key(br.get("roomNumber")),
            ]
        ).lower()
        if q == str(br.get("id")) or q == room_number_key(br.get("roomNumber")) or q in hay:
            match = br
            if q == str(br.get("id")) or q == room_number_key(br.get("roomNumber")):
                break
    if match is None:
        send_message(config, chat_id, "No in-house stay matched. Check /inhouse.")
        return
    brid = int(match["id"])
    text = (
        f"Check-out?\n"
        f"Guest: {match.get('_guest')}\n"
        f"Room: {match.get('roomNumber') or '-'}\n"
        f"bookingRoomId: {brid}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:o:{brid}", f"wn:o:{brid}"),
    )


def ask_assign_confirm(config: Config, auth: AuthState, chat_id: int, booking_room_id: str, room_query: str) -> None:
    rooms = fetch_pms_rooms(config, auth)
    room = find_pms_room(rooms, room_query)
    if room is None:
        send_message(config, chat_id, f"Room not found: {room_query}")
        return
    try:
        brid = int(booking_room_id)
    except ValueError:
        send_message(config, chat_id, "Usage: /assign <bookingRoomId> <room>")
        return
    rid = int(room["id"])
    text = (
        "Assign room?\n"
        f"Booking room: {brid}\n"
        f"Room: {room.get('roomNumber')}"
    )
    send_message(
        config,
        chat_id,
        text,
        with_menu=False,
        reply_markup=confirm_keyboard(f"wy:a:{brid}:{rid}", f"wn:a:{brid}"),
    )


def handle_command(config: Config, auth: AuthState, user_id: int, chat_id: int, text: str) -> None:
    if not is_authorized(config, user_id, chat_id):
        LOG.warning("Ignored unauthorized Telegram user_id=%s chat_id=%s", user_id, chat_id)
        return
    raw = text.strip()
    parts = raw.split()
    command = parts[0].split("@")[0].lower() if parts else ""
    # Reply-keyboard taps arrive as plain text labels (no leading /).
    label = normalize_button_label(raw)
    if label in BUTTON_TEXT_ACTIONS and not command.startswith("/"):
        action = BUTTON_TEXT_ACTIONS[label]
        if action == "help":
            send_message(config, chat_id, command_help())
            return
        if action == "home":
            send_message(config, chat_id, menu_home_text())
            return
        if action == "edit":
            try:
                send_edit_menu(config, auth, chat_id)
            except Exception as exc:
                LOG.exception("Edit menu failed")
                send_message(config, chat_id, f"Edit failed: {type(exc).__name__}: {exc}")
            return
        try:
            send_message(config, chat_id, render_action(config, auth, action))
        except Exception as exc:
            LOG.exception("Button label action failed")
            send_message(config, chat_id, f"{raw} failed: {type(exc).__name__}: {exc}")
        return
    if command in {"/start", "/menu"}:
        send_message(config, chat_id, menu_home_text())
        return
    if command == "/help":
        send_message(config, chat_id, command_help())
        return
    if command in {"/hermes", "/ask"}:
        prompt = " ".join(parts[1:]).strip()
        if not prompt:
            send_message(
                config,
                chat_id,
                "Usage: /hermes <question or draft request>\nExample: /hermes write a polite reply for early check-in request",
            )
            return
        LOG.info("Routing slash Hermes request chat_id=%s command=%s", chat_id, command)
        if not config.openrouter_api_key:
            send_message(config, chat_id, "Hermes is not configured yet. OPENROUTER_API_KEY is missing.")
            return
        reply = openrouter_hermes_reply(config, prompt)
        send_message(
            config,
            chat_id,
            reply or "Hermes is not available right now. Please try again in a minute.",
            with_menu=True,
        )
        return
    if command == "/session":
        send_message(config, chat_id, check_session(config, auth, refresh=True))
        return
    if command == "/status":
        send_message(config, chat_id, status_text(run_health(config)))
        return
    if command == "/edit":
        try:
            send_edit_menu(config, auth, chat_id)
        except Exception as exc:
            LOG.exception("Edit menu failed")
            send_message(config, chat_id, f"/edit failed: {type(exc).__name__}: {exc}")
        return
    if command == "/clean":
        if len(parts) < 2:
            send_message(config, chat_id, "Usage: /clean <roomNumber>")
            return
        try:
            ask_clean_confirm(config, auth, chat_id, parts[1])
        except Exception as exc:
            LOG.exception("clean ask failed")
            send_message(config, chat_id, f"/clean failed: {type(exc).__name__}: {exc}")
        return
    if command == "/checkin":
        if len(parts) < 2:
            send_message(config, chat_id, "Usage: /checkin <room|guest|bookingRoomId>")
            return
        try:
            ask_checkin_confirm(config, auth, chat_id, " ".join(parts[1:]))
        except Exception as exc:
            LOG.exception("checkin ask failed")
            send_message(config, chat_id, f"/checkin failed: {type(exc).__name__}: {exc}")
        return
    if command in {"/pay", "/payment", "/collect"}:
        if len(parts) < 2:
            send_message(config, chat_id, "Usage: /pay <room|guest|bookingRoomId>")
            return
        try:
            ask_payment_confirm(config, auth, chat_id, " ".join(parts[1:]))
        except Exception as exc:
            LOG.exception("payment ask failed")
            send_message(config, chat_id, f"/pay failed: {type(exc).__name__}: {exc}")
        return
    if command == "/checkout":
        if len(parts) < 2:
            send_message(config, chat_id, "Usage: /checkout <room|guest|bookingRoomId>")
            return
        try:
            ask_checkout_confirm(config, auth, chat_id, " ".join(parts[1:]))
        except Exception as exc:
            LOG.exception("checkout ask failed")
            send_message(config, chat_id, f"/checkout failed: {type(exc).__name__}: {exc}")
        return
    if command in {"/book", "/booking"}:
        if len(parts) < 2:
            send_message(config, chat_id, booking_help_text(config))
            return
        try:
            ask_booking_confirm(config, auth, chat_id, raw)
        except Exception as exc:
            LOG.exception("booking ask failed")
            send_message(config, chat_id, f"/book failed: {type(exc).__name__}: {exc}")
        return
    if command == "/assign":
        if len(parts) < 3:
            send_message(config, chat_id, "Usage: /assign <bookingRoomId> <room>")
            return
        try:
            ask_assign_confirm(config, auth, chat_id, parts[1], parts[2])
        except Exception as exc:
            LOG.exception("assign ask failed")
            send_message(config, chat_id, f"/assign failed: {type(exc).__name__}: {exc}")
        return
    if command == "/board":
        send_message(config, chat_id, format_board(load_rooms(config.rooms_file)))
        return
    if command == "/setroom":
        if len(parts) < 3:
            send_message(config, chat_id, "Usage: /setroom <code|name> <status> [note]")
            return
        try:
            data = load_rooms(config.rooms_file)
            room = set_room_status(data, parts[1], parts[2], " ".join(parts[3:]).strip())
            save_rooms(config.rooms_file, data)
            send_message(
                config,
                chat_id,
                f"Updated {room.get('pms_code') or room.get('id')} → {room.get('status')}"
                + (f"\n{room.get('note')}" if room.get("note") else ""),
            )
        except Exception as exc:
            send_message(config, chat_id, f"/setroom failed: {exc}")
        return

    action_by_command = {
        "/room": "rooms",
        "/rooms": "rooms",
        "/pmsrooms": "rooms",
        "/inhouse": "inhouse",
        "/upcoming": "upcoming",
    }
    if command in action_by_command:
        try:
            send_message(config, chat_id, render_action(config, auth, action_by_command[command]))
        except Exception as exc:
            LOG.exception("PMS command failed")
            send_message(config, chat_id, f"{command} failed: {type(exc).__name__}: {exc}")
        return
    send_message(config, chat_id, "Unknown command. Use /menu or tap a button.")


def handle_write_callback(
    config: Config,
    auth: AuthState,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    callback_id: str,
    data: str,
) -> bool:
    """Handle edit/confirm write callbacks. Returns True if handled."""
    if data.startswith("book_hold:"):
        answer_callback(config, callback_id, "Confirm hold…")
        _, room_query, date_token = data.split(":", 2)
        try:
            rooms = fetch_pms_rooms(config, auth)
            room = find_pms_room(rooms, room_query)
            if room is None:
                edit_message(config, chat_id, message_id, f"Room not found: {room_query}", with_menu=True)
                return True
            rid = int(room["id"])
            number = room.get("roomNumber") or rid
            start_ymd = expand_compact_ymd(date_token)
            end_ymd = add_days_ymd(start_ymd, 1)
            edit_message(
                config,
                chat_id,
                message_id,
                f"Hold room instead?\nRoom: {number}\nDate: {start_ymd} → {end_ymd}",
                with_menu=False,
            )
            send_message(
                config,
                chat_id,
                f"Confirm hold for {number}?",
                with_menu=False,
                reply_markup=confirm_keyboard(f"wy:b:{rid}:{date_token}", f"wn:b:{rid}"),
            )
        except Exception as exc:
            LOG.exception("booking form hold handoff failed")
            edit_message(config, chat_id, message_id, f"Hold option failed: {exc}", with_menu=True)
        return True
    if data == "edit_act:home" or data.startswith("edit_act:"):
        answer_callback(config, callback_id, "OK")
        rooms, checkins, checkouts = load_edit_context(config, auth)
        act = data.split(":", 1)[1] if ":" in data else "home"
        if act in {"home", ""}:
            edit_message(config, chat_id, message_id, format_edit_menu(rooms, checkins, checkouts), with_menu=False)
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_home_keyboard(rooms, checkins, checkouts),
                },
            )
            return True
        if act == "clean":
            edit_message(
                config,
                chat_id,
                message_id,
                "🟢 Which room should be marked CLEAN / AVAILABLE?\n🔴 Dirty · 🟢 Available · 🟡 Other",
                with_menu=False,
            )
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_clean_room_keyboard(rooms),
                },
            )
            return True
        if act == "ci":
            edit_message(
                config,
                chat_id,
                message_id,
                "🔵 Which guest should check in?\n(Assigned / pre-assigned stays)",
                with_menu=False,
            )
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_checkin_keyboard(checkins),
                },
            )
            return True
        if act == "co":
            edit_message(
                config,
                chat_id,
                message_id,
                "🟠 Which guest should check out?",
                with_menu=False,
            )
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_checkout_keyboard(checkouts),
                },
            )
            return True
        if act == "assign":
            upcoming = iter_booking_rooms(pms_get(config, auth, config.endpoint_upcoming))
            edit_message(
                config,
                chat_id,
                message_id,
                "🟣 Choose a stay first, then a room.",
                with_menu=False,
            )
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_assign_booking_keyboard(upcoming),
                },
            )
            return True
        return True
    if data.startswith("edit_as:"):
        answer_callback(config, callback_id, "Pick room…")
        brid = int(data.split(":", 1)[1])
        rooms = fetch_pms_rooms(config, auth)
        edit_message(
            config,
            chat_id,
            message_id,
            f"🟣 Assign: bookingRoom {brid}\nWhich room?",
            with_menu=False,
        )
        telegram_call(
            config,
            "editMessageReplyMarkup",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "reply_markup": edit_assign_room_keyboard(brid, rooms),
            },
        )
        return True
    if data.startswith("edit_assign:"):
        answer_callback(config, callback_id, "Confirm…")
        _, brid, rid = data.split(":", 2)
        room = find_pms_room(fetch_pms_rooms(config, auth), rid)
        label = room.get("roomNumber") if room else rid
        edit_message(
            config,
            chat_id,
            message_id,
            f"🟣 Assign confirm?\nbookingRoom {brid} → {label} ({rid})",
            with_menu=False,
        )
        send_message(
            config,
            chat_id,
            f"Confirm assign {label}?",
            with_menu=False,
            reply_markup=confirm_keyboard(f"wy:a:{brid}:{rid}", f"wn:a:{brid}"),
        )
        return True
    if data.startswith("edit_clean:"):
        answer_callback(config, callback_id, "Confirm…")
        rid = data.split(":", 1)[1]
        room = find_pms_room(fetch_pms_rooms(config, auth), rid)
        label = room.get("roomNumber") if room else rid
        status = str(room.get("roomStatus") or "?").upper() if room else "?"
        edit_message(
            config,
            chat_id,
            message_id,
            f"🟢 Mark AVAILABLE (clean)?\nRoom: {label} (id {rid})\nNow: {status_emoji(status)} {status}",
            with_menu=False,
        )
        send_message(
            config,
            chat_id,
            f"✅ Confirm clean for {label}?",
            with_menu=False,
            reply_markup=confirm_keyboard(f"wy:c:{rid}", f"wn:c:{rid}"),
        )
        return True
    if data.startswith("edit_ci:"):
        answer_callback(config, callback_id, "Confirm…")
        _, brid, rid = data.split(":", 2)
        send_message(
            config,
            chat_id,
            f"🔵 Confirm check-in?\nbookingRoomId {brid} → roomId {rid}",
            with_menu=False,
            reply_markup=confirm_keyboard(f"wy:i:{brid}:{rid}", f"wn:i:{brid}"),
        )
        return True
    if data.startswith("edit_co:"):
        answer_callback(config, callback_id, "Confirm…")
        brid = data.split(":", 1)[1]
        send_message(
            config,
            chat_id,
            f"🟠 Confirm check-out?\nbookingRoomId {brid}",
            with_menu=False,
            reply_markup=confirm_keyboard(f"wy:o:{brid}", f"wn:o:{brid}"),
        )
        return True
    if data.startswith("wn:"):
        answer_callback(config, callback_id, "Cancelled")
        edit_message(config, chat_id, message_id, "❌ Cancelled.", with_menu=True)
        append_audit(
            config,
            {"action": "cancel", "data": data, "user_id": user_id, "chat_id": chat_id},
        )
        return True
    if data.startswith("wy:c:"):
        answer_callback(config, callback_id, "Cleaning…")
        rid = int(data.split(":")[2])
        try:
            ensure_session(config, auth)
            result = pms_mark_available(config, auth, rid)
            room = pms_data(result) if isinstance(result, dict) else {}
            number = room.get("roomNumber") if isinstance(room, dict) else rid
            status = room.get("roomStatus") if isinstance(room, dict) else "AVAILABLE"
            append_audit(
                config,
                {
                    "action": "clean",
                    "room_id": rid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                    "status": status,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Done · {number} → {status}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("clean write failed")
            append_audit(
                config,
                {"action": "clean", "room_id": rid, "user_id": user_id, "chat_id": chat_id, "ok": False, "error": str(exc)},
            )
            edit_message(config, chat_id, message_id, f"Clean failed: {exc}", with_menu=True)
        return True
    if data.startswith("wy:b:"):
        answer_callback(config, callback_id, "Blocking…")
        parts = data.split(":")
        rid, date_token = int(parts[2]), parts[3]
        start_ymd = expand_compact_ymd(date_token)
        end_ymd = add_days_ymd(start_ymd, 1)
        try:
            ensure_session(config, auth)
            rooms = fetch_pms_rooms(config, auth)
            room = next((r for r in rooms if int(r.get("id") or 0) == rid), {})
            number = room.get("roomNumber") if isinstance(room, dict) else rid
            pms_block_room(config, auth, rid, start_ymd, end_ymd)
            append_audit(
                config,
                {
                    "action": "block_today",
                    "room_id": rid,
                    "room_number": number,
                    "start_date": start_ymd,
                    "end_date": end_ymd,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Done · room {number} FULL/hold for {start_ymd} → {end_ymd}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("room block write failed")
            append_audit(
                config,
                {
                    "action": "block_today",
                    "room_id": rid,
                    "start_date": start_ymd,
                    "end_date": end_ymd,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            if is_passcode_error(exc) and not config.pms_block_pin:
                message = "Room hold needs PMS passcode. Please configure PMS_BLOCK_PIN on the bot server."
            elif is_passcode_error(exc):
                message = "Room hold failed: PMS passcode was rejected. Please check PMS_BLOCK_PIN."
            else:
                message = f"Room hold failed: {exc}"
            edit_message(config, chat_id, message_id, message, with_menu=True)
        return True
    if data.startswith("wy:u:"):
        answer_callback(config, callback_id, "Removing hold…")
        parts = data.split(":")
        block_id, rid, date_token = int(parts[2]), int(parts[3]), parts[4]
        start_ymd = expand_compact_ymd(date_token)
        end_ymd = add_days_ymd(start_ymd, 1)
        try:
            ensure_session(config, auth)
            rooms = fetch_pms_rooms(config, auth)
            room = next((r for r in rooms if int(r.get("id") or 0) == rid), {})
            number = room.get("roomNumber") if isinstance(room, dict) else rid
            pms_unblock_room(config, auth, block_id)
            append_audit(
                config,
                {
                    "action": "unblock_today",
                    "room_block_id": block_id,
                    "room_id": rid,
                    "room_number": number,
                    "start_date": start_ymd,
                    "end_date": end_ymd,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Done · room {number} hold removed for {start_ymd} → {end_ymd}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("room unblock write failed")
            append_audit(
                config,
                {
                    "action": "unblock_today",
                    "room_block_id": block_id,
                    "room_id": rid,
                    "start_date": start_ymd,
                    "end_date": end_ymd,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            if is_passcode_error(exc) and not config.pms_block_pin:
                message = "Room hold cancel needs PMS passcode. Please configure PMS_BLOCK_PIN on the bot server."
            elif is_passcode_error(exc):
                message = "Room hold cancel failed: PMS passcode was rejected. Please check PMS_BLOCK_PIN."
            else:
                message = f"Room hold cancel failed: {exc}"
            edit_message(config, chat_id, message_id, message, with_menu=True)
        return True
    if data.startswith("wy:bc:"):
        answer_callback(config, callback_id, "Cancelling booking…")
        booking_id = int(data.split(":", 2)[2])
        try:
            ensure_session(config, auth)
            pms_cancel_booking(config, auth, booking_id)
            append_audit(
                config,
                {
                    "action": "cancel_booking",
                    "booking_id": booking_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(config, chat_id, message_id, f"Done · booking {booking_id} cancelled.", with_menu=True)
        except Exception as exc:
            LOG.exception("booking cancel write failed")
            append_audit(
                config,
                {
                    "action": "cancel_booking",
                    "booking_id": booking_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            if is_passcode_error(exc) and not config.pms_block_pin:
                message = "Booking cancel needs PMS passcode. Please configure PMS_BLOCK_PIN on the bot server."
            elif is_passcode_error(exc):
                message = "Booking cancel failed: PMS passcode was rejected. Please check PMS_BLOCK_PIN."
            else:
                message = f"Booking cancel failed: {exc}"
            edit_message(config, chat_id, message_id, message, with_menu=True)
        return True
    if data.startswith("wy:g:"):
        answer_callback(config, callback_id, "Creating booking…")
        draft_id = data.split(":", 2)[2]
        draft = pop_booking_draft(config, draft_id)
        if draft is None:
            edit_message(config, chat_id, message_id, "Booking draft expired. Please send /book again.", with_menu=True)
            return True
        try:
            booking_id, booking_room_id = pms_create_guest_booking(config, auth, draft)
            append_audit(
                config,
                {
                    "action": "guest_booking",
                    "room_id": draft.room_id,
                    "room_number": draft.room_number,
                    "room_type_id": draft.room_type_id,
                    "check_in": draft.check_in,
                    "check_out": draft.check_out,
                    "tariff": draft.tariff,
                    "booking_id": booking_id,
                    "booking_room_id": booking_room_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            assigned = f" · bookingRoom {booking_room_id} assigned" if booking_room_id else " · booking created; room assign check manually"
            edit_message(
                config,
                chat_id,
                message_id,
                f"Done · booking {booking_id or '-'} for room {draft.room_number}{assigned}\n{draft.check_in} → {draft.check_out}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("guest booking write failed")
            append_audit(
                config,
                {
                    "action": "guest_booking",
                    "room_id": draft.room_id,
                    "room_number": draft.room_number,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            edit_message(config, chat_id, message_id, f"Booking failed: {exc}", with_menu=True)
        return True
    if data.startswith("wy:i:"):
        answer_callback(config, callback_id, "Checking in…")
        parts = data.split(":")
        brid, rid = int(parts[2]), int(parts[3])
        try:
            ensure_session(config, auth)
            pms_checkin(config, auth, brid, rid)
            append_audit(
                config,
                {
                    "action": "checkin",
                    "booking_room_id": brid,
                    "room_id": rid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Checked in · bookingRoom {brid} → room {rid}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("checkin write failed")
            append_audit(
                config,
                {
                    "action": "checkin",
                    "booking_room_id": brid,
                    "room_id": rid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            edit_message(config, chat_id, message_id, f"Check-in failed: {exc}", with_menu=True)
        return True
    if data.startswith("wy:p:"):
        answer_callback(config, callback_id, "Collecting payment…")
        parts = data.split(":")
        booking_id, brid, cents = int(parts[2]), int(parts[3]), int(parts[4])
        amount = cents / 100.0
        try:
            ensure_session(config, auth)
            pms_collect_booking_payment(config, auth, booking_id, amount)
            append_audit(
                config,
                {
                    "action": "collect_payment",
                    "booking_id": booking_id,
                    "booking_room_id": brid,
                    "amount": amount,
                    "mode": "CASH",
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Payment collected · booking {booking_id} · bookingRoom {brid} · {format_money(amount)} CASH\nNow send /checkout {brid}.",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("payment write failed")
            append_audit(
                config,
                {
                    "action": "collect_payment",
                    "booking_id": booking_id,
                    "booking_room_id": brid,
                    "amount": amount,
                    "mode": "CASH",
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            edit_message(config, chat_id, message_id, f"Payment failed: {exc}", with_menu=True)
        return True
    if data.startswith("wy:o:"):
        answer_callback(config, callback_id, "Checking out…")
        brid = int(data.split(":")[2])
        try:
            ensure_session(config, auth)
            pms_checkout(config, auth, brid)
            append_audit(
                config,
                {
                    "action": "checkout",
                    "booking_room_id": brid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Checked out · bookingRoom {brid}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("checkout write failed")
            append_audit(
                config,
                {
                    "action": "checkout",
                    "booking_room_id": brid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            if "payment" in str(exc).lower() and "pending" in str(exc).lower():
                message = f"Check-out failed: payment is pending. Send /pay {brid}, confirm cash collection, then try /checkout {brid}.\n{exc}"
            else:
                message = f"Check-out failed: {exc}"
            edit_message(config, chat_id, message_id, message, with_menu=True)
        return True
    if data.startswith("wy:a:"):
        answer_callback(config, callback_id, "Assigning…")
        parts = data.split(":")
        brid, rid = int(parts[2]), int(parts[3])
        try:
            ensure_session(config, auth)
            pms_assign(config, auth, brid, rid)
            append_audit(
                config,
                {
                    "action": "assign",
                    "booking_room_id": brid,
                    "room_id": rid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": True,
                },
            )
            edit_message(
                config,
                chat_id,
                message_id,
                f"Assigned · bookingRoom {brid} → room {rid}",
                with_menu=True,
            )
        except Exception as exc:
            LOG.exception("assign write failed")
            append_audit(
                config,
                {
                    "action": "assign",
                    "booking_room_id": brid,
                    "room_id": rid,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                },
            )
            edit_message(config, chat_id, message_id, f"Assign failed: {exc}", with_menu=True)
        return True
    if data.startswith("wn:p:"):
        answer_callback(config, callback_id, "Cancelled")
        edit_message(config, chat_id, message_id, "Payment cancelled.", with_menu=True)
        return True
    return False


def handle_callback(config: Config, auth: AuthState, callback: dict[str, Any]) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if "id" not in sender or "id" not in chat or "message_id" not in message:
        return
    user_id = int(sender["id"])
    chat_id = int(chat["id"])
    message_id = int(message["message_id"])
    if not is_authorized(config, user_id, chat_id):
        answer_callback(config, callback_id, "Not allowed")
        LOG.warning("Ignored unauthorized callback user_id=%s chat_id=%s", user_id, chat_id)
        return
    if handle_write_callback(
        config,
        auth,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        callback_id=callback_id,
        data=data,
    ):
        return
    action = MENU_ACTIONS.get(data)
    if not action:
        answer_callback(config, callback_id, "Unknown button")
        return
    answer_callback(config, callback_id, "Loading…")
    loading_label = {
        "rooms": "⏳ Loading rooms…",
        "inhouse": "⏳ Loading in-house…",
        "upcoming": "⏳ Loading upcoming…",
        "edit": "⏳ Loading edit…",
        "status": "⏳ Checking status…",
        "session": "⏳ Checking session…",
        "home": menu_home_text(),
    }.get(action, "⏳ Loading…")
    try:
        if action == "edit":
            edit_message(config, chat_id, message_id, loading_label, with_menu=False)
            rooms, checkins, checkouts = load_edit_context(config, auth)
            edit_message(config, chat_id, message_id, format_edit_menu(rooms, checkins, checkouts), with_menu=False)
            # Attach action buttons in a follow-up (editMessageText + custom markup)
            telegram_call(
                config,
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "reply_markup": edit_menu_keyboard(rooms, checkins, checkouts),
                },
            )
            return
        if action != "home":
            edit_message(config, chat_id, message_id, loading_label, with_menu=False)
        text_out = render_action(config, auth, action)
        edit_message(config, chat_id, message_id, text_out, with_menu=True)
    except Exception as exc:
        LOG.exception("Callback action failed")
        edit_message(config, chat_id, message_id, f"Failed: {type(exc).__name__}: {exc}", with_menu=True)


def is_session_auth_problem(session_note: str) -> bool:
    text = session_note.lower()
    auth_markers = (
        "session: missing",
        "session: expired",
        "401",
        "unauthorized",
        "2fa",
        "login needs",
        "auto-login failed",
        "pms_username/pms_password",
    )
    return any(marker in text for marker in auth_markers)


def monitor_once(config: Config, auth: AuthState) -> None:
    probes = run_health(config)
    current_ok = all(item.ok for item in probes)
    state = load_state(config.state_file)
    previous = state.get("status", "unknown")
    failures = int(state.get("failures", 0))
    previous_session = state.get("session", "unknown")

    session_ok = False
    session_note = ""
    try:
        report = check_session(config, auth, refresh=True)
        session_ok = report.startswith("Session: ACTIVE") or report.startswith("Session: RESTORED")
        session_note = report.splitlines()[0]
    except Exception as exc:
        session_ok = False
        session_note = f"Session check error: {exc}"

    if current_ok:
        if previous == "down" and config.alert_chat_id is not None:
            started = state.get("incident_started_at") or "unknown"
            send_message(
                config,
                config.alert_chat_id,
                f"Zimmerstack RECOVERED\nIncident started: {started}\n{status_text(probes)}",
            )
        state = {
            "status": "up",
            "failures": 0,
            "incident_started_at": None,
            "checked_at": utc_now(),
            "session": "active" if session_ok else "expired",
            "session_note": session_note,
        }
    else:
        failures += 1
        if failures >= config.failure_threshold:
            if previous != "down" and config.alert_chat_id is not None:
                send_message(config, config.alert_chat_id, f"Zimmerstack DOWN\n{status_text(probes)}")
            state = {
                "status": "down",
                "failures": failures,
                "incident_started_at": state.get("incident_started_at") or utc_now(),
                "checked_at": utc_now(),
                "session": "active" if session_ok else "expired",
                "session_note": session_note,
            }
        else:
            state.update({"failures": failures, "checked_at": utc_now(), "session": "active" if session_ok else "expired"})

    if (
        (not session_ok)
        and previous_session != "expired"
        and config.alert_chat_id is not None
        and is_session_auth_problem(session_note)
    ):
        send_message(
            config,
            config.alert_chat_id,
            "PMS session issue\n"
            f"{session_note}\n"
            "Week-off tip: set PMS_USERNAME + PMS_PASSWORD on VPS for auto-refresh "
            "(2FA must be off for that staff login).",
            with_menu=False,
        )
    save_state(config.state_file, state)


def set_bot_commands(config: Config) -> None:
    commands = [
        {"command": "menu", "description": "Show main menu"},
        {"command": "rooms", "description": "Room status"},
        {"command": "room", "description": "Room status"},
        {"command": "inhouse", "description": "In-house guests"},
        {"command": "upcoming", "description": "Upcoming bookings"},
        {"command": "edit", "description": "Clean, check-in, check-out, assign"},
        {"command": "pay", "description": "Collect cash due for a booking"},
        {"command": "book", "description": "Create guest booking"},
        {"command": "hermes", "description": "Ask Hermes to draft or answer"},
        {"command": "ask", "description": "Ask Hermes"},
        {"command": "checkin", "description": "Check in a guest"},
        {"command": "checkout", "description": "Check out a guest"},
        {"command": "clean", "description": "Mark room available"},
        {"command": "status", "description": "Bot health"},
        {"command": "session", "description": "PMS session check"},
        {"command": "help", "description": "Command help"},
    ]
    telegram_call(config, "setMyCommands", {"commands": json.dumps(commands)})


def run_bot(config: Config) -> None:
    if not config.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not config.allowed_user_ids and not config.allowed_chat_ids:
        raise SystemExit("Set TELEGRAM_ALLOWED_USER_IDS and/or TELEGRAM_ALLOWED_CHAT_IDS")

    auth = AuthState.bootstrap(config)
    offset = 0
    next_health = 0.0
    LOG.info(
        "Starting Zimmerstack bot users=%d chats=%d hermes_chats=%d auto_login=%s",
        len(config.allowed_user_ids),
        len(config.allowed_chat_ids),
        len(config.hermes_chat_ids),
        bool(config.pms_username and config.pms_password),
    )
    try:
        set_bot_commands(config)
    except Exception:
        LOG.exception("Failed to set Telegram slash commands")
    while True:
        now = time.monotonic()
        if now >= next_health:
            try:
                monitor_once(config, auth)
            except Exception:
                LOG.exception("Health monitor cycle failed")
            next_health = now + config.check_interval_seconds

        try:
            updates = telegram_call(
                config,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message", "channel_post", "callback_query"]),
                },
            )
            for update in updates or []:
                offset = max(offset, int(update["update_id"]) + 1)
                if update.get("callback_query"):
                    handle_callback(config, auth, update["callback_query"])
                    continue
                message = update.get("message") or update.get("channel_post") or {}
                sender = message.get("from") or {}
                chat = message.get("chat") or {}
                text = message.get("text")
                if isinstance(text, str) and "id" in chat:
                    sender_id = int(sender.get("id") or 0)
                    # Slash commands, reply-keyboard labels, and light PMS free-text routing.
                    if text.startswith("/") or normalize_button_label(text) in BUTTON_TEXT_ACTIONS:
                        handle_command(config, auth, sender_id, int(chat["id"]), text)
                    else:
                        handle_free_text(config, auth, sender_id, int(chat["id"]), text)
        except KeyboardInterrupt:
            raise
        except Exception:
            LOG.exception("Telegram polling cycle failed")
            time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zimmerstack Telegram bot and uptime monitor")
    parser.add_argument("--once", action="store_true", help="Run one health check without Telegram")
    parser.add_argument("--print-config", action="store_true", help="Print non-secret effective configuration")
    parser.add_argument("--check-session", action="store_true", help="Check PMS session and exit")
    parser.add_argument("--set-commands", action="store_true", help="Register Telegram slash commands and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    auth = AuthState.bootstrap(config)

    if args.print_config:
        safe = asdict(config)
        for key in ["telegram_token", "pms_bearer_token", "pms_api_key", "pms_cookie", "pms_password", "openrouter_api_key"]:
            safe[key] = "[SET]" if safe[key] else "[NOT SET]"
        safe["pms_username"] = "[SET]" if safe["pms_username"] else "[NOT SET]"
        safe["allowed_user_ids"] = sorted(safe["allowed_user_ids"])
        safe["allowed_chat_ids"] = sorted(safe["allowed_chat_ids"])
        safe["state_file"] = str(safe["state_file"])
        safe["rooms_file"] = str(safe["rooms_file"])
        safe["auth_file"] = str(safe["auth_file"])
        safe["audit_file"] = str(safe["audit_file"])
        print(json.dumps(safe, indent=2))
        return 0

    if args.check_session:
        print(check_session(config, auth, refresh=True))
        return 0

    if args.set_commands:
        set_bot_commands(config)
        print("Telegram commands updated")
        return 0

    if args.once:
        probes = run_health(config)
        print(json.dumps([asdict(item) for item in probes], indent=2))
        return 0 if all(item.ok for item in probes) else 1

    run_bot(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
