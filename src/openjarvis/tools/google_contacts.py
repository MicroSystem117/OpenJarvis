"""Google Contacts tool for searching and inspecting contacts via People API v1."""

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

_TOOL_NAME = "google_contacts"
_DEFAULT_CREDENTIALS_PATH = str(get_config_dir() / "connectors" / "gcontacts.json")
_PEOPLE_API_BASE = "https://people.googleapis.com/v1"


@ToolRegistry.register(_TOOL_NAME)
class GoogleContactsTool(BaseTool):
    """Tool to search and inspect contacts in Google Contacts."""

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
                "Search and inspect Google Contacts. Actions: "
                "'search_contacts' to find a contact by name, email, or phone; "
                "'list_contacts' to see recent contacts in the address book; "
                "'get_contact' to fetch detailed information for a specific contact."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search_contacts", "list_contacts", "get_contact"],
                        "description": "Action to perform on Google Contacts.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search term (name, email, or phone number) for search_contacts.",
                    },
                    "resource_name": {
                        "type": "string",
                        "description": "Contact resource name (e.g. 'people/c123456789') for get_contact.",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Maximum number of contacts to retrieve (default 20).",
                        "default": 20,
                    },
                },
                "required": ["action"],
            },
            category="communication",
            requires_confirmation=False,
            timeout_seconds=25.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

        action_map = {
            "search": "search_contacts",
            "search_contacts": "search_contacts",
            "find": "search_contacts",
            "buscar": "search_contacts",
            "list": "list_contacts",
            "list_contacts": "list_contacts",
            "listar": "list_contacts",
            "ver": "list_contacts",
            "get": "get_contact",
            "get_contact": "get_contact",
            "info": "get_contact",
            "contact": "get_contact",
        }
        action = action_map.get(str(action).lower().strip(), action)

        try:
            if action == "search_contacts":
                return self._search_contacts(params)
            elif action == "list_contacts":
                return self._list_contacts(params)
            elif action == "get_contact":
                return self._get_contact(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Google Contacts tool error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Google Contacts error: {exc}", success=False)

    def _format_person(self, p: Dict[str, Any]) -> str:
        names = p.get("names", [])
        name = names[0].get("displayName", "Sin nombre") if names else "Sin nombre"
        res_name = p.get("resourceName", "")

        emails = [e.get("value") for e in p.get("emailAddresses", []) if e.get("value")]
        phones = [ph.get("value") for ph in p.get("phoneNumbers", []) if ph.get("value")]
        orgs = [o.get("name") for o in p.get("organizations", []) if o.get("name")]

        lines = [f"• **{name}** [ID: `{res_name}`]"]
        if phones:
            lines.append(f"  📞 Tel: {', '.join(phones)}")
        if emails:
            lines.append(f"  ✉️ Email: {', '.join(emails)}")
        if orgs:
            lines.append(f"  🏢 Organización: {', '.join(orgs)}")

        return "\n".join(lines)

    def _handle_api_error(self, status: int, data: Dict[str, Any]) -> ToolResult:
        err = data.get("error", {})
        msg = err.get("message", str(data))
        if status == 403 and "SERVICE_DISABLED" in str(data):
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=(
                    "La People API (Google Contacts) no está habilitada en tu proyecto de Google Cloud. "
                    "Para activarla con un click, visita: "
                    "https://console.developers.google.com/apis/api/people.googleapis.com/overview?project=867371534674 "
                    "y haz click en 'Habilitar' (Enable)."
                ),
                success=False,
            )
        return ToolResult(tool_name=_TOOL_NAME, content=f"Error en Google Contacts ({status}): {msg}", success=False)

    def _search_contacts(self, params: Dict[str, Any]) -> ToolResult:
        query = params.get("query", "").strip()
        if not query:
            return ToolResult(tool_name=_TOOL_NAME, content="'query' is required for search_contacts.", success=False)

        max_results = min(int(params.get("page_size", 20)), 50)

        def _do_search(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_PEOPLE_API_BASE}/people:searchContacts"
            p = {
                "query": query,
                "readMask": "names,emailAddresses,phoneNumbers,organizations",
                "pageSize": max_results,
            }
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_search, self._credentials_path)
        if status != 200:
            return self._handle_api_error(status, data)

        results = data.get("results", [])
        if not results:
            return ToolResult(tool_name=_TOOL_NAME, content=f"No se encontraron contactos para: '{query}'", success=True)

        formatted = [self._format_person(r.get("person", {})) for r in results]
        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Contactos encontrados ({len(results)}):\n\n" + "\n\n".join(formatted),
            success=True,
            metadata={"count": len(results)},
        )

    def _list_contacts(self, params: Dict[str, Any]) -> ToolResult:
        page_size = min(int(params.get("page_size", 20)), 100)

        def _do_list(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_PEOPLE_API_BASE}/people/me/connections"
            p = {
                "personFields": "names,emailAddresses,phoneNumbers,organizations",
                "pageSize": page_size,
                "sortOrder": "LAST_MODIFIED_DESCENDING",
            }
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=25.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_list, self._credentials_path)
        if status != 200:
            return self._handle_api_error(status, data)

        connections = data.get("connections", [])
        if not connections:
            return ToolResult(tool_name=_TOOL_NAME, content="No tienes contactos en tu libreta de Google.", success=True)

        formatted = [self._format_person(c) for c in connections]
        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Contactos recientes ({len(connections)}):\n\n" + "\n\n".join(formatted),
            success=True,
            metadata={"count": len(connections)},
        )

    def _get_contact(self, params: Dict[str, Any]) -> ToolResult:
        res_name = str(params.get("resource_name") or params.get("id") or params.get("contact_id") or "").strip()
        if not res_name:
            return ToolResult(tool_name=_TOOL_NAME, content="'resource_name' or 'id' is required for get_contact.", success=False)

        if not res_name.startswith("people/"):
            res_name = f"people/{res_name}"

        if not res_name.startswith("people/"):
            res_name = f"people/{res_name}"

        def _do_get(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_PEOPLE_API_BASE}/{res_name}"
            p = {"personFields": "names,emailAddresses,phoneNumbers,organizations,addresses,biographies"}
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_get, self._credentials_path)
        if status != 200:
            return self._handle_api_error(status, data)

        return ToolResult(tool_name=_TOOL_NAME, content=self._format_person(data), success=True, metadata=data)


__all__ = ["GoogleContactsTool"]
