"""Google Tasks tool for listing and managing tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

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
_PERMISSION_HINT = (
    "❌ Permiso denegado por Google: El token actual de Google Tasks tiene permiso de solo lectura ('tasks.readonly'). "
    "Para crear, completar o borrar tareas en Google Tasks se debe re-autorizar la cuenta con el scope de escritura ('tasks'). "
    "Ejecuta en la terminal: `jarvis connect google --force` para actualizar los permisos en tu navegador."
)


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
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

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
        title = params.get("title", "").strip()
        if not title:
            return ToolResult(tool_name=_TOOL_NAME, content="'title' is required for create_task.", success=False)

        body: Dict[str, Any] = {"title": title}
        if params.get("notes"):
            body["notes"] = params["notes"]
        if params.get("due"):
            body["due"] = params["due"]

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
        task_id = (params.get("task_id") or params.get("id") or params.get("task") or "").strip()
        title = (params.get("title") or params.get("name") or "").strip()
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
        task_id = (params.get("task_id") or params.get("id") or params.get("task") or "").strip()
        title = (params.get("title") or params.get("name") or "").strip()
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

        resolved_id, resolved_title, err = self._find_task(task_id, title, tasklist)
        if err or not resolved_id:
            return ToolResult(tool_name=_TOOL_NAME, content=err or "Tarea no encontrada.", success=False)

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
