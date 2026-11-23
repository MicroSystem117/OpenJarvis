"""Speech-to-text transcribing helper using Gemini multimodal API."""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _get_api_key() -> str:
    """Retrieve Gemini API key from environment or cloud-keys.env."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    if key:
        return key

    from openjarvis.core.paths import get_config_dir

    env_path = get_config_dir() / "cloud-keys.env"
    if env_path.is_file():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY=") or line.startswith("GOOGLE_API_KEY="):
                        return line.split("=", 1)[1].strip("\"' ")
        except Exception as exc:
            logger.debug("Error reading cloud-keys.env: %s", exc)

    return ""


def transcribe_audio_bytes(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    prompt: str = "Transcribe exactamente el siguiente audio a texto en español. Solo devuelve la transcripción literal, sin preámbulos ni explicaciones adicionales.",
) -> str:
    """Transcribe audio bytes using Gemini 3.1 Flash Lite."""
    api_key = _get_api_key()
    if not api_key:
        logger.error("No Gemini API key available for audio transcription.")
        return ""

    if not audio_bytes:
        return ""

    b64_data = base64.b64encode(audio_bytes).decode("utf-8")
    models = [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
    ]

    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
        try:
            resp = httpx.post(url, json=payload, timeout=25.0)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
            elif resp.status_code in (503, 429):
                logger.warning(
                    "Gemini audio transcription transient error (%s) on %s. Trying next model...",
                    resp.status_code,
                    m,
                )
                continue
            else:
                logger.warning(
                    "Gemini audio transcription failed (%s) on %s: %s",
                    resp.status_code,
                    m,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Gemini audio transcription exception on %s: %s", m, exc)
            continue
    return ""


__all__ = ["transcribe_audio_bytes"]
