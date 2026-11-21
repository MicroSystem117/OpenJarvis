"""Google Tasks tool for listing and managing tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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
                "List and view Google Tasks. Actions: 'list_tasks' to fetch pending "
                "or completed tasks; 'list_tasklists' to see all task lists; and "
                "'create_task' to add a new task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_tasks", "list_tasklists", "create_task"],
                        "description": "Action to perform on Google Tasks.",
                    },
                    "tasklist": {
                        "type": "string",
                        "description": "Task list ID (defaults to '@default').",
                        "default": "@default",
                    },
                    "show_completed": {
                        "type": "boolean",
                        "description": "Whether to include completed tasks (default false).",
                        "default": False,
                    },
                    "title": {
                        "type": "string",
                        "description": "Title of the task (required for 'create_task').",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes or description for 'create_task'.",
                    },
                    "due": {
                        "type": "string",
                        "description": "RFC3339 due date/time (e.g. '2026-11-22T00:00:00Z').",
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
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Google Tasks tool execution error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Google Tasks error: {exc}", success=False)

    def _list_tasks(self, params: Dict[str, Any], tasklist: str) -> ToolResult:
        show_comp = bool(params.get("show_completed", False))

        def _get(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_TASKS_API_BASE}/lists/{tasklist}/tasks"
            p = {"showCompleted": str(show_comp).lower(), "maxResults": 30}
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
        else:
            if status == 403:
                return ToolResult(
                    tool_name=_TOOL_NAME,
                    content=(
                        f"No fue posible crear la tarea en Google Tasks: El token OAuth actual solo tiene permisos de lectura ('tasks.readonly'). "
                        f"Para crear tareas directamente en Google Tasks se requiere autorizar el scope de escritura, o puedes agendarla como un recordatorio/evento en Google Calendar."
                    ),
                    success=False,
                )
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error creando tarea ({status}): {err}", success=False)


__all__ = ["GoogleTasksTool"]
