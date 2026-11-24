"""Tests for resilient parameter handling across Google tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.tools.google_calendar import GoogleCalendarTool
from openjarvis.tools.google_tasks import GoogleTasksTool
from openjarvis.tools.google_gmail import GoogleGmailTool
from openjarvis.tools.google_drive import GoogleDriveTool
from openjarvis.tools.google_contacts import GoogleContactsTool
from openjarvis.tools._stubs import ToolExecutor, ToolCall


class TestGoogleToolsResilience:
    """Ensure tools automatically handle nested parameters and infer missing actions."""

    @patch("openjarvis.tools.google_calendar.call_with_refresh")
    def test_calendar_infers_create_event_when_action_missing(self, mock_call):
        mock_call.return_value = (200, {"id": "ev123", "htmlLink": "https://cal/ev123"})
        tool = GoogleCalendarTool()

        # Invocation with NO action parameter, but summary and start_time provided
        res = tool.execute(summary="Boda de mi papá", start_time="2026-11-27T18:00:00")
        assert res.success is True
        assert "Evento creado exitosamente" in res.content
        mock_call.assert_called_once()

    @patch("openjarvis.tools.google_calendar.call_with_refresh")
    def test_calendar_unwraps_nested_parameters(self, mock_call):
        mock_call.return_value = (200, {"id": "ev456", "htmlLink": "https://cal/ev456"})
        tool = GoogleCalendarTool()

        # Invocation nested inside parameters dict
        res = tool.execute(parameters={"summary": "Cena familiar", "start_time": "2026-11-27T20:00:00"})
        assert res.success is True
        assert "Evento creado exitosamente" in res.content

    @patch("openjarvis.tools.google_calendar.call_with_refresh")
    def test_calendar_defaults_to_list_events(self, mock_call):
        mock_call.return_value = (200, {"items": []})
        tool = GoogleCalendarTool()

        # Invocation with no parameters at all
        res = tool.execute()
        assert res.success is True
        assert "Próximos eventos" in res.content or "No se encontraron" in res.content

    @patch("openjarvis.tools.google_tasks.call_with_refresh")
    def test_tasks_infers_create_task_when_action_missing(self, mock_call):
        mock_call.return_value = (200, {"id": "task123", "title": "Comprar flores"})
        tool = GoogleTasksTool()

        res = tool.execute(title="Comprar flores")
        assert res.success is True
        assert "creada exitosamente" in res.content

    @patch("openjarvis.tools.google_tasks.call_with_refresh")
    def test_tasks_unwraps_nested_parameters(self, mock_call):
        mock_call.return_value = (200, {"id": "task124", "title": "Pagar luz"})
        tool = GoogleTasksTool()

        res = tool.execute(params={"title": "Pagar luz", "due": "2026-11-30"})
        assert res.success is True
        assert "creada exitosamente" in res.content

    @patch("openjarvis.tools.google_gmail.call_with_refresh")
    def test_gmail_infers_send_email_when_action_missing(self, mock_call):
        mock_call.return_value = (200, {"id": "msg123"})
        tool = GoogleGmailTool()

        res = tool.execute(to="test@example.com", subject="Hola", body="Mensaje de prueba")
        assert res.success is True
        assert "enviado exitosamente" in res.content

    @patch("openjarvis.tools.google_gmail.call_with_refresh")
    def test_gmail_infers_search_emails_when_query_present(self, mock_call):
        mock_call.return_value = (200, {"messages": []})
        tool = GoogleGmailTool()

        res = tool.execute(query="is:unread")
        assert res.success is True

    @patch("openjarvis.tools.google_drive.call_with_refresh")
    def test_drive_infers_read_file_when_file_id_present(self, mock_call):
        mock_call.side_effect = [
            (200, {"mimeType": "text/plain", "name": "doc.txt"}),
            (200, "Contenido del archivo"),
        ]
        tool = GoogleDriveTool()

        res = tool.execute(file_id="file123")
        assert res.success is True

    @patch("openjarvis.tools.google_contacts.call_with_refresh")
    def test_contacts_infers_search_contacts_when_query_present(self, mock_call):
        mock_call.return_value = (200, {"results": []})
        tool = GoogleContactsTool()

        res = tool.execute(query="Carlos")
        assert res.success is True

    def test_tool_executor_unwraps_nested_arguments(self):
        tool = MagicMock()
        tool.spec.name = "mock_tool"
        tool.spec.required_capabilities = []
        tool.spec.requires_confirmation = False
        tool.spec.timeout_seconds = 5.0
        tool.is_local = True
        tool.execute.return_value = MagicMock(success=True, content="ok", metadata={})

        executor = ToolExecutor(tools=[tool])
        call = ToolCall(
            id="call_1",
            name="mock_tool",
            arguments=json.dumps({"parameters": {"action": "custom_action", "value": 42}}),
        )

        res = executor.execute(call)
        assert res.success is True
        # Verify tool.execute received flat kwargs with action and value
        tool.execute.assert_called_once()
        _, kwargs = tool.execute.call_args
        assert kwargs.get("action") == "custom_action"
        assert kwargs.get("value") == 42
