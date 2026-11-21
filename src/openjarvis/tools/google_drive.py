"""Google Drive tool for searching, inspecting, and reading Drive files."""

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

_TOOL_NAME = "google_drive"
_DEFAULT_CREDENTIALS_PATH = str(get_config_dir() / "connectors" / "gdrive.json")
_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


@ToolRegistry.register(_TOOL_NAME)
class GoogleDriveTool(BaseTool):
    """Tool to search and inspect files in Google Drive."""

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
                "Search, inspect, and read files in Google Drive. Actions: "
                "'search_files' to query files by name or keyword; "
                "'list_recent' to view recently modified documents; "
                "'get_file_info' to get file metadata and link; "
                "'read_file_content' to read plain text/docs content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search_files", "list_recent", "get_file_info", "read_file_content"],
                        "description": "Action to perform on Google Drive.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Keywords or filename part to search for.",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "Google Drive file ID for get_file_info or read_file_content.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of files to return (default 10).",
                        "default": 10,
                    },
                },
                "required": ["action"],
            },
            category="productivity",
            requires_confirmation=False,
            timeout_seconds=35.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

        try:
            if action == "search_files":
                return self._search_files(params)
            elif action == "list_recent":
                return self._list_recent(params)
            elif action == "get_file_info":
                return self._get_file_info(params)
            elif action == "read_file_content":
                return self._read_file_content(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Google Drive tool error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Google Drive error: {exc}", success=False)

    def _search_files(self, params: Dict[str, Any]) -> ToolResult:
        query = params.get("query", "").strip()
        max_results = min(int(params.get("max_results", 10)), 30)

        # Build Drive v3 q string
        q_parts = ["trashed = false"]
        if query:
            clean_q = query.replace("'", "\\'")
            q_parts.append(f"(name contains '{clean_q}' or fullText contains '{clean_q}')")

        def _do_search(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_DRIVE_API_BASE}/files"
            p = {
                "q": " and ".join(q_parts),
                "pageSize": max_results,
                "fields": "files(id, name, mimeType, modifiedTime, size, webViewLink)",
                "orderBy": "modifiedTime desc",
            }
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=25.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_search, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error searching Drive ({status}): {err}", success=False)

        files = data.get("files", [])
        if not files:
            return ToolResult(tool_name=_TOOL_NAME, content=f"No se encontraron archivos en Drive para: '{query}'", success=True)

        lines = []
        for f in files:
            name = f.get("name", "Sin nombre")
            fid = f.get("id", "")
            mime = f.get("mimeType", "")
            mod = f.get("modifiedTime", "")
            link = f.get("webViewLink", "")
            lines.append(f"• **{name}**\n  ID: `{fid}`\n  Tipo: {mime}\n  Modificado: {mod}\n  Enlace: {link}")

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Archivos encontrados en Google Drive ({len(files)}):\n\n" + "\n\n".join(lines),
            success=True,
            metadata={"count": len(files)},
        )

    def _list_recent(self, params: Dict[str, Any]) -> ToolResult:
        max_results = min(int(params.get("max_results", 10)), 30)

        def _do_list(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_DRIVE_API_BASE}/files"
            p = {
                "q": "trashed = false",
                "pageSize": max_results,
                "fields": "files(id, name, mimeType, modifiedTime, size, webViewLink)",
                "orderBy": "modifiedTime desc",
            }
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=25.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_list, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error listing files ({status}): {err}", success=False)

        files = data.get("files", [])
        if not files:
            return ToolResult(tool_name=_TOOL_NAME, content="No hay archivos recientes en Google Drive.", success=True)

        lines = []
        for f in files:
            name = f.get("name", "Sin nombre")
            fid = f.get("id", "")
            mod = f.get("modifiedTime", "")
            lines.append(f"• **{name}** [ID: `{fid}`] (Modificado: {mod})")

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Archivos recientes en Drive ({len(files)}):\n\n" + "\n".join(lines),
            success=True,
            metadata={"count": len(files)},
        )

    def _get_file_info(self, params: Dict[str, Any]) -> ToolResult:
        file_id = params.get("file_id", "").strip()
        if not file_id:
            return ToolResult(tool_name=_TOOL_NAME, content="'file_id' is required.", success=False)

        def _do_get(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_DRIVE_API_BASE}/files/{file_id}"
            p = {"fields": "id, name, mimeType, modifiedTime, size, webViewLink, owners, description"}
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_do_get, self._credentials_path)
        if status != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error getting file info ({status}): {err}", success=False)

        name = data.get("name")
        mime = data.get("mimeType")
        size = data.get("size", "N/A")
        mod = data.get("modifiedTime")
        link = data.get("webViewLink")
        owners = [o.get("displayName", "") for o in data.get("owners", [])]

        info = (
            f"📄 **Detalles del archivo:**\n"
            f"• Nombre: {name}\n"
            f"• ID: `{file_id}`\n"
            f"• Tipo MIME: {mime}\n"
            f"• Tamaño: {size} bytes\n"
            f"• Última modificación: {mod}\n"
            f"• Propietario(s): {', '.join(owners)}\n"
            f"• Enlace directo: {link}"
        )
        return ToolResult(tool_name=_TOOL_NAME, content=info, success=True, metadata=data)

    def _read_file_content(self, params: Dict[str, Any]) -> ToolResult:
        file_id = params.get("file_id", "").strip()
        if not file_id:
            return ToolResult(tool_name=_TOOL_NAME, content="'file_id' is required.", success=False)

        # First get metadata to know mimeType
        def _get_meta(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_DRIVE_API_BASE}/files/{file_id}"
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params={"fields": "id, name, mimeType"}, timeout=20.0)
            return r.status_code, r.json()

        st, meta = call_with_refresh(_get_meta, self._credentials_path)
        if st != 200:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Could not retrieve file metadata ({st}).", success=False)

        mime = meta.get("mimeType", "")
        name = meta.get("name", "Documento")

        # If it's a Google Doc, export to plain text
        if mime == "application/vnd.google-apps.document":
            def _export_doc(token: str) -> tuple[int, str]:
                url = f"{_DRIVE_API_BASE}/files/{file_id}/export"
                r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params={"mimeType": "text/plain"}, timeout=30.0)
                return r.status_code, r.text

            status, text = call_with_refresh(_export_doc, self._credentials_path)
            if status != 200:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error exporting Google Doc: {text[:200]}", success=False)
            preview = text[:8000] + ("\n... [Contenido truncado por longitud]" if len(text) > 8000 else "")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Contenido de '{name}':\n\n{preview}", success=True)

        # If text/plain, json, csv, etc.
        if mime.startswith("text/") or mime in ("application/json", "application/javascript", "text/csv"):
            def _download_media(token: str) -> tuple[int, str]:
                url = f"{_DRIVE_API_BASE}/files/{file_id}"
                r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params={"alt": "media"}, timeout=30.0)
                return r.status_code, r.text

            status, text = call_with_refresh(_download_media, self._credentials_path)
            if status != 200:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error downloading text file: {text[:200]}", success=False)
            preview = text[:8000] + ("\n... [Contenido truncado]" if len(text) > 8000 else "")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Contenido de '{name}':\n\n{preview}", success=True)

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=(
                f"El archivo '{name}' es de tipo '{mime}' (formato binario o suite). "
                f"Para abrirlo o editarlo, usa el enlace web oficial en Google Drive."
            ),
            success=True,
        )


__all__ = ["GoogleDriveTool"]
