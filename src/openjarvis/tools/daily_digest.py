"""Daily morning briefing and proactive digest tool for Telegram."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from openjarvis.connectors.telegram_notify import send_telegram_notification
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.google_calendar import GoogleCalendarTool
from openjarvis.tools.google_gmail import GoogleGmailTool
from openjarvis.tools.google_tasks import GoogleTasksTool
from openjarvis.tools.system_sentinel import get_system_snapshot

logger = logging.getLogger(__name__)

_TOOL_NAME = "daily_digest"
_STATE_FILE = get_config_dir() / "digest_state.json"
_LOCAL_TZ = ZoneInfo("America/Caracas")

_digest_thread: Optional[threading.Thread] = None
_digest_lock = threading.Lock()


def _load_state() -> Dict[str, Any]:
    if not _STATE_FILE.is_file():
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.warning("Could not save digest state: %s", exc)


def build_daily_briefing() -> str:
    """Aggregate calendar events, unread emails, tasks, and system metrics into a clean briefing."""
    now_local = datetime.now(_LOCAL_TZ)
    date_header = now_local.strftime("%A, %d de %B de %Y")

    # 1. Calendar
    events_text = "Sin eventos registrados para hoy."
    try:
        cal = GoogleCalendarTool()
        c_res = cal.execute(action="list_events", days_ahead=1)
        if c_res.success and c_res.content and "No hay eventos" not in c_res.content:
            events_text = c_res.content
    except Exception as exc:
        logger.debug("Digest calendar fetch error: %s", exc)

    # 2. Gmail
    gmail_text = "Bandeja al día, sin correos urgentes."
    try:
        gmail = GoogleGmailTool()
        g_res = gmail.execute(action="search_emails", query="is:unread", max_results=3)
        if g_res.success and g_res.content and "No se encontraron" not in g_res.content:
            gmail_text = g_res.content
    except Exception as exc:
        logger.debug("Digest gmail fetch error: %s", exc)

    # 3. Google Tasks
    tasks_text = "Sin tareas pendientes en Google Tasks."
    try:
        tasks = GoogleTasksTool()
        t_res = tasks.execute(action="list_tasks")
        if t_res.success and t_res.content and "No hay tareas" not in t_res.content:
            tasks_text = t_res.content
    except Exception as exc:
        logger.debug("Digest tasks fetch error: %s", exc)

    # 4. System Health
    snap = get_system_snapshot()
    cpu = snap.get("cpu", {})
    mem = snap.get("memory", {})
    disk = snap.get("disk", {})
    batt = snap.get("battery", {})

    batt_str = f"{batt.get('percent', 100)}%" if batt else "AC"

    briefing = (
        f"🌅 *Buenos días, Elías.* Aquí tienes tu resumen matutino:\n\n"
        f"📅 *{date_header}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗓️ *Agenda de Hoy y Mañana:*\n{events_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📬 *Correos Recientes No Leídos:*\n{gmail_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Tareas Pendientes:*\n{tasks_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ *Estado de la Máquina ({snap.get('node', 'Laptop')}):*\n"
        f"• CPU: `{cpu.get('percent', 0)}%` | RAM: `{mem.get('percent', 0)}%` ({mem.get('used_gb', 0)}/{mem.get('total_gb', 0)} GB)\n"
        f"• Disco NVMe: `{disk.get('root_percent', 0)}%` usado | Batería: `{batt_str}`\n\n"
        f"_Todo en orden para comenzar. ¿En qué trabajamos hoy?_"
    )
    return briefing


def _morning_digest_loop() -> None:
    """Background loop that fires the daily briefing at 07:00 AM America/Caracas."""
    logger.info("Morning digest scheduler thread started.")
    while True:
        try:
            now_local = datetime.now(_LOCAL_TZ)
            today_str = now_local.strftime("%Y-%m-%d")

            # Check if it's 7:00 AM (between 7:00 and 7:05)
            if now_local.hour == 7 and 0 <= now_local.minute <= 5:
                state = _load_state()
                last_sent = state.get("last_digest_date")

                if last_sent != today_str:
                    logger.info("Dispatching scheduled morning briefing for %s", today_str)
                    briefing = build_daily_briefing()
                    success = send_telegram_notification(briefing)
                    if success:
                        state["last_digest_date"] = today_str
                        state["last_sent_at"] = now_local.isoformat()
                        _save_state(state)
        except Exception as exc:
            logger.debug("Morning digest loop error: %s", exc)

        time.sleep(30)


def ensure_digest_scheduler_started() -> None:
    """Ensure morning digest scheduler runs in background."""
    global _digest_thread
    with _digest_lock:
        if _digest_thread is None or not _digest_thread.is_alive():
            _digest_thread = threading.Thread(
                target=_morning_digest_loop,
                daemon=True,
                name="jarvis-daily-digest",
            )
            _digest_thread.start()


ensure_digest_scheduler_started()


@ToolRegistry.register(_TOOL_NAME)
class DailyDigestTool(BaseTool):
    """Tool to generate or proactively dispatch the daily executive briefing via Telegram."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Generate or dispatch the morning daily briefing containing calendar events, "
                "unread emails, pending tasks, and hardware status. Actions: "
                "'get_briefing' to fetch the current briefing text; "
                "'send_morning_briefing' to dispatch it directly to Telegram."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get_briefing", "send_morning_briefing"],
                        "description": "Action to perform.",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Optional Telegram chat ID to send briefing to (defaults to Elías).",
                    },
                },
                "required": ["action"],
            },
            category="productivity",
            requires_confirmation=False,
            timeout_seconds=30.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action") or params.get("operation") or "get_briefing"
        action = str(action).lower().strip()

        try:
            briefing = build_daily_briefing()
            if action == "get_briefing":
                return ToolResult(tool_name=_TOOL_NAME, content=briefing, success=True)
            elif action == "send_morning_briefing":
                chat_id = params.get("chat_id")
                sent = send_telegram_notification(briefing, chat_id=chat_id)
                if sent:
                    return ToolResult(tool_name=_TOOL_NAME, content="Resumen diario enviado exitosamente por Telegram.", success=True)
                else:
                    return ToolResult(tool_name=_TOOL_NAME, content="No se pudo enviar el resumen a Telegram.", success=False)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Daily digest error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Daily digest error: {exc}", success=False)


__all__ = ["DailyDigestTool", "build_daily_briefing"]
