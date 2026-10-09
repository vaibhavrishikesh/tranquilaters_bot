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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        "• /status — PMS/API health\n\n"
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
                send_message(config, chat_id, hermes_reply(config, text))
        except KeyboardInterrupt:
            raise
        except Exception:
            LOG.exception("Hermes polling cycle failed")
            time.sleep(5)


if __name__ == "__main__":
    run(Config.from_env())
