"""Telegram notification helper for background alerts, reminders, and daily digests."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_ID = os.environ.get("OPENJARVIS_TELEGRAM_CHAT_ID", "")
_DEFAULT_BOT_TOKEN = os.environ.get("OPENJARVIS_TELEGRAM_BOT_TOKEN", "")


def get_telegram_config() -> tuple[str, str]:
    """Retrieve bot token and default allowed chat id from config or environment."""
    token = _DEFAULT_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = _DEFAULT_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")

    try:
        import tomllib
        from openjarvis.core.paths import get_config_dir

        cfg_path = get_config_dir() / "config.toml"
        if cfg_path.is_file():
            with open(cfg_path, "rb") as f:
                raw = tomllib.load(f)
                tg = raw.get("channel", {}).get("telegram", {})
                if tg.get("bot_token"):
                    token = tg["bot_token"]
                if tg.get("allowed_chat_ids"):
                    chat_id = str(tg["allowed_chat_ids"]).split(",")[0].strip()
    except Exception as exc:
        logger.debug("Error loading telegram config: %s", exc)

    return token, chat_id


def send_telegram_notification(
    text: str,
    chat_id: Optional[str] = None,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a proactive notification to Telegram with automatic markdown fallback."""
    token, default_chat_id = get_telegram_config()
    target_chat = str(chat_id or default_chat_id)

    if not token or not target_chat:
        logger.warning("Telegram token or chat_id is missing, cannot send notification.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": target_chat,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = httpx.post(url, json=payload, timeout=12.0)
        if resp.status_code == 200:
            return True

        # Fallback to plain text if Markdown parsing failed (400 Bad Request)
        if resp.status_code == 400 and parse_mode:
            logger.info("Retrying Telegram notification without Markdown formatting.")
            payload.pop("parse_mode", None)
            retry_resp = httpx.post(url, json=payload, timeout=12.0)
            return retry_resp.status_code == 200

        logger.warning("Telegram notification failed (%s): %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.warning("Telegram notification exception: %s", exc)
        return False


__all__ = ["get_telegram_config", "send_telegram_notification"]
