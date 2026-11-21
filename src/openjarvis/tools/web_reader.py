"""Web reader tool for extracting clean article text from URLs for summarization."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

import httpx
from bs4 import BeautifulSoup
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "web_reader"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@ToolRegistry.register(_TOOL_NAME)
class WebReaderTool(BaseTool):
    """Tool to fetch and extract clean readable text from web pages."""

    tool_id = _TOOL_NAME
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Fetch a web page from a URL and extract its clean readable text and article content, "
                "stripping ads, scripts, navigation bars, and headers. Use this whenever the user shares "
                "a link or asks for a summary of a web page/article."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full HTTP or HTTPS URL to read.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters of text to return (default 12000).",
                        "default": 12000,
                    },
                },
                "required": ["url"],
            },
            category="web",
            requires_confirmation=False,
            timeout_seconds=30.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        url = params.get("url", "").strip()
        if not url:
            return ToolResult(tool_name=_TOOL_NAME, content="Missing required 'url' parameter.", success=False)

        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        max_chars = min(int(params.get("max_chars", 12000)), 25000)

        try:
            headers = {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            }
            with httpx.Client(follow_redirects=True, timeout=25.0) as client:
                resp = client.get(url, headers=headers)

            if resp.status_code != 200:
                return ToolResult(
                    tool_name=_TOOL_NAME,
                    content=f"No fue posible acceder al enlace (HTTP {resp.status_code}).",
                    success=False,
                )

            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract Title
            title = soup.title.string.strip() if soup.title and soup.title.string else "Sin título"

            # Remove noisy tags
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "iframe"]):
                tag.decompose()

            # Target article or main if present, else body
            main_elem = soup.find("article") or soup.find("main") or soup.find("div", {"role": "main"}) or soup.body

            if not main_elem:
                return ToolResult(tool_name=_TOOL_NAME, content="No se encontró contenido legible en la página.", success=False)

            # Extract clean paragraphs
            paragraphs = []
            for p in main_elem.find_all(["p", "h1", "h2", "h3", "h4", "li"]):
                txt = p.get_text(strip=True)
                if len(txt) > 20:
                    paragraphs.append(txt)

            full_text = "\n\n".join(paragraphs)
            if not full_text:
                full_text = main_elem.get_text(separator="\n", strip=True)

            # Collapse extra newlines
            clean_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

            if len(clean_text) > max_chars:
                clean_text = clean_text[:max_chars] + f"\n\n... [Texto truncado a {max_chars} caracteres para el resumen]"

            result_msg = (
                f"🌐 **Fuente:** {url}\n"
                f"📰 **Título:** {title}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{clean_text}"
            )

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=result_msg,
                success=True,
                metadata={"title": title, "url": url, "char_count": len(clean_text)},
            )

        except Exception as exc:
            logger.exception("Web reader error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error leyendo URL: {exc}", success=False)


__all__ = ["WebReaderTool"]
