"""Edge TTS voice synthesis helper for Telegram and local audio with natural speech preprocessing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "es-ES-AlvaroNeural"  # Microsoft's flagship articulate Spanish male voice
_DEFAULT_RATE = "+4%"  # Lively, natural conversational cadence


def _resolve_voice_and_rate() -> tuple[str, str]:
    """Retrieve configured voice and speech rate from config or environment."""
    voice = os.environ.get("OPENJARVIS_TTS_VOICE", "")
    rate = os.environ.get("OPENJARVIS_TTS_RATE", "")

    if not voice or not rate:
        try:
            import tomllib
            from openjarvis.core.paths import get_config_dir

            cfg_path = get_config_dir() / "config.toml"
            if cfg_path.is_file():
                with open(cfg_path, "rb") as f:
                    raw = tomllib.load(f)
                    sp = raw.get("speech", {})
                    if not voice and sp.get("voice_id"):
                        voice = sp["voice_id"]
                    if not voice and sp.get("voice"):
                        voice = sp["voice"]
                    if not rate and sp.get("voice_rate"):
                        rate = sp["voice_rate"]
        except Exception as exc:
            logger.debug("Error reading TTS config: %s", exc)

    return voice or _DEFAULT_VOICE, rate or _DEFAULT_RATE


def clean_text_for_speech(text: str, max_chars: int = 1400) -> str:
    """Sanitize and phonetically adapt text for natural human speech synthesis."""
    if not text:
        return ""

    t = text

    # 1. Strip raw code blocks and inline code
    t = re.sub(r"```[\s\S]*?```", " código omitido. ", t)
    t = re.sub(r"`[^`]+`", lambda m: m.group(0).strip("`"), t)

    # 2. Strip URLs
    t = re.sub(r"https?://\S+", "", t)

    # 3. Strip database and resource IDs (e.g. [ID: people/c...] or (ID: 68r3g...))
    t = re.sub(r"\[ID:\s*[^\]]+\]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\(ID:\s*[^\)]+\)", "", t, flags=re.IGNORECASE)

    # 4. Strip emojis and unicode symbols
    t = re.sub(
        r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50\u200d\ufe0f]",
        "",
        t,
    )

    # 5. Format phone numbers for natural spoken chunks (avoids reading as billions/millions)
    def format_phone(m: re.Match[str]) -> str:
        raw = re.sub(r"[^\d+]", "", m.group(0))
        # Venezuelan number +58 4XX XXX XX XX
        if raw.startswith("+58") and len(raw) == 13:
            return f"más 58, {raw[3:6]}, {raw[6:9]}, {raw[9:11]}, {raw[11:13]}"
        if raw.startswith("04") and len(raw) == 11:
            return f"0{raw[1:4]}, {raw[4:7]}, {raw[7:9]}, {raw[9:11]}"
        # Generic international numbers: read in friendly pairs/triplets
        if len(raw) >= 7:
            parts = [raw[i : i + 3] for i in range(0, len(raw), 3)]
            return " ".join(parts)
        return " ".join(raw)

    t = re.sub(r"\+?\d{2,4}[-.\s]?\d{3}[-.\s]?\d{3,4}[-.\s]?\d{2,4}", format_phone, t)

    # 6. Expand common technical abbreviations and units for natural pronunciation
    t = re.sub(r"\bGB\b", " gigabytes", t)
    t = re.sub(r"\bMB\b", " megabytes", t)
    t = re.sub(r"\bRAM\b", " memoria Ram", t)
    t = re.sub(r"\bCPU\b", "procesador", t)
    t = re.sub(r"\bNVMe\b", "disco de estado sólido", t)
    t = re.sub(r"\bSO\b", "sistema operativo", t)
    t = re.sub(r"%", " por ciento", t)
    t = re.sub(r"°C\b", " grados centígrados", t)
    t = re.sub(r"\bPID\s*(\d+)", r"proceso \1", t)

    # 7. Strip markdown styling characters while keeping natural pauses
    t = re.sub(r"[\*\_#•\-]+", " ", t)

    # 8. Collapse whitespace and repeated punctuation
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*\.\s*", ". ", t)

    if len(t) > max_chars:
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
    voice: str | None = None,
    rate: str | None = None,
) -> bool:
    """Synthesize text to an audio file asynchronously."""
    try:
        import edge_tts

        cfg_voice, cfg_rate = _resolve_voice_and_rate()
        target_voice = voice or cfg_voice
        target_rate = rate or cfg_rate

        clean = clean_text_for_speech(text)
        if not clean:
            return False

        communicate = edge_tts.Communicate(clean, target_voice, rate=target_rate)
        await communicate.save(output_path)
        return Path(output_path).is_file() and Path(output_path).stat().st_size > 0
    except Exception as exc:
        logger.warning("Edge TTS synthesis failed: %s", exc)
        return False


def synthesize_speech(
    text: str,
    output_path: str,
    voice: str | None = None,
    rate: str | None = None,
) -> bool:
    """Synchronous wrapper for synthesize_speech_async safe for active event loops."""
    try:
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        if loop is not None and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    synthesize_speech_async(text, output_path, voice, rate),
                )
                return future.result(timeout=25.0)
        else:
            return asyncio.run(
                synthesize_speech_async(text, output_path, voice, rate)
            )
    except Exception as exc:
        logger.warning("Synchronous Edge TTS error: %s", exc)
        return False


__all__ = ["clean_text_for_speech", "synthesize_speech_async", "synthesize_speech"]
