"""Screen vision and desktop interaction tool for OpenJarvis on Linux/Wayland."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.speech.gemini_stt import _get_api_key
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "screen_vision"


def _capture_screen_spectacle(output_path: str, mode: str = "fullscreen") -> bool:
    """Capture screen using spectacle under KDE Plasma Wayland/X11."""
    env = os.environ.copy()
    if "WAYLAND_DISPLAY" not in env:
        env["WAYLAND_DISPLAY"] = "wayland-1"
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"

    # -b: background, -n: nonotify (prevents waiting for notification popup), -d 0: no delay
    cmd = ["spectacle", "-b", "-n", "-d", "0"]
    if mode == "active":
        cmd.append("-a")
    elif mode == "current":
        cmd.append("-m")
    else:
        cmd.append("-f")  # fullscreen

    cmd.extend(["-o", output_path])

    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
        return res.returncode == 0 and Path(output_path).is_file() and Path(output_path).stat().st_size > 0
    except Exception as exc:
        logger.warning("Spectacle capture failed: %s", exc)
        return False


def _send_photo_telegram(image_path: str, caption: str = "") -> bool:
    """Send captured image directly to user Telegram chat."""
    try:
        from openjarvis.connectors.telegram_notify import get_telegram_config

        token, chat_id = get_telegram_config()
        if not token or not chat_id:
            return False

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(image_path, "rb") as f:
            resp = httpx.post(
                url,
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": (Path(image_path).name, f, "image/png")},
                timeout=25.0,
            )
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Failed to send photo to Telegram: %s", exc)
        return False


def _analyze_image_gemini(image_path: str, prompt: str) -> str:
    """Analyze screenshot using Gemini Vision API."""
    api_key = _get_api_key()
    if not api_key:
        return "Error: No hay clave de Gemini API configurada para visión."

    try:
        with open(image_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")

        models = [
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.8-flash",
            "gemini-3.1-flash-lite",
        ]
        for m in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {"inlineData": {"mimeType": "image/png", "data": b64_data}},
                        ]
                    }
                ]
            }
            resp = httpx.post(url, json=payload, timeout=25.0)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
        return "No se pudo obtener análisis del modelo visual de Gemini."
    except Exception as exc:
        logger.exception("Gemini Vision analysis error")
        return f"Error al analizar la imagen: {exc}"


@ToolRegistry.register(_TOOL_NAME)
class ScreenVisionTool(BaseTool):
    """Tool to inspect, capture, and notify user on Linux screen/desktop."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Screen vision and desktop interaction. Actions: "
                "'screenshot' (captures current screen and optionally sends to Telegram), "
                "'inspect' (captures screen and analyzes it with Gemini Vision to answer user questions about what is on screen), "
                "'notify' (displays a native KDE desktop notification popup using 'title' and 'message'), "
                "'lock_screen' (locks user desktop session)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["screenshot", "inspect", "notify", "lock_screen"],
                        "description": "Action to perform.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Question or prompt for 'inspect' action (e.g. '¿Qué error tengo en pantalla?', 'Resume el documento abierto').",
                        "default": "Analiza detalladamente lo que se ve en la pantalla, identifica qué ventanas están abiertas y resume la actividad actual.",
                    },
                    "send_telegram": {
                        "type": "boolean",
                        "description": "Whether to send the screenshot as a photo to Telegram.",
                        "default": True,
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for 'notify' desktop notification.",
                        "default": "Jarvis",
                    },
                    "message": {
                        "type": "string",
                        "description": "Body text for 'notify' desktop notification.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fullscreen", "active", "current"],
                        "description": "Capture mode ('fullscreen', 'active' window, 'current' screen).",
                        "default": "fullscreen",
                    },
                },
                "required": ["action"],
            },
            category="system",
            requires_confirmation=False,
            timeout_seconds=30.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

        try:
            if action == "screenshot":
                return self._screenshot(params)
            elif action == "inspect":
                return self._inspect(params)
            elif action == "notify":
                return self._notify(params)
            elif action == "lock_screen":
                return self._lock_screen()
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Screen vision error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Screen vision error: {exc}", success=False)

    def _screenshot(self, params: Dict[str, Any]) -> ToolResult:
        mode = params.get("mode", "fullscreen")
        send_tg = params.get("send_telegram", True)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            path = tf.name

        ok = _capture_screen_spectacle(path, mode=mode)
        if not ok:
            return ToolResult(tool_name=_TOOL_NAME, content="Error al capturar la pantalla con Spectacle.", success=False)

        tg_status = ""
        if send_tg:
            sent = _send_photo_telegram(path, caption="🖥️ Captura de pantalla actual (VivoBook)")
            tg_status = " Enviada a tu Telegram." if sent else " No se pudo enviar por Telegram."

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"📸 Captura de pantalla realizada con éxito.{tg_status}",
            success=True,
            metadata={"path": path, "telegram_sent": bool(tg_status and "Enviada" in tg_status)},
        )

    def _inspect(self, params: Dict[str, Any]) -> ToolResult:
        prompt = params.get("prompt") or "Analiza detalladamente lo que se ve en la pantalla, identifica qué ventanas están abiertas y resume la actividad actual."
        send_tg = params.get("send_telegram", False)
        mode = params.get("mode", "fullscreen")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            path = tf.name

        ok = _capture_screen_spectacle(path, mode=mode)
        if not ok:
            return ToolResult(tool_name=_TOOL_NAME, content="Error al capturar la pantalla para inspección.", success=False)

        if send_tg:
            _send_photo_telegram(path, caption="🖥️ Captura analizada por Jarvis Vision")

        analysis = _analyze_image_gemini(path, prompt)

        try:
            os.unlink(path)
        except Exception:
            pass

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"👁️ **Análisis visual de pantalla:**\n\n{analysis}",
            success=True,
            metadata={"analysis": analysis},
        )

    def _notify(self, params: Dict[str, Any]) -> ToolResult:
        title = params.get("title", "Jarvis")
        message = params.get("message", "")
        if not message:
            return ToolResult(tool_name=_TOOL_NAME, content="El parámetro 'message' es requerido para notificar.", success=False)

        env = os.environ.copy()
        if "WAYLAND_DISPLAY" not in env:
            env["WAYLAND_DISPLAY"] = "wayland-1"
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"

        try:
            res = subprocess.run(["notify-send", "-a", title, "-i", "dialog-information", title, message], env=env, timeout=5)
            if res.returncode == 0:
                return ToolResult(tool_name=_TOOL_NAME, content=f"🔔 Notificación mostrada en pantalla: [{title}] {message}", success=True)
            return ToolResult(tool_name=_TOOL_NAME, content="notify-send devolvió código de error.", success=False)
        except Exception as exc:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error al enviar notificación: {exc}", success=False)

    def _lock_screen(self) -> ToolResult:
        try:
            res = subprocess.run(["loginctl", "lock-session"], timeout=5)
            if res.returncode == 0:
                return ToolResult(tool_name=_TOOL_NAME, content="🔒 Pantalla bloqueada correctamente.", success=True)
            return ToolResult(tool_name=_TOOL_NAME, content="loginctl lock-session falló.", success=False)
        except Exception as exc:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error al bloquear pantalla: {exc}", success=False)


__all__ = ["ScreenVisionTool"]
