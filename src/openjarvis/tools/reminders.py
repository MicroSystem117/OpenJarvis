"""Reminders tool with background timer daemon delivering alerts directly to Telegram."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from openjarvis.connectors.telegram_notify import send_telegram_notification
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "reminders"
_REMINDERS_FILE = get_config_dir() / "reminders.json"
_LOCAL_TZ = ZoneInfo("America/Caracas")

_reminders_thread: Optional[threading.Thread] = None
_reminders_lock = threading.Lock()


def _load_reminders() -> List[Dict[str, Any]]:
    """Load reminder entries from JSON store."""
    if not _REMINDERS_FILE.is_file():
        return []
    try:
        with open(_REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Error reading reminders file: %s", exc)
        return []


def _save_reminders(data: List[Dict[str, Any]]) -> None:
    """Save reminder entries to JSON store."""
    try:
        _REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.error("Error saving reminders file: %s", exc)


def _reminders_daemon_loop() -> None:
    """Background polling loop checking for due reminders every 10 seconds."""
    logger.info("Reminders background watcher started.")
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            with _reminders_lock:
                reminders = _load_reminders()
                changed = False

                for r in reminders:
                    if r.get("status") == "pending":
                        due_time = r.get("due_time", "")
                        if due_time and due_time <= now_iso:
                            msg = r.get("message", "Recordatorio programado")
                            tg_text = (
                                f"🔔 *Recordatorio de Jarvis*\n\n"
                                f"{msg}\n\n"
                                f"_(Programado para: {r.get('human_time', due_time)})_"
                            )
                            send_telegram_notification(tg_text)
                            r["status"] = "completed"
                            r["completed_at"] = now_iso
                            changed = True

                if changed:
                    _save_reminders(reminders)

        except Exception as exc:
            logger.debug("Reminders loop error: %s", exc)

        time.sleep(10)


def ensure_reminders_daemon_started() -> None:
    """Ensure reminder watcher thread runs in the background."""
    global _reminders_thread
    with _reminders_lock:
        if _reminders_thread is None or not _reminders_thread.is_alive():
            _reminders_thread = threading.Thread(
                target=_reminders_daemon_loop,
                daemon=True,
                name="jarvis-reminders-watcher",
            )
            _reminders_thread.start()


ensure_reminders_daemon_started()


@ToolRegistry.register(_TOOL_NAME)
class RemindersTool(BaseTool):
    """Tool to create, list, and manage proactive natural language reminders via Telegram."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Create and manage reminders delivered proactively to Telegram. "
                "Actions: 'create_reminder' with 'message' and either 'delay_minutes' "
                "(e.g. 15, 60) or 'due_datetime' (ISO string or 'HH:MM'); "
                "'list_reminders' to see pending or completed reminders; "
                "'cancel_reminder' with 'reminder_id' to cancel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create_reminder", "list_reminders", "cancel_reminder"],
                        "description": "Action to perform.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The reminder note or task to be notified of.",
                    },
                    "delay_minutes": {
                        "type": "number",
                        "description": "Minutes from now when reminder should trigger (e.g. 30, 1.5).",
                    },
                    "due_datetime": {
                        "type": "string",
                        "description": "Target time or ISO string (e.g. '18:30' or '2026-11-22T09:00:00').",
                    },
                    "reminder_id": {
                        "type": "string",
                        "description": "ID of reminder to cancel.",
                    },
                    "include_completed": {
                        "type": "boolean",
                        "description": "Include completed reminders in list (default false).",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
            category="productivity",
            requires_confirmation=False,
            timeout_seconds=15.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

        try:
            if action == "create_reminder":
                return self._create_reminder(params)
            elif action == "list_reminders":
                return self._list_reminders(params)
            elif action == "cancel_reminder":
                return self._cancel_reminder(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Reminders tool error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Reminders error: {exc}", success=False)

    def _create_reminder(self, params: Dict[str, Any]) -> ToolResult:
        message = params.get("message", "").strip()
        if not message:
            return ToolResult(tool_name=_TOOL_NAME, content="'message' is required for create_reminder.", success=False)

        delay_min = params.get("delay_minutes")
        due_str = params.get("due_datetime", "").strip()

        now_utc = datetime.now(timezone.utc)
        target_utc: Optional[datetime] = None
        human_time_str = ""

        if delay_min is not None:
            mins = float(delay_min)
            target_utc = now_utc + timedelta(minutes=mins)
            target_local = target_utc.astimezone(_LOCAL_TZ)
            human_time_str = target_local.strftime("%Y-%m-%d %I:%M %p")
        elif due_str:
            # Try parsing time formats
            if ":" in due_str and "T" not in due_str and "-" not in due_str:
                parts = due_str.split(":")
                hour, minute = int(parts[0]), int(parts[1][:2])
                now_local = datetime.now(_LOCAL_TZ)
                target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target_local <= now_local:
                    target_local += timedelta(days=1)
                target_utc = target_local.astimezone(timezone.utc)
                human_time_str = target_local.strftime("%Y-%m-%d %I:%M %p")
            else:
                try:
                    dt = datetime.fromisoformat(due_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_LOCAL_TZ)
                    target_utc = dt.astimezone(timezone.utc)
                    human_time_str = target_utc.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    pass

        if target_utc is None:
            return ToolResult(
                tool_name=_TOOL_NAME,
                content="Debes especificar 'delay_minutes' (ej. 15) o 'due_datetime' válido (ej. '17:30' o '2026-11-22T09:00:00').",
                success=False,
            )

        rid = uuid.uuid4().hex[:8]
        entry = {
            "id": rid,
            "message": message,
            "created_at": now_utc.isoformat(),
            "due_time": target_utc.isoformat(),
            "human_time": human_time_str,
            "status": "pending",
        }

        with _reminders_lock:
            items = _load_reminders()
            items.append(entry)
            _save_reminders(items)

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=(
                f"✅ Recordatorio agendado [ID: `{rid}`]:\n"
                f"• **Mensaje:** {message}\n"
                f"• **Fecha y Hora:** {human_time_str} (Venezuela / Caracas)\n\n"
                f"Te avisaré automáticamente por Telegram cuando sea el momento."
            ),
            success=True,
            metadata=entry,
        )

    def _list_reminders(self, params: Dict[str, Any]) -> ToolResult:
        inc_comp = bool(params.get("include_completed", False))
        with _reminders_lock:
            items = _load_reminders()

        if not inc_comp:
            items = [r for r in items if r.get("status") == "pending"]

        if not items:
            return ToolResult(tool_name=_TOOL_NAME, content="No tienes recordatorios pendientes.", success=True)

        lines = []
        for r in items:
            rid = r.get("id")
            msg = r.get("message")
            ht = r.get("human_time", r.get("due_time"))
            st = r.get("status")
            lines.append(f"• [`{st}`] **{msg}** - Programado: {ht} [ID: `{rid}`]")

        return ToolResult(tool_name=_TOOL_NAME, content="📋 **Tus Recordatorios:**\n\n" + "\n".join(lines), success=True)

    def _cancel_reminder(self, params: Dict[str, Any]) -> ToolResult:
        rid = params.get("reminder_id", "").strip()
        if not rid:
            return ToolResult(tool_name=_TOOL_NAME, content="'reminder_id' is required.", success=False)

        with _reminders_lock:
            items = _load_reminders()
            found = False
            for r in items:
                if r.get("id") == rid and r.get("status") == "pending":
                    r["status"] = "cancelled"
                    found = True
                    break
            if found:
                _save_reminders(items)
                return ToolResult(tool_name=_TOOL_NAME, content=f"Recordatorio `{rid}` cancelado exitosamente.", success=True)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"No se encontró un recordatorio pendiente con ID `{rid}`.", success=False)


__all__ = ["RemindersTool"]
