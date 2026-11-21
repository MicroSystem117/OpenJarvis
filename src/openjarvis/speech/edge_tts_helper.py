"""Edge TTS voice synthesis helper for Telegram and local audio."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "es-VE-SebastianNeural"


def clean_text_for_speech(text: str, max_chars: int = 1200) -> str:
    """Sanitize markdown text for natural speech synthesis."""
    if not text:
        return ""

    # Replace code blocks
    t = re.sub(r"```[\s\S]*?```", " [código omitido] ", text)
    # Remove URLs
    t = re.sub(r"https?://\S+", "", t)
    # Remove markdown formatting characters
    t = re.sub(r"[\*\_`#•\-]", " ", t)
    # Replace multiple spaces/newlines with single space
    t = re.sub(r"\s+", " ", t).strip()

    if len(t) > max_chars:
        # Cut at sentence boundary if possible
        cut = t[:max_chars]
        last_period = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if last_period > 300:
            t = cut[: last_period + 1]
        else:
            t = cut + "..."

    return t


async def synthesize_speech_async(
    text: str,
    output_path: str,
    voice: str = _DEFAULT_VOICE,
) -> bool:
    """Synthesize text to an audio file asynchronously."""
    try:
        import edge_tts

        clean = clean_text_for_speech(text)
        if not clean:
            return False

        communicate = edge_tts.Communicate(clean, voice)
        await communicate.save(output_path)
        return Path(output_path).is_file() and Path(output_path).stat().st_size > 0
    except Exception as exc:
        logger.warning("Edge TTS synthesis failed: %s", exc)
        return False


def synthesize_speech(
    text: str,
    output_path: str,
    voice: str = _DEFAULT_VOICE,
) -> bool:
    """Synchronous wrapper for synthesize_speech_async."""
    try:
        return asyncio.run(synthesize_speech_async(text, output_path, voice))
    except Exception as exc:
        logger.warning("Synchronous Edge TTS error: %s", exc)
        return False


__all__ = ["clean_text_for_speech", "synthesize_speech_async", "synthesize_speech"]
