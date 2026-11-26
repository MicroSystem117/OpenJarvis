"""Google Tasks tool for listing and managing tasks."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from openjarvis.connectors.google_auth import call_with_refresh
from openjarvis.connectors.oauth import resolve_google_credentials
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "google_tasks"
_DEFAULT_CREDENTIALS_PATH = str(get_config_dir() / "connectors" / "google_tasks.json")
_TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"
_LOCAL_TZ = ZoneInfo("America/Caracas")
_PERMISSION_HINT = (
    "❌ Permiso denegado por Google: El token actual de Google Tasks tiene permiso de solo lectura ('tasks.readonly'). "
    "Para crear, completar o borrar tareas en Google Tasks se debe re-autorizar la cuenta con el scope de escritura ('tasks'). "
    "Ejecuta en la terminal: `jarvis connect google --force` para actualizar los permisos en tu navegador."
)


def _parse_due_to_rfc3339(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse relative time or date string into RFC 3339 format for Google Tasks.

    Returns (rfc3339_str, fallback_note).
    If parsed successfully -> (formatted_str, None).
    If unrecognized -> (None, original_str) so it can safely be appended to notes.
    """
    if not raw or not isinstance(raw, str):
        return None, None
    s = raw.strip()
    if not s:
        return None, None

    now_local = datetime.now(_LOCAL_TZ)

    # 1. Regex for hours: 'in 1 hour', '1 hour', 'dentro de 2 horas', 'en 1 hora', '1h', '2 hrs'
    m_hour = re.search(
        r"(?:(?:en|dentro\s+de|in)\s+)?(\d+(?:\.\d+)?)\s*(?:hours?|horas?|hrs?|h)\b",
        s,
        re.I,
    )
    if m_hour:
        hrs = float(m_hour.group(1))
        dt = (now_local + timedelta(hours=hrs)).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 2. Regex for minutes: 'in 30 mins', '30 minutes', 'en 15 minutos', '10m', '45 min'
    m_min = re.search(
        r"(?:(?:en|dentro\s+de|in)\s+)?(\d+(?:\.\d+)?)\s*(?:minutes?|minutos?|mins?|m)\b",
        s,
        re.I,
    )
    if m_min:
        mins = float(m_min.group(1))
        dt = (now_local + timedelta(minutes=mins)).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 3. Regex for days: 'in 2 days', '2 dias', 'en 3 días'
    m_day = re.search(
        r"(?:(?:en|dentro\s+de|in)\s+)?(\d+)\s*(?:days?|dias?|días?|d)\b",
        s,
        re.I,
    )
    if m_day:
        days = int(m_day.group(1))
        dt = (
            (now_local + timedelta(days=days))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 4. Words: 'tomorrow', 'mañana'
    if re.search(r"\b(?:tomorrow|mañana)\b", s, re.I):
        dt = (
            (now_local + timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 5. Words: 'today', 'hoy'
    if re.search(r"\b(?:today|hoy)\b", s, re.I):
        dt = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 6. Time only: '15:30', '18:00', '3:30 pm'
    m_time = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?$", s, re.I)
    if m_time:
        hr = int(m_time.group(1))
        mn = int(m_time.group(2))
        ampm = m_time.group(4)
        if ampm:
            if ampm.lower() == "pm" and hr < 12:
                hr += 12
            elif ampm.lower() == "am" and hr == 12:
                hr = 0
        dt_cand = now_local.replace(hour=hr, minute=mn, second=0, microsecond=0)
        if dt_cand <= now_local:
            dt_cand += timedelta(days=1)
        return dt_cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), None

    # 7. ISO date / datetime: e.g. '2026-11-26', '2026-11-26 15:00:00', '2026-11-26T15:00:00Z'
    clean_s = s.replace("Z", "+00:00")
    if len(s) == 10 and s.count("-") == 2:
        return f"{s}T00:00:00.000Z", None
    try:
        dt = datetime.fromisoformat(clean_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_LOCAL_TZ)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), None
    except Exception:
        pass

    # Fallback: cannot parse safely into RFC 3339 -> return (None, s) so it won't crash Google Tasks API
    return None, s


@ToolRegistry.register(_TOOL_NAME)
class GoogleTasksTool(BaseTool):
    """Tool to list and manage Google Tasks."""

    tool_id = _TOOL_NAME
    is_local = False

    def __init__(self, credentials_path: str = "") -> None:
        self._credentials_path = resolve_google_credentials(
            credentials_path or _DEFAULT_CREDENTIALS_PATH
        )

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "List and manage Google Tasks. Actions: 'list_tasks' to fetch pending "
                "or completed tasks; 'list_tasklists' to see all task lists; "
                "'create_task' to add a new task; "
                "'complete_task' to mark a task as completed (by 'task_id' or 'title'); "
                "'delete_task' to delete a task (by 'task_id' or 'title'); and "
                "'clear_completed' to clear all completed tasks from a list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_tasks",
                            "list_tasklists",
                            "create_task",
                            "complete_task",
                            "delete_task",
                            "clear_completed",
                        ],
                        "description": "Action to perform on Google Tasks.",
                    },
                    "tasklist": {
                        "type": "string",
                        "description": "Task list ID (defaults to '@default').",
                        "default": "@default",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to complete or delete.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title of the task (required for 'create_task'; or search term for 'complete_task'/'delete_task'). Use 'all' or 'todas' to complete/delete all tasks in the list.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes or description for 'create_task'.",
                    },
                    "due": {
                        "type": "string",
                        "description": "RFC3339 due date/time (e.g. '2026-11-22T00:00:00Z').",
                    },
                    "show_completed": {
                        "type": "boolean",
                        "description": "Whether to include completed tasks (default false).",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
            category="productivity",
            requires_confirmation=False,
            timeout_seconds=30.0,
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
            or params.get("mode")
            or ""
        )

        # Smart action inference if missing
        if not action:
            if any(params.get(k) for k in ("title", "name", "task", "task_name", "task_title", "notes", "description", "content", "summary")):
                action = "create_task"
            elif any(params.get(k) for k in ("task_id", "id")):
                if params.get("delete") or params.get("remove") or params.get("borrar") or params.get("eliminar"):
                    action = "delete_task"
                else:
                    action = "complete_task"
            else:
                action = "list_tasks"

        action_map = {
            "list": "list_tasks",
            "list_tasks": "list_tasks",
            "listar": "list_tasks",
            "ver": "list_tasks",
            "tasks": "list_tasks",
            "get_tasks": "list_tasks",
            "list_lists": "list_tasklists",
            "list_tasklists": "list_tasklists",
            "tasklists": "list_tasklists",
            "create": "create_task",
            "create_task": "create_task",
            "add": "create_task",
            "add_task": "create_task",
            "new": "create_task",
            "new_task": "create_task",
            "crear": "create_task",
            "anotar": "create_task",
            "guardar": "create_task",
            "save": "create_task",
            "insert": "create_task",
            "complete": "complete_task",
            "complete_task": "complete_task",
            "done": "complete_task",
            "completar": "complete_task",
            "finalizar": "complete_task",
            "delete": "delete_task",
            "delete_task": "delete_task",
            "remove": "delete_task",
            "borrar": "delete_task",
            "eliminar": "delete_task",
            "clear": "clear_completed",
            "clear_completed": "clear_completed",
            "limpiar": "clear_completed",
        }
        action = action_map.get(str(action).lower().strip().replace(" ", "_").replace("-", "_"), action)

        tasklist = params.get("tasklist") or "@default"

        try:
            if action == "list_tasks":
                return self._list_tasks(params, tasklist)
            elif action == "list_tasklists":
                return self._list_tasklists()
            elif action == "create_task":
                return self._create_task(params, tasklist)
            elif action == "complete_task":
                return self._complete_task(params, tasklist)
            elif action == "delete_task":
                return self._delete_task(params, tasklist)
            elif action == "clear_completed":
                return self._clear_completed(tasklist)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Google Tasks tool execution error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Google Tasks error: {exc}", success=False)

    def _fetch_all_tasks(self, tasklist: str) -> Tuple[int, List[Dict[str, Any]], str]:
        """Fetch all tasks for resolution. Returns (status, items, error_message)."""
        def _get(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks"
            p = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_get, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return status, [], err
        return status, data.get("items", []), ""

    def _find_task(self, task_id: str, title: str, tasklist: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Find task id and title by ID or by searching title. Returns (task_id, task_title, error_message)."""
        if task_id:
            return task_id, title or task_id, None

        if not title:
            return None, None, "Se requiere 'task_id' o 'title' para identificar la tarea."

        status, items, err = self._fetch_all_tasks(tasklist)
        if status != 200:
            return None, None, f"Error buscando tareas ({status}): {err}"

        norm_target = title.strip().lower()

        # 1. Exact match
        for it in items:
            t = (it.get("title") or "").strip().lower()
            if t == norm_target:
                return it.get("id"), it.get("title"), None

        # 2. Substring match
        matches = [it for it in items if norm_target in (it.get("title") or "").lower()]
        if len(matches) == 1:
            return matches[0].get("id"), matches[0].get("title"), None
        elif len(matches) > 1:
            match_titles = ", ".join(f"'{m.get('title')}' (ID: {m.get('id')})" for m in matches[:5])
            return None, None, f"Se encontraron múltiples tareas que coinciden con '{title}': {match_titles}. Especifica el 'task_id'."

        return None, None, f"No se encontró ninguna tarea con el título '{title}' en la lista."

    def _list_tasks(self, params: Dict[str, Any], tasklist: str) -> ToolResult:
        show_comp = bool(params.get("show_completed", False))

        def _get(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks"
            p = {"showCompleted": str(show_comp).lower(), "maxResults": 50}
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_get, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error listing tasks ({status}): {err}", success=False)

        items = data.get("items", [])
        if not items:
            return ToolResult(tool_name=_TOOL_NAME, content="No hay tareas pendientes en esta lista.", success=True)

        lines = []
        for it in items:
            title = it.get("title", "(Sin título)")
            due = it.get("due", "Sin fecha límite")
            tid = it.get("id", "")
            status_task = it.get("status", "needsAction")
            lines.append(f"• [{status_task}] {title} (Vence: {due}) [ID: {tid}]")

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Tareas encontradas ({len(items)}):\n\n" + "\n".join(lines),
            success=True,
            metadata={"count": len(items)},
        )

    def _list_tasklists(self) -> ToolResult:
        def _get_lists(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/users/@me/lists"
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_get_lists, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error listing task lists ({status}): {err}", success=False)

        items = data.get("items", [])
        lines = [f"• {it.get('title')} (ID: {it.get('id')})" for it in items]
        return ToolResult(tool_name=_TOOL_NAME, content="Listas de tareas:\n" + "\n".join(lines), success=True)

    def _create_task(self, params: Dict[str, Any], tasklist: str) -> ToolResult:
        title = str(
            params.get("title")
            or params.get("task")
            or params.get("task_title")
            or params.get("name")
            or params.get("content")
            or params.get("text")
            or params.get("summary")
            or ""
        ).strip()
        notes = str(
            params.get("notes")
            or params.get("description")
            or params.get("details")
            or params.get("body")
            or ""
        ).strip()

        if not title and notes:
            title = notes
            notes = ""

        if not title:
            return ToolResult(tool_name=_TOOL_NAME, content="'title' is required for create_task.", success=False)

        body: Dict[str, Any] = {"title": title}

        due_raw = str(
            params.get("due")
            or params.get("due_date")
            or params.get("date")
            or params.get("time")
            or params.get("when")
            or params.get("delay")
            or ""
        ).strip()
        if due_raw:
            rfc_due, fallback_note = _parse_due_to_rfc3339(due_raw)
            if rfc_due:
                body["due"] = rfc_due
            elif fallback_note:
                extra_note = f"Vencimiento solicitado: {fallback_note}"
                notes = f"{notes}\n{extra_note}".strip() if notes else extra_note

        if notes:
            body["notes"] = notes

        def _post(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks"
            r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_post, self._credentials_path)
        if status in (200, 201):
            return ToolResult(tool_name=_TOOL_NAME, content=f"Tarea '{title}' creada exitosamente en Google Tasks.", success=True)
        elif status == 403:
            return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
        else:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error creando tarea ({status}): {err}", success=False)

    def _complete_task(self, params: Dict[str, Any], tasklist: str) -> ToolResult:
        task_id = (params.get("task_id") or params.get("id") or "").strip()
        title = (params.get("title") or params.get("name") or params.get("task") or "").strip()
        if " " in task_id and not title:
            title = task_id
            task_id = ""
        is_all = params.get("all") or title.lower() in ("all", "todas", "todo", "todas las tareas")

        if is_all:
            status, items, err = self._fetch_all_tasks(tasklist)
            if status != 200:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error obteniendo tareas ({status}): {err}", success=False)
            pending = [it for it in items if it.get("status") != "completed"]
            if not pending:
                return ToolResult(tool_name=_TOOL_NAME, content="No hay tareas pendientes para marcar como completadas.", success=True)

            completed_count = 0
            for it in pending:
                tid = it["id"]
                def _patch(token: str) -> tuple[int, Dict[str, Any]]:
                    url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{tid}"
                    r = httpx.patch(url, headers={"Authorization": f"Bearer {token}"}, json={"status": "completed"}, timeout=20.0)
                    try:
                        return r.status_code, r.json()
                    except Exception:
                        return r.status_code, {}

                st, dt = call_with_refresh(_patch, self._credentials_path)
                if st == 403:
                    return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
                if st in (200, 204):
                    completed_count += 1

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"✅ Se marcaron {completed_count} tareas como completadas en Google Tasks.",
                success=True,
            )

        resolved_id, resolved_title, err = self._find_task(task_id, title, tasklist)
        if err or not resolved_id:
            return ToolResult(tool_name=_TOOL_NAME, content=err or "Tarea no encontrada.", success=False)

        def _patch_single(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{resolved_id}"
            r = httpx.patch(url, headers={"Authorization": f"Bearer {token}"}, json={"status": "completed"}, timeout=20.0)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}

        status, data = call_with_refresh(_patch_single, self._credentials_path)
        if status in (200, 204):
            return ToolResult(tool_name=_TOOL_NAME, content=f"✅ Tarea '{resolved_title}' marcada como completada en Google Tasks.", success=True)
        elif status == 403:
            return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
        else:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error completando tarea ({status}): {err}", success=False)

    def _delete_task(self, params: Dict[str, Any], tasklist: str) -> ToolResult:
        task_id = (params.get("task_id") or params.get("id") or "").strip()
        title = (params.get("title") or params.get("name") or params.get("task") or "").strip()
        if " " in task_id and not title:
            title = task_id
            task_id = ""
        is_all = params.get("all") or title.lower() in ("all", "todas", "todo", "todas las tareas")

        if is_all:
            status, items, err = self._fetch_all_tasks(tasklist)
            if status != 200:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error obteniendo tareas ({status}): {err}", success=False)
            if not items:
                return ToolResult(tool_name=_TOOL_NAME, content="No hay tareas para eliminar en esta lista.", success=True)

            deleted_count = 0
            for it in items:
                tid = it["id"]
                def _del(token: str) -> tuple[int, Dict[str, Any]]:
                    url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{tid}"
                    r = httpx.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
                    try:
                        return r.status_code, r.json()
                    except Exception:
                        return r.status_code, {}

                st, dt = call_with_refresh(_del, self._credentials_path)
                if st == 403:
                    return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
                if st in (200, 204):
                    deleted_count += 1

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"✅ Se eliminaron {deleted_count} tareas de Google Tasks.",
                success=True,
            )

        # If title is provided without a specific task_id, look for all matching tasks
        if not task_id and title:
            status, items, err = self._fetch_all_tasks(tasklist)
            if status != 200:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error obteniendo tareas ({status}): {err}", success=False)
            norm_target = title.strip().lower()
            matches = [it for it in items if norm_target == (it.get("title") or "").strip().lower()]
            if not matches:
                matches = [it for it in items if norm_target in (it.get("title") or "").lower()]

            if len(matches) > 1:
                deleted_names = []
                for m in matches:
                    tid = m["id"]
                    def _patch_done_m(token: str, target_id: str = tid) -> None:
                        try:
                            httpx.patch(f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{target_id}",
                                        headers={"Authorization": f"Bearer {token}"},
                                        json={"status": "completed"}, timeout=10.0)
                        except Exception:
                            pass
                    call_with_refresh(_patch_done_m, self._credentials_path)

                    def _del_match(token: str, target_id: str = tid) -> tuple[int, Dict[str, Any]]:
                        url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{target_id}"
                        r = httpx.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
                        try:
                            return r.status_code, r.json()
                        except Exception:
                            return r.status_code, {}

                    st, _ = call_with_refresh(_del_match, self._credentials_path)
                    if st in (200, 204):
                        deleted_names.append(m.get("title") or tid)
                return ToolResult(
                    tool_name=_TOOL_NAME,
                    content=f"✅ Se eliminaron {len(deleted_names)} tareas coincidentes con '{title}': {', '.join(deleted_names)}.",
                    success=True,
                )

        resolved_id, resolved_title, err = self._find_task(task_id, title, tasklist)
        if err or not resolved_id:
            return ToolResult(tool_name=_TOOL_NAME, content=err or "Tarea no encontrada.", success=False)

        # Mark as completed first to suppress recurring instances from re-spawning
        def _patch_done(token: str) -> None:
            try:
                httpx.patch(f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{resolved_id}",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"status": "completed"}, timeout=10.0)
            except Exception:
                pass
        call_with_refresh(_patch_done, self._credentials_path)

        def _del_single(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks/{resolved_id}"
            r = httpx.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}

        status, data = call_with_refresh(_del_single, self._credentials_path)
        if status in (200, 204):
            return ToolResult(tool_name=_TOOL_NAME, content=f"✅ Tarea '{resolved_title}' eliminada exitosamente de Google Tasks.", success=True)
        elif status == 403:
            return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
        else:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error eliminando tarea ({status}): {err}", success=False)

    def _clear_completed(self, tasklist: str) -> ToolResult:
        def _clear(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/clear"
            r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}

        status, data = call_with_refresh(_clear, self._credentials_path)
        if status in (200, 204):
            return ToolResult(tool_name=_TOOL_NAME, content="✅ Tareas completadas limpiadas de la lista.", success=True)
        elif status == 403:
            return ToolResult(tool_name=_TOOL_NAME, content=_PERMISSION_HINT, success=False)
        else:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error limpiando tareas ({status}): {err}", success=False)


__all__ = ["GoogleTasksTool"]
