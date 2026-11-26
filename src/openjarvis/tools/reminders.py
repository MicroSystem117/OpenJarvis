"""Reminders tool with background timer daemon delivering alerts directly to Telegram."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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


def _parse_reminder_target(params: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(_LOCAL_TZ)

    # 1. Numeric or string fields for minutes delay
    for k in ("delay_minutes", "delay", "minutes", "delay_min", "delay_mins", "mins"):
        v = params.get(k)
        if v is not None:
            if isinstance(v, (int, float)):
                target_utc = now_utc + timedelta(minutes=float(v))
                target_local = target_utc.astimezone(_LOCAL_TZ)
                return target_utc, target_local.strftime("%Y-%m-%d %I:%M %p")
            elif isinstance(v, str) and v.strip():
                try:
                    val = float(v.strip())
                    target_utc = now_utc + timedelta(minutes=val)
                    target_local = target_utc.astimezone(_LOCAL_TZ)
                    return target_utc, target_local.strftime("%Y-%m-%d %I:%M %p")
                except ValueError:
                    pass

    # 2. Check hours field
    for k in ("hours", "delay_hours", "delay_hr", "hrs"):
        v = params.get(k)
        if v is not None:
            try:
                hrs = float(v)
                target_utc = now_utc + timedelta(hours=hrs)
                target_local = target_utc.astimezone(_LOCAL_TZ)
                return target_utc, target_local.strftime("%Y-%m-%d %I:%M %p")
            except ValueError:
                pass

    # 3. Check natural language or string time in any date/due/delay/time field
    candidates = []
    for k in (
        "due_datetime", "due_time", "due_date", "due", "time", "datetime",
        "delay_minutes", "delay", "when", "target_time"
    ):
        v = params.get(k)
        if v and isinstance(v, str):
            candidates.append(v.strip())

    for s in candidates:
        # Regex hours: 'in 1 hour', '1 hour', 'dentro de 1 hora', 'en 1 hora', '2 horas', '1h'
        m_hr = re.search(r"(?:(?:en|dentro\s+de|in)\s+)?(\d+(?:\.\d+)?)\s*(?:hours?|horas?|hrs?|h)\b", s, re.I)
        if m_hr:
            hrs = float(m_hr.group(1))
            target_utc = now_utc + timedelta(hours=hrs)
            target_local = target_utc.astimezone(_LOCAL_TZ)
            return target_utc, target_local.strftime("%Y-%m-%d %I:%M %p")

        # Regex minutes: 'in 30 mins', '30 minutes', 'en 15 minutos', '15m'
        m_mn = re.search(r"(?:(?:en|dentro\s+de|in)\s+)?(\d+(?:\.\d+)?)\s*(?:minutes?|minutos?|mins?|m)\b", s, re.I)
        if m_mn:
            mns = float(m_mn.group(1))
            target_utc = now_utc + timedelta(minutes=mns)
            target_local = target_utc.astimezone(_LOCAL_TZ)
            return target_utc, target_local.strftime("%Y-%m-%d %I:%M %p")

        # Regex time: '15:30', '3:30 pm', '18:00'
        m_time = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?$", s, re.I)
        if m_time:
            hr, mn = int(m_time.group(1)), int(m_time.group(2))
            ampm = m_time.group(4)
            if ampm:
                if ampm.lower() == "pm" and hr < 12:
                    hr += 12
                elif ampm.lower() == "am" and hr == 12:
                    hr = 0
            cand = now_local.replace(hour=hr, minute=mn, second=0, microsecond=0)
            if cand <= now_local:
                cand += timedelta(days=1)
            target_utc = cand.astimezone(timezone.utc)
            return target_utc, cand.strftime("%Y-%m-%d %I:%M %p")

        # ISO format
        clean_s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(clean_s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_LOCAL_TZ)
            target_utc = dt.astimezone(timezone.utc)
            return target_utc, dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            pass

    return None, ""


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
                "(e.g. 15, 60, '1 hour') or 'due_datetime' (ISO string or 'HH:MM'); "
                "'list_reminders' to see pending or completed reminders; "
                "'cancel_reminder' with 'reminder_id' or message search term to cancel."
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
                        "description": "Minutes from now when reminder should trigger (e.g. 30, 60, '1 hour').",
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
        # Defensive unwrapping of nested dicts/arguments
        for wrapper in ("parameters", "params", "args", "kwargs", "action_input", "input", "data"):
            sub = params.get(wrapper)
            if isinstance(sub, dict):
                params = {**sub, **params}
            elif isinstance(sub, str):
                sub_str = sub.strip()
                if sub_str.startswith("{") and sub_str.endswith("}"):
                    try:
                        parsed_sub = json.loads(sub_str)
                        if isinstance(parsed_sub, dict):
                            params = {**parsed_sub, **params}
                    except Exception:
                        pass

        action = (
            params.get("action")
            or params.get("operation")
            or params.get("command")
            or params.get("method")
            or params.get("type")
            or ""
        )
        if not action:
            if any(params.get(k) for k in (
                "message", "text", "reminder", "task", "title", "note", "content",
                "delay_minutes", "delay", "minutes", "hours", "time", "due", "due_datetime", "when"
            )):
                action = "create_reminder"
            elif any(params.get(k) for k in ("reminder_id", "id")):
                action = "cancel_reminder"
            else:
                action = "list_reminders"

        action_map = {
            "create": "create_reminder",
            "create_reminder": "create_reminder",
            "add": "create_reminder",
            "add_reminder": "create_reminder",
            "new": "create_reminder",
            "remind": "create_reminder",
            "reminder": "create_reminder",
            "set": "create_reminder",
            "set_reminder": "create_reminder",
            "recordar": "create_reminder",
            "recordatorio": "create_reminder",
            "programar": "create_reminder",
            "agendar": "create_reminder",
            "schedule": "create_reminder",
            "schedule_reminder": "create_reminder",
            "notify": "create_reminder",
            "alerta": "create_reminder",
            "list": "list_reminders",
            "list_reminders": "list_reminders",
            "get": "list_reminders",
            "get_reminders": "list_reminders",
            "view": "list_reminders",
            "show": "list_reminders",
            "listar": "list_reminders",
            "ver": "list_reminders",
            "reminders": "list_reminders",
            "delete": "cancel_reminder",
            "delete_reminder": "cancel_reminder",
            "remove": "cancel_reminder",
            "cancel": "cancel_reminder",
            "cancel_reminder": "cancel_reminder",
            "borrar": "cancel_reminder",
            "eliminar": "cancel_reminder",
        }
        action = action_map.get(str(action).lower().strip().replace(" ", "_").replace("-", "_"), action)

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
        message = str(
            params.get("message")
            or params.get("reminder")
            or params.get("task")
            or params.get("text")
            or params.get("title")
            or params.get("content")
            or params.get("note")
            or ""
        ).strip()
        if not message:
            return ToolResult(tool_name=_TOOL_NAME, content="'message' is required for create_reminder.", success=False)

        target_utc, human_time_str = _parse_reminder_target(params)

        if target_utc is None:
            return ToolResult(
                tool_name=_TOOL_NAME,
                content="Debes especificar 'delay_minutes' (ej. 15, '1 hour') o 'due_datetime' válido (ej. '17:30' o '2026-11-22T09:00:00').",
                success=False,
            )

        rid = uuid.uuid4().hex[:8]
        entry = {
            "id": rid,
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat(),
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
        rid = str(params.get("reminder_id") or params.get("id") or "").strip()
        search_term = str(params.get("message") or params.get("title") or params.get("task") or "").strip().lower()
        if not rid and not search_term:
            return ToolResult(tool_name=_TOOL_NAME, content="'reminder_id' o término de búsqueda requerido.", success=False)

        with _reminders_lock:
            items = _load_reminders()
            cancelled = []
            for r in items:
                if r.get("status") == "pending":
                    if rid and r.get("id") == rid:
                        r["status"] = "cancelled"
                        cancelled.append(r["id"])
                        break
                    elif search_term and search_term in (r.get("message") or "").lower():
                        r["status"] = "cancelled"
                        cancelled.append(r["id"])

            if cancelled:
                _save_reminders(items)
                return ToolResult(tool_name=_TOOL_NAME, content=f"Recordatorio(s) cancelado(s) exitosamente: {', '.join(cancelled)}.", success=True)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content="No se encontró ningún recordatorio pendiente que coincida.", success=False)


__all__ = ["RemindersTool"]
