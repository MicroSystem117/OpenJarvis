"""Google Gmail tool for searching, reading, and sending emails."""

from __future__ import annotations

import base64
import email
from email.mime.text import MIMEText
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

_TOOL_NAME = "google_gmail"
_DEFAULT_CREDENTIALS_PATH = str(get_config_dir() / "connectors" / "gmail.json")
_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


@ToolRegistry.register(_TOOL_NAME)
class GoogleGmailTool(BaseTool):
    """Tool to search, read, and send emails via Gmail API."""

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
                "Search, read, and manage Gmail messages. Actions: 'search_emails' to search "
                "by query (e.g. 'is:unread', 'from:someone@domain.com'); 'read_email' to fetch "
                "message content by ID; and 'send_email' to compose and send an email."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search_emails", "read_email", "send_email"],
                        "description": "Action to perform on Gmail.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for 'search_emails' (e.g. 'is:unread', 'from:google.com', 'subject:factura').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of emails to return (default 5, max 20).",
                        "default": 5,
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID required for 'read_email'.",
                    },
                    "to": {
                        "type": "string",
                        "description": "Recipient email address for 'send_email'.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject line for 'send_email'.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body content for 'send_email'.",
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

        action_map = {
            "search": "search_emails",
            "search_emails": "search_emails",
            "find": "search_emails",
            "buscar": "search_emails",
            "list": "search_emails",
            "list_emails": "search_emails",
            "list_messages": "search_emails",
            "listar": "search_emails",
            "ver": "search_emails",
            "inbox": "search_emails",
            "read": "read_email",
            "read_email": "read_email",
            "get": "read_email",
            "get_email": "read_email",
            "leer": "read_email",
            "view": "read_email",
            "send": "send_email",
            "send_email": "send_email",
            "enviar": "send_email",
            "compose": "send_email",
        }
        action = action_map.get(str(action).lower().strip(), action)

        try:
            if action == "search_emails":
                return self._search_emails(params)
            elif action == "read_email":
                return self._read_email(params)
            elif action == "send_email":
                return self._send_email(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Gmail tool execution error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Gmail error: {exc}", success=False)

    def _search_emails(self, params: Dict[str, Any]) -> ToolResult:
        q = params.get("query", "").strip() or "is:inbox"
        max_r = min(int(params.get("max_results") or 5), 20)

        def _search(token: str) -> tuple[int, Dict[str, Any]]:
            url = f"{_GMAIL_API_BASE}/messages"
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, params={"q": q, "maxResults": max_r}, timeout=20.0)
            return r.status_code, r.json()

        status, data = call_with_refresh(_search, self._credentials_path)
        if status != 200:
            error_msg = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error searching emails ({status}): {error_msg}", success=False)

        messages = data.get("messages", [])
        if not messages:
            return ToolResult(tool_name=_TOOL_NAME, content=f"No se encontraron correos para la búsqueda: '{q}'.", success=True)

        results = []
        for m in messages:
            m_id = m.get("id")

            def _get_meta(token: str) -> tuple[int, Dict[str, Any]]:
                u = f"{_GMAIL_API_BASE}/messages/{m_id}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date"
                resp = httpx.get(u, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
                return resp.status_code, resp.json()

            st, m_data = call_with_refresh(_get_meta, self._credentials_path)
            if st == 200:
                headers = {h["name"]: h["value"] for h in m_data.get("payload", {}).get("headers", [])}
                subject = headers.get("Subject", "(Sin asunto)")
                sender = headers.get("From", "(Remitente desconocido)")
                date = headers.get("Date", "")
                snippet = m_data.get("snippet", "")
                results.append(
                    f"• ID: `{m_id}`\n"
                    f"  De: {sender}\n"
                    f"  Fecha: {date}\n"
                    f"  Asunto: {subject}\n"
                    f"  Extracto: {snippet[:120]}..."
                )

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"Correos encontrados ({len(results)}):\n\n" + "\n\n".join(results),
            success=True,
            metadata={"count": len(results)},
        )

    def _read_email(self, params: Dict[str, Any]) -> ToolResult:
        m_id = str(params.get("message_id") or params.get("id") or params.get("email_id") or "").strip()
        if not m_id:
            return ToolResult(tool_name=_TOOL_NAME, content="'message_id' is required for read_email.", success=False)

        def _get_msg(token: str) -> tuple[int, Dict[str, Any]]:
            u = f"{_GMAIL_API_BASE}/messages/{m_id}?format=full"
            resp = httpx.get(u, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
            return resp.status_code, resp.json()

        st, data = call_with_refresh(_get_msg, self._credentials_path)
        if st != 200:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error reading email {m_id} ({st}): {err}", success=False)

        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "(Sin asunto)")
        sender = headers.get("From", "(Remitente desconocido)")
        date = headers.get("Date", "")
        snippet = data.get("snippet", "")

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=(
                f"Correo ID: {m_id}\n"
                f"De: {sender}\n"
                f"Fecha: {date}\n"
                f"Asunto: {subject}\n\n"
                f"Contenido / Extracto:\n{snippet}"
            ),
            success=True,
            metadata={"id": m_id, "subject": subject, "sender": sender},
        )

    def _send_email(self, params: Dict[str, Any]) -> ToolResult:
        to = str(params.get("to") or params.get("recipient") or params.get("destinatario") or "").strip()
        subject = str(params.get("subject") or params.get("asunto") or "").strip()
        body = str(params.get("body") or params.get("content") or params.get("mensaje") or params.get("text") or "").strip()

        if not to or not subject or not body:
            return ToolResult(tool_name=_TOOL_NAME, content="'to', 'subject', and 'body' are required to send an email.", success=False)

        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        def _send(token: str) -> tuple[int, Dict[str, Any]]:
            u = f"{_GMAIL_API_BASE}/messages/send"
            resp = httpx.post(u, headers={"Authorization": f"Bearer {token}"}, json={"raw": raw}, timeout=20.0)
            return resp.status_code, resp.json()

        st, data = call_with_refresh(_send, self._credentials_path)
        if st in (200, 201):
            sent_id = data.get("id", "")
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"Correo enviado exitosamente a {to} (Asunto: '{subject}', ID: {sent_id}).",
                success=True,
                metadata={"sent_id": sent_id, "to": to},
            )
        else:
            err = data.get("error", {}).get("message", str(data))
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error enviando correo ({st}): {err}", success=False)


__all__ = ["GoogleGmailTool"]
