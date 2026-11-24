"""Google Calendar tool for creating, listing, and managing calendar events."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx
from openjarvis.connectors.google_auth import call_with_refresh
from openjarvis.connectors.oauth import resolve_google_credentials
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "google_calendar"
_DEFAULT_CREDENTIALS_PATH = str(get_config_dir() / "connectors" / "gcalendar.json")
_GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"
_DEFAULT_TIMEZONE = "America/Caracas"


@ToolRegistry.register(_TOOL_NAME)
class GoogleCalendarTool(BaseTool):
    """Tool to create, list, and delete Google Calendar events."""

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
                "Manage Google Calendar events. Use 'create_event' to schedule "
                "new events, meetings, or reminders; 'list_events' to see upcoming events; "
                "and 'delete_event' to remove an event by ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create_event", "list_events", "delete_event"],
                        "description": "Action to perform on Google Calendar.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Title or summary of the event (required for create_event).",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Start date/time in ISO format, e.g. '2026-11-22T09:00:00' or "
                            "'2026-11-22T09:00:00-04:00'. Required for create_event."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "End date/time in ISO format. If omitted for create_event, "
                            "defaults to 1 hour after start_time."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional notes or description for the event.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional location for the event.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID. Defaults to 'primary'.",
                        "default": "primary",
                    },
                    "time_min": {
                        "type": "string",
                        "description": "RFC3339 lower bound for list_events (e.g. '2026-11-22T00:00:00Z').",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "RFC3339 upper bound for list_events.",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "Event ID required for delete_event.",
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
            if any(params.get(k) for k in ("summary", "title", "start_time", "start", "start_date", "end_time", "end", "event", "name", "date", "datetime")):
                action = "create_event"
            elif any(params.get(k) for k in ("event_id", "id")) and not (params.get("summary") or params.get("title")):
                action = "delete_event"
            else:
                action = "list_events"

        action_map = {
            "create": "create_event",
            "create_event": "create_event",
            "crear": "create_event",
            "add": "create_event",
            "add_event": "create_event",
            "new": "create_event",
            "agendar": "create_event",
            "agenda": "create_event",
            "programar": "create_event",
            "schedule": "create_event",
            "evento": "create_event",
            "insert": "create_event",
            "list": "list_events",
            "list_events": "list_events",
            "get": "list_events",
            "view": "list_events",
            "show": "list_events",
            "listar": "list_events",
            "ver": "list_events",
            "consultar": "list_events",
            "delete": "delete_event",
            "delete_event": "delete_event",
            "remove": "delete_event",
            "cancel": "delete_event",
            "cancelar": "delete_event",
            "borrar": "delete_event",
            "eliminar": "delete_event",
        }
        action = action_map.get(str(action).lower().strip().replace(" ", "_").replace("-", "_"), action)

        calendar_id = params.get("calendar_id") or "primary"

        try:
            if action == "create_event":
                return self._create_event(params, calendar_id)
            elif action == "list_events":
                return self._list_events(params, calendar_id)
            elif action == "delete_event":
                return self._delete_event(params, calendar_id)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Google Calendar tool execution error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Google Calendar error: {exc}", success=False)

    def _create_event(self, params: Dict[str, Any], calendar_id: str) -> ToolResult:
        summary = (
            params.get("summary")
            or params.get("title")
            or params.get("event")
            or params.get("name")
            or ""
        ).strip()
        if not summary:
            return ToolResult(tool_name=_TOOL_NAME, content="'summary' is required to create an event.", success=False)

        start_raw = str(
            params.get("start_time")
            or params.get("start")
            or params.get("date")
            or params.get("datetime")
            or params.get("start_date")
            or ""
        ).strip()
        if not start_raw:
            return ToolResult(tool_name=_TOOL_NAME, content="'start_time' is required to create an event.", success=False)

        # Normalize start_raw
        if len(start_raw) == 10 and start_raw.count("-") == 2:
            start_raw = f"{start_raw}T09:00:00"
        elif " " in start_raw and "T" not in start_raw:
            start_raw = start_raw.replace(" ", "T")

        # Parse start and end times
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except ValueError:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Invalid start_time format: '{start_raw}'. Please use ISO format like '2026-11-25T15:00:00'.", success=False)

        end_raw = str(params.get("end_time") or params.get("end") or "").strip()
        if end_raw:
            if len(end_raw) == 10 and end_raw.count("-") == 2:
                end_raw = f"{end_raw}T10:00:00"
            elif " " in end_raw and "T" not in end_raw:
                end_raw = end_raw.replace(" ", "T")
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except ValueError:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Invalid end_time format: '{end_raw}'.", success=False)
        else:
            end_dt = start_dt + timedelta(hours=1)

        # Ensure ISO string format
        start_payload: Dict[str, str] = {"timeZone": _DEFAULT_TIMEZONE}
        end_payload: Dict[str, str] = {"timeZone": _DEFAULT_TIMEZONE}

        if start_dt.tzinfo is not None:
            start_payload["dateTime"] = start_dt.isoformat()
            end_payload["dateTime"] = end_dt.isoformat()
        else:
            # Append local timezone if naive
            start_payload["dateTime"] = f"{start_dt.isoformat()}-04:00"
            end_payload["dateTime"] = f"{end_dt.isoformat()}-04:00"

        event_body: Dict[str, Any] = {
            "summary": summary,
            "start": start_payload,
            "end": end_payload,
        }
        if params.get("description"):
            event_body["description"] = params["description"]
        if params.get("location"):
            event_body["location"] = params["location"]

        def _insert(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_GCAL_API_BASE}/calendars/{calendar_id}/events"
            r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=event_body, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_insert, self._credentials_path)
        if status in (200, 201):
            event_id = data.get("id", "")
            link = data.get("htmlLink", "")
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=(
                    f"Evento creado exitosamente en Google Calendar:\n"
                    f"- Título: {summary}\n"
                    f"- Inicio: {start_payload['dateTime']}\n"
                    f"- Fin: {end_payload['dateTime']}\n"
                    f"- ID: {event_id}\n"
                    f"- Enlace: {link}"
                ),
                success=True,
                metadata={"event_id": event_id, "link": link},
            )
        else:
            error_msg = data.get("error", {}).get("message", str(data))
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"Error creando evento en Google Calendar ({status}): {error_msg}",
                success=False,
            )

    def _list_events(self, params: Dict[str, Any], calendar_id: str) -> ToolResult:
        max_r = params.get("max_results") or params.get("limit") or params.get("maxResults") or 25
        try:
            max_results_val = min(int(max_r), 100)
        except (ValueError, TypeError):
            max_results_val = 25

        query_params: Dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max_results_val,
        }
        if params.get("time_min"):
            t_min = str(params["time_min"]).strip()
            if len(t_min) == 10 and t_min.count("-") == 2:
                query_params["timeMin"] = f"{t_min}T00:00:00Z"
            elif "T" in t_min and not t_min.endswith("Z") and "+" not in t_min[-6:] and "-" not in t_min[-6:]:
                query_params["timeMin"] = f"{t_min}Z"
            else:
                query_params["timeMin"] = t_min
        else:
            # Default to today at 00:00:00Z
            query_params["timeMin"] = datetime.now().strftime("%Y-%m-%dT00:00:00Z")

        if params.get("time_max"):
            t_max = str(params["time_max"]).strip()
            if len(t_max) == 10 and t_max.count("-") == 2:
                query_params["timeMax"] = f"{t_max}T23:59:59Z"
            elif "T" in t_max and not t_max.endswith("Z") and "+" not in t_max[-6:] and "-" not in t_max[-6:]:
                query_params["timeMax"] = f"{t_max}Z"
            else:
                query_params["timeMax"] = t_max
        elif params.get("days_ahead") is not None:
            days = int(params["days_ahead"])
            query_params["timeMax"] = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59Z")

        def _list(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_GCAL_API_BASE}/calendars/{calendar_id}/events"
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=query_params, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_list, self._credentials_path)
        if status == 200:
            items = data.get("items", [])
            if not items:
                return ToolResult(tool_name=_TOOL_NAME, content="No se encontraron eventos en el período solicitado.", success=True)
            formatted = []
            for it in items:
                start = it.get("start", {}).get("dateTime") or it.get("start", {}).get("date", "")
                summary = it.get("summary", "(Sin título)")
                eid = it.get("id", "")
                formatted.append(f"• [{start}] {summary} (ID: {eid})")
            return ToolResult(tool_name=_TOOL_NAME, content="\n".join(formatted), success=True)
        else:
            error_msg = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error listando eventos ({status}): {error_msg}", success=False)

    def _delete_event(self, params: Dict[str, Any], calendar_id: str) -> ToolResult:
        event_id = str(params.get("event_id") or params.get("id") or "").strip()
        if not event_id:
            return ToolResult(tool_name=_TOOL_NAME, content="'event_id' is required for delete_event.", success=False)

        def _delete(token: str) -> tuple[int, str]:
            url = f"{_GCAL_API_BASE}/calendars/{calendar_id}/events/{event_id}"
            r = httpx.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
            return r.status_code, r.text

        status, text = call_with_refresh(_delete, self._credentials_path)
        if status in (200, 204):
            return ToolResult(tool_name=_TOOL_NAME, content=f"Evento {event_id} eliminado exitosamente de Google Calendar.", success=True)
        else:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error eliminando evento ({status}): {text}", success=False)


__all__ = ["GoogleCalendarTool"]
