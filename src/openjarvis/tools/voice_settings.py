"""Voice settings tool for switching Edge TTS neural voices on the fly."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict

from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "voice_settings"

_VOICE_ALIASES: Dict[str, str] = {
    "alvaro": "es-ES-AlvaroNeural",
    "álvaro": "es-ES-AlvaroNeural",
    "jorge": "es-MX-JorgeNeural",
    "alonso": "es-US-AlonsoNeural",
    "gonzalo": "es-CO-GonzaloNeural",
    "sebastian": "es-VE-SebastianNeural",
    "sebastián": "es-VE-SebastianNeural",
    "tomas": "es-AR-TomasNeural",
    "tomás": "es-AR-TomasNeural",
    "lorenzo": "es-CL-LorenzoNeural",
    "alex": "es-PE-AlexNeural",
    "emilio": "es-DO-EmilioNeural",
    "marcelo": "es-BO-MarceloNeural",
    "dalia": "es-MX-DaliaNeural",
    "salome": "es-CO-SalomeNeural",
    "salomé": "es-CO-SalomeNeural",
    "paola": "es-VE-PaolaNeural",
    "paloma": "es-US-PalomaNeural",
    "elena": "es-AR-ElenaNeural",
    "elvira": "es-ES-ElviraNeural",
    "ximena": "es-ES-XimenaNeural",
}


@ToolRegistry.register(_TOOL_NAME)
class VoiceSettingsTool(BaseTool):
    """Tool to view and switch Jarvis voice settings on the fly."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "View and change Jarvis's speaking voice and cadence. Actions: "
                "'list_voices' to see available voices; "
                "'set_voice' with 'voice' (e.g. 'Alonso', 'Gonzalo', 'Alvaro', 'Jorge', 'Dalia') "
                "and optional 'rate' (e.g. '+4%', '+0%', '-3%') to switch speaking voice immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_voices", "set_voice"],
                        "description": "Action to perform.",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice name or ID to set (e.g. 'Alonso', 'Gonzalo', 'Jorge', 'Alvaro').",
                    },
                    "rate": {
                        "type": "string",
                        "description": "Speech speed rate (e.g. '+4%', '+0%', '-2%').",
                        "default": "+4%",
                    },
                },
                "required": ["action"],
            },
            category="system",
            requires_confirmation=False,
            timeout_seconds=15.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'action' parameter.", success=False)

        try:
            if action == "list_voices":
                return self._list_voices()
            elif action == "set_voice":
                return self._set_voice(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)
        except Exception as exc:
            logger.exception("Voice settings error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Voice settings error: {exc}", success=False)

    def _list_voices(self) -> ToolResult:
        catalog = (
            "🎙️ **Voces Principales Disponibles:**\n\n"
            "**Masculinas (Recomendadas para Jarvis):**\n"
            "• `Alvaro` (es-ES-AlvaroNeural): Timbre profundo, firme y articulado (estilo Jarvis cinematográfico).\n"
            "• `Alonso` (es-US-AlonsoNeural): Acento latino neutro / internacional, moderno y ágil.\n"
            "• `Jorge` (es-MX-JorgeNeural): Acento mexicano, cálido, amigable y conversacional.\n"
            "• `Gonzalo` (es-CO-GonzaloNeural): Acento colombiano, suave, respetuoso y pausado.\n"
            "• `Tomas` (es-AR-TomasNeural): Acento argentino.\n"
            "• `Sebastian` (es-VE-SebastianNeural): Acento venezolano.\n\n"
            "**Femeninas:**\n"
            "• `Dalia` (es-MX-DaliaNeural): Muy natural y expresiva.\n"
            "• `Salome` (es-CO-SalomeNeural): Melódica y clara.\n"
            "• `Paola` (es-VE-PaolaNeural): Acento venezolano.\n"
            "• `Paloma` (es-US-PalomaNeural): Acento latino neutro."
        )
        return ToolResult(tool_name=_TOOL_NAME, content=catalog, success=True)

    def _set_voice(self, params: Dict[str, Any]) -> ToolResult:
        v_input = params.get("voice", "").strip().lower()
        if not v_input:
            return ToolResult(tool_name=_TOOL_NAME, content="'voice' is required for set_voice.", success=False)

        target_voice = _VOICE_ALIASES.get(v_input, params.get("voice", "").strip())
        rate = params.get("rate", "+4%").strip()

        # Update config.toml
        cfg_path = get_config_dir() / "config.toml"
        if cfg_path.is_file():
            content = cfg_path.read_text(encoding="utf-8")
            # If [speech] section exists
            if "[speech]" in content:
                content = re.sub(r'voice\s*=\s*"[^"]*"', f'voice = "{target_voice}"', content)
                content = re.sub(r'voice_rate\s*=\s*"[^"]*"', f'voice_rate = "{rate}"', content)
                if f'voice = "{target_voice}"' not in content:
                    content = content.replace("[speech]", f'[speech]\nvoice = "{target_voice}"\nvoice_rate = "{rate}"')
            else:
                content += f'\n\n[speech]\nvoice = "{target_voice}"\nvoice_rate = "{rate}"\n'

            cfg_path.write_text(content, encoding="utf-8")

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=(
                f"✅ Voz actualizada con éxito:\n"
                f"• **Voz:** `{target_voice}`\n"
                f"• **Velocidad:** `{rate}`\n\n"
                f"Mis próximas notas de voz se generarán con este tono."
            ),
            success=True,
            metadata={"voice": target_voice, "rate": rate},
        )


__all__ = ["VoiceSettingsTool"]
