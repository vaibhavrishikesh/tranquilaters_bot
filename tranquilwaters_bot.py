#!/usr/bin/env python3
"""Telegram Hermes/OpenRouter assistant bot.

Separate from the Lily's Desk PMS bot. This bot never performs PMS writes.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except Exception:  # Google Docs integration is optional until dependencies are installed.
    service_account = None
    build = None

import pms_readonly as pms_bot

TELEGRAM_LIMIT = 4096
LOG = logging.getLogger("hermes-bot")


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
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    openrouter_api_key: str
    openrouter_model: str
    request_timeout_seconds: int
    assistant_name: str
    system_prompt: str
    google_credentials_file: str
    google_docs_folder_id: str
    google_docs_report_id: str
    state_file: Path
    notes_file: Path
    auto_report_enabled: bool
    auto_report_time: str
    auto_report_chat_id: int | None

    @classmethod
    def from_env(cls) -> "Config":
        default_prompt = (
            "You are Hermes, a concise helpful assistant for TranquilWaters and Lily's Retreats staff. "
            "Answer in clear English only. Keep replies practical and short unless the user asks for detail. "
            "You can help draft guest replies, summarize notes, suggest operational wording, and answer general questions. "
            "Do not perform PMS writes, bookings, payments, check-ins, or check-outs. "
            "For PMS operations, tell staff to use the Lily's Desk PMS bot commands such as /rooms, /book, /pay, /checkin, /checkout, or /clean. "
            "Never reveal API keys, passcodes, passwords, tokens, or private credentials."
        )
        return cls(
            telegram_token=os.getenv("HERMES_TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_chat_ids=parse_int_set(os.getenv("HERMES_ALLOWED_CHAT_IDS", "")),
            allowed_user_ids=parse_int_set(os.getenv("HERMES_ALLOWED_USER_IDS", "")),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
            request_timeout_seconds=max(2, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12") or "12")),
            assistant_name=os.getenv("HERMES_ASSISTANT_NAME", "Hermes").strip() or "Hermes",
            system_prompt=os.getenv("HERMES_SYSTEM_PROMPT", default_prompt).strip() or default_prompt,
            google_credentials_file=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip(),
            google_docs_folder_id=os.getenv("GOOGLE_DOCS_FOLDER_ID", "").strip(),
            google_docs_report_id=os.getenv("GOOGLE_DOCS_REPORT_ID", "").strip(),
            state_file=Path(os.getenv("HERMES_STATE_FILE", "/var/lib/tranquilwaters-bot/state.json")),
            notes_file=Path(os.getenv("HERMES_NOTES_FILE", "/var/lib/tranquilwaters-bot/team-notes.jsonl")),
            auto_report_enabled=os.getenv("AUTO_DOC_REPORT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            auto_report_time=os.getenv("AUTO_DOC_REPORT_TIME", "09:00").strip() or "09:00",
            auto_report_chat_id=int(os.getenv("AUTO_DOC_REPORT_CHAT_ID", "0") or "0") or None,
        )


def http_request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None, timeout: int = 12) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def telegram_call(config: Config, method: str, payload: dict[str, Any], *, timeout: int | None = None) -> Any:
    if not config.telegram_token:
        raise RuntimeError("HERMES_TELEGRAM_BOT_TOKEN is not configured")
    status, body = http_request(
        f"https://api.telegram.org/bot{config.telegram_token}/{method}",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        timeout=timeout or config.request_timeout_seconds,
    )
    decoded = json.loads(body.decode("utf-8", errors="replace") or "{}")
    if status < 200 or status >= 300 or not decoded.get("ok", False):
        raise RuntimeError(f"Telegram {method} failed HTTP {status}: {decoded}")
    return decoded.get("result")


def send_message(config: Config, chat_id: int, text: str) -> None:
    chunks = [text[i : i + TELEGRAM_LIMIT - 100] for i in range(0, len(text), TELEGRAM_LIMIT - 100)] or [""]
    for chunk in chunks:
        telegram_call(
            config,
            "sendMessage",
            {"chat_id": str(chat_id), "text": chunk, "disable_web_page_preview": True},
        )


def is_authorized(config: Config, user_id: int, chat_id: int) -> bool:
    if chat_id in config.allowed_chat_ids:
        return True
    if user_id and user_id in config.allowed_user_ids:
        return True
    return not config.allowed_chat_ids and not config.allowed_user_ids


def hermes_reply(config: Config, text: str) -> str:
    if not config.openrouter_api_key:
        return "Hermes is not configured yet: OPENROUTER_API_KEY is missing."
    payload = {
        "model": config.openrouter_model or "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": text.strip()[:2000]},
        ],
        "temperature": 0.35,
        "max_tokens": 500,
    }
    status, body = http_request(
        "https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        headers={
            "Authorization": f"Bearer {config.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/TranquilWaters_bot",
            "X-Title": config.assistant_name,
        },
        data=json.dumps(payload).encode("utf-8"),
        timeout=min(max(6, config.request_timeout_seconds), 20),
    )
    if status < 200 or status >= 300:
        LOG.warning("OpenRouter failed HTTP %s: %s", status, body[:500])
        return "Hermes is not available right now. Please try again in a minute."
    try:
        decoded = json.loads(body.decode("utf-8", errors="replace"))
        content = str(decoded["choices"][0]["message"]["content"]).strip()
    except Exception:
        LOG.exception("OpenRouter reply parse failed")
        return "Hermes reply could not be read. Please try again."
    return content or "Hermes did not return a reply."


def help_text(config: Config) -> str:
    return (
        f"{config.assistant_name}\n"
        "Send any question or draft request in English.\n"
        "Examples:\n"
        "• Draft a polite reply for early check-in\n"
        "• Summarize this guest complaint\n"
        "• Write a short WhatsApp message for payment reminder\n\n"
        "Read-only PMS reports:\n"
        "• /rooms — current room status\n"
        "• /inhouse — guests currently checked in\n"
        "• /upcoming — upcoming bookings\n"
        "• /report — rooms, in-house, and upcoming summary\n"
        "• /status — PMS/API health\n"
        "• /doc_report — append today's PMS summary to Google Docs\n"
        "• /doc_status — check Google Docs setup\n"
        "• /note <text> — save a team note locally\n"
        "• /notes — show recent team notes\n"
        "• /doc_notes — append recent team notes to Google Docs\n\n"
        "PMS write actions stay in the Lily's Desk PMS bot."
    )


def pms_read_context() -> tuple[pms_bot.Config, pms_bot.AuthState]:
    config = pms_bot.Config.from_env()
    auth = pms_bot.AuthState.bootstrap(config)
    return config, auth


def pms_report(action: str) -> str:
    config, auth = pms_read_context()
    if action == "status":
        return pms_bot.status_text(pms_bot.run_health(config))
    if action == "session":
        return pms_bot.check_session(config, auth, refresh=True)
    if action in {"rooms", "inhouse", "upcoming"}:
        return pms_bot.render_action(config, auth, action)
    if action == "report":
        sections = []
        for item in ("rooms", "inhouse", "upcoming"):
            try:
                sections.append(pms_bot.render_action(config, auth, item))
            except Exception as exc:
                LOG.exception("PMS report section failed: %s", item)
                sections.append(f"{item.title()} failed: {type(exc).__name__}: {exc}")
        return "\n\n".join(sections)
    return "Unknown PMS report."


def pms_report_sections() -> dict[str, str]:
    pms_config, auth = pms_read_context()
    result: dict[str, str] = {}
    for item in ("rooms", "inhouse", "upcoming"):
        try:
            result[item] = pms_bot.render_action(pms_config, auth, item).strip()
        except Exception as exc:
            LOG.exception("PMS report section failed: %s", item)
            result[item] = f"{item.title()} failed: {type(exc).__name__}: {exc}"
    return result


def section_count(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if "·" in first:
        return first.rsplit("·", 1)[-1].strip()
    return "-"


def room_status_counts(rooms_text: str) -> dict[str, int]:
    counts = {"available": 0, "occupied": 0, "blocked": 0, "other": 0}
    for line in rooms_text.splitlines()[2:]:
        upper = line.upper()
        if "AVAILABLE" in upper:
            counts["available"] += 1
        elif "OCCUPIED" in upper:
            counts["occupied"] += 1
        elif "FULL" in upper or "HOLD" in upper or "BLOCK" in upper:
            counts["blocked"] += 1
        elif line.strip():
            counts["other"] += 1
    return counts


def indent_section(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(f"  {line}" if line else "" for line in lines)


def professional_doc_report() -> str:
    sections = pms_report_sections()
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    room_counts = room_status_counts(sections.get("rooms", ""))
    inhouse_count = section_count(sections.get("inhouse", ""))
    upcoming_count = section_count(sections.get("upcoming", ""))
    divider = "─" * 62
    return (
        f"\n\n{divider}\n"
        f"TRANQUILWATERS PMS DAILY REPORT\n"
        f"Generated: {now}\n"
        f"Source: Zimmerstack PMS via TranquilWaters Bot\n"
        f"{divider}\n\n"
        "EXECUTIVE SUMMARY\n"
        f"• Available rooms: {room_counts['available']}\n"
        f"• Occupied rooms: {room_counts['occupied']}\n"
        f"• Blocked / held rooms: {room_counts['blocked']}\n"
        f"• In-house guests: {inhouse_count}\n"
        f"• Upcoming bookings: {upcoming_count}\n\n"
        "ROOM STATUS\n"
        f"{indent_section(sections.get('rooms', 'No room data.'))}\n\n"
        "IN-HOUSE GUESTS\n"
        f"{indent_section(sections.get('inhouse', 'No in-house data.'))}\n\n"
        "UPCOMING BOOKINGS\n"
        f"{indent_section(sections.get('upcoming', 'No upcoming data.'))}\n\n"
        "TEAM NOTES\n"
        "• Review unpaid or pending guest balances before checkout.\n"
        "• Use Lily's Desk PMS bot for booking, payment, check-in, checkout, and room status write actions.\n"
        "• This document is an operational log; PMS remains the source of truth.\n"
    )


GOOGLE_DOC_SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)


def google_docs_ready(config: Config) -> tuple[bool, str]:
    if service_account is None or build is None:
        return False, "Google client libraries are not installed. Install google-api-python-client and google-auth."
    if not config.google_credentials_file:
        return False, "GOOGLE_APPLICATION_CREDENTIALS is not set."
    path = Path(config.google_credentials_file)
    if not path.exists():
        return False, f"Google credentials file not found: {path}"
    if not config.google_docs_report_id and not config.google_docs_folder_id:
        return False, "Set GOOGLE_DOCS_REPORT_ID for an existing doc, or GOOGLE_DOCS_FOLDER_ID to create report docs in a Drive folder."
    return True, "Google Docs integration is configured."


def google_credentials(config: Config):
    ready, detail = google_docs_ready(config)
    if not ready:
        raise RuntimeError(detail)
    return service_account.Credentials.from_service_account_file(
        config.google_credentials_file,
        scopes=list(GOOGLE_DOC_SCOPES),
    )


def docs_service(config: Config):
    return build("docs", "v1", credentials=google_credentials(config), cache_discovery=False)


def drive_service(config: Config):
    return build("drive", "v3", credentials=google_credentials(config), cache_discovery=False)


def create_google_doc(config: Config, title: str) -> str:
    metadata: dict[str, Any] = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if config.google_docs_folder_id:
        metadata["parents"] = [config.google_docs_folder_id]
    created = drive_service(config).files().create(
        body=metadata,
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return str(created["id"])


def document_end_index(document: dict[str, Any]) -> int:
    content = document.get("body", {}).get("content", [])
    if not content:
        return 1
    return max(1, int(content[-1].get("endIndex", 1)) - 1)


def append_google_doc(config: Config, document_id: str, text: str) -> str:
    service = docs_service(config)
    document = service.documents().get(documentId=document_id).execute()
    index = document_end_index(document)
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{"insertText": {"location": {"index": index}, "text": text}}]},
    ).execute()
    return f"https://docs.google.com/document/d/{document_id}/edit"


def save_pms_report_to_google_doc(config: Config) -> str:
    ready, detail = google_docs_ready(config)
    if not ready:
        return f"Google Docs not ready: {detail}"
    report = professional_doc_report()
    title = f"TranquilWaters PMS Report {datetime.now().strftime('%Y-%m-%d')}"
    document_id = config.google_docs_report_id or create_google_doc(config, title)
    link = append_google_doc(config, document_id, report)
    return f"Saved PMS report to Google Docs:\n{link}"


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def ist_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata"))


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return max(0, min(23, int(hour))), max(0, min(59, int(minute)))


def auto_report_due(config: Config, now: datetime) -> bool:
    if not config.auto_report_enabled:
        return False
    try:
        hour, minute = parse_hhmm(config.auto_report_time)
    except Exception:
        LOG.warning("Invalid AUTO_DOC_REPORT_TIME=%s", config.auto_report_time)
        return False
    if (now.hour, now.minute) < (hour, minute):
        return False
    state = load_json_file(config.state_file)
    return state.get("last_auto_doc_report_date") != now.date().isoformat()


def mark_auto_report_done(config: Config, now: datetime) -> None:
    state = load_json_file(config.state_file)
    state["last_auto_doc_report_date"] = now.date().isoformat()
    state["last_auto_doc_report_at"] = now.isoformat()
    save_json_file(config.state_file, state)


def auto_report_chat_id(config: Config) -> int | None:
    if config.auto_report_chat_id:
        return config.auto_report_chat_id
    if config.allowed_chat_ids:
        return sorted(config.allowed_chat_ids)[0]
    return None


def maybe_run_auto_report(config: Config) -> None:
    now = ist_now()
    if not auto_report_due(config, now):
        return
    chat_id = auto_report_chat_id(config)
    try:
        result = save_pms_report_to_google_doc(config)
        mark_auto_report_done(config, now)
        if chat_id:
            send_message(config, chat_id, f"Auto daily PMS report saved.\n{result}")
        LOG.info("Auto daily PMS report saved for %s", now.date().isoformat())
    except Exception:
        LOG.exception("Auto daily PMS report failed")


def append_team_note(config: Config, chat_id: int, user_id: int, author: str, note: str) -> str:
    note = note.strip()
    if not note:
        return "Usage: /note <team note>"
    config.notes_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": ist_now().isoformat(timespec="seconds"),
        "chat_id": chat_id,
        "user_id": user_id,
        "author": author or "Team",
        "note": note[:1200],
    }
    with config.notes_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return "Team note saved."


def recent_team_notes(config: Config, limit: int = 10) -> list[dict[str, Any]]:
    try:
        lines = config.notes_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    notes: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            notes.append(item)
    return notes


def render_team_notes(config: Config, limit: int = 10) -> str:
    notes = recent_team_notes(config, limit)
    if not notes:
        return "No team notes saved yet."
    lines = [f"Recent team notes · {len(notes)}"]
    for item in notes:
        when = str(item.get("created_at", ""))[:16].replace("T", " ")
        author = str(item.get("author") or "Team")
        note = str(item.get("note") or "").strip()
        lines.append(f"• {when} · {author}: {note}")
    return "\n".join(lines)


def save_team_notes_to_google_doc(config: Config) -> str:
    ready, detail = google_docs_ready(config)
    if not ready:
        return f"Google Docs not ready: {detail}"
    notes_text = render_team_notes(config, 20)
    now = ist_now().strftime("%d %b %Y, %I:%M %p")
    text = f"\n\nTEAM NOTES UPDATE\nGenerated: {now}\n\n{notes_text}\n"
    document_id = config.google_docs_report_id or create_google_doc(config, f"TranquilWaters Team Notes {ist_now().date().isoformat()}")
    link = append_google_doc(config, document_id, text)
    return f"Saved team notes to Google Docs:\n{link}"


def set_commands(config: Config) -> None:
    telegram_call(
        config,
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Show Hermes help"},
                {"command": "help", "description": "Show Hermes examples"},
                {"command": "rooms", "description": "Read-only PMS room status"},
                {"command": "inhouse", "description": "Read-only in-house guests"},
                {"command": "upcoming", "description": "Read-only upcoming bookings"},
                {"command": "report", "description": "Read-only PMS daily summary"},
                {"command": "status", "description": "PMS/API health"},
                {"command": "session", "description": "PMS session check"},
                {"command": "doc_report", "description": "Save PMS summary to Google Docs"},
                {"command": "doc_status", "description": "Check Google Docs setup"},
                {"command": "note", "description": "Save a team note"},
                {"command": "notes", "description": "Show recent team notes"},
                {"command": "doc_notes", "description": "Save recent notes to Google Docs"},
            ]
        },
    )


def run(config: Config) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    LOG.info("Starting Hermes bot chats=%d users=%d model=%s", len(config.allowed_chat_ids), len(config.allowed_user_ids), config.openrouter_model)
    try:
        set_commands(config)
    except Exception:
        LOG.exception("Failed to set Hermes commands")
    offset = 0
    while True:
        try:
            poll_timeout = 25
            updates = telegram_call(
                config,
                "getUpdates",
                {"offset": offset, "timeout": poll_timeout, "allowed_updates": ["message", "channel_post"]},
                timeout=max(config.request_timeout_seconds, poll_timeout + 10),
            )
            maybe_run_auto_report(config)
            for update in updates or []:
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or update.get("channel_post") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                text = message.get("text")
                if not isinstance(text, str) or "id" not in chat:
                    continue
                chat_id = int(chat["id"])
                user_id = int(sender.get("id") or 0)
                if not is_authorized(config, user_id, chat_id):
                    LOG.warning("Ignored unauthorized chat_id=%s user_id=%s", chat_id, user_id)
                    continue
                command = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else ""
                if command in {"/start", "/help"}:
                    send_message(config, chat_id, help_text(config))
                    continue
                if command in {"/rooms", "/room", "/inhouse", "/upcoming", "/report", "/status", "/session"}:
                    action = command.lstrip("/")
                    if action == "room":
                        action = "rooms"
                    try:
                        send_message(config, chat_id, pms_report(action))
                    except Exception as exc:
                        LOG.exception("PMS read-only command failed: %s", command)
                        send_message(config, chat_id, f"{command} failed: {type(exc).__name__}: {exc}")
                    continue
                if command == "/doc_status":
                    ready, detail = google_docs_ready(config)
                    send_message(config, chat_id, f"Google Docs: {'READY' if ready else 'NOT READY'}\n{detail}")
                    continue
                if command == "/doc_report":
                    try:
                        send_message(config, chat_id, save_pms_report_to_google_doc(config))
                    except Exception as exc:
                        LOG.exception("Google Docs report failed")
                        send_message(config, chat_id, f"/doc_report failed: {type(exc).__name__}: {exc}")
                    continue
                if command == "/note":
                    author = str(sender.get("first_name") or sender.get("username") or "Team")
                    note = text.strip().split(maxsplit=1)[1] if len(text.strip().split(maxsplit=1)) > 1 else ""
                    send_message(config, chat_id, append_team_note(config, chat_id, user_id, author, note))
                    continue
                if command == "/notes":
                    send_message(config, chat_id, render_team_notes(config))
                    continue
                if command == "/doc_notes":
                    try:
                        send_message(config, chat_id, save_team_notes_to_google_doc(config))
                    except Exception as exc:
                        LOG.exception("Google Docs notes failed")
                        send_message(config, chat_id, f"/doc_notes failed: {type(exc).__name__}: {exc}")
                    continue
                send_message(config, chat_id, hermes_reply(config, text))
        except KeyboardInterrupt:
            raise
        except Exception:
            LOG.exception("Hermes polling cycle failed")
            time.sleep(5)


if __name__ == "__main__":
    run(Config.from_env())
