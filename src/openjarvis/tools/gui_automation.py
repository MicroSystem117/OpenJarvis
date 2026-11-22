"""Native Linux/Wayland GUI automation tool for OpenJarvis via evdev/uinput."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "gui_automation"

try:
    from evdev import UInput, ecodes
    _EVDEV_AVAILABLE = True
except ImportError:
    _EVDEV_AVAILABLE = False
    UInput = None
    ecodes = None


_DESKTOP_DIRS = [
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path(os.path.expanduser("~/.local/share/applications")),
    Path(os.path.expanduser("~/.local/share/flatpak/exports/share/applications")),
]

_APP_ALIASES = {
    "chrome": "google-chrome",
    "google chrome": "google-chrome",
    "browser": "google-chrome",
    "navegador": "google-chrome",
    "whatsapp": "com.rtosta.zapzap",
    "zap": "com.rtosta.zapzap",
    "zapzap": "com.rtosta.zapzap",
    "ide": "antigravity-ide",
    "code": "antigravity-ide",
    "antigravity": "antigravity-ide",
    "antigravity-ide": "antigravity-ide",
    "terminal": "Alacritty",
    "alacritty": "Alacritty",
    "consola": "Alacritty",
    "dolphin": "org.kde.dolphin",
    "archivos": "org.kde.dolphin",
    "carpetas": "org.kde.dolphin",
    "explorador": "org.kde.dolphin",
    "calculadora": "org.kde.kcalc",
    "calc": "org.kde.kcalc",
    "kcalc": "org.kde.kcalc",
    "telegram": "org.telegram.desktop",
    "tg": "org.telegram.desktop",
    "obs": "com.obsproject.Studio",
    "discord": "discord",
    "steam": "steam",
    "spotify": "spotify",
    "preferencias": "systemsettings",
    "configuracion": "systemsettings",
    "ajustes": "systemsettings",
}


def find_desktop_file(query: str) -> Tuple[Optional[str], Optional[str]]:
    """Find desktop entry file for application name or alias."""
    q = query.lower().strip()
    target_id = _APP_ALIASES.get(q, q)

    # 1. Direct exact match
    for d in _DESKTOP_DIRS:
        if not d.exists():
            continue
        direct = d / f"{target_id}.desktop"
        if direct.exists():
            return target_id, str(direct)

    # 2. Match stem
    for d in _DESKTOP_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.desktop"):
            stem_low = f.stem.lower()
            if target_id == stem_low or target_id in stem_low or q in stem_low:
                return f.stem, str(f)

    # 3. Match Name= inside desktop entry
    for d in _DESKTOP_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.desktop"):
            try:
                with open(f, "r", errors="ignore") as fp:
                    for line in fp:
                        if line.startswith("Name=") and q in line[5:].lower():
                            return f.stem, str(f)
            except Exception:
                pass

    return None, None


def _focus_desktop_app(app_name: str) -> bool:
    """Bring specified application window to the foreground under KDE Plasma / Wayland."""
    name = app_name.lower().strip()
    target_query = name
    if name in ("whatsapp", "zap", "zapzap"):
        target_query = "zapzap"
    elif name in ("code", "ide"):
        target_query = "antigravity"
    elif name in ("chrome", "browser", "navegador"):
        target_query = "chrome"
    elif name in ("tg", "telegram"):
        target_query = "telegram"
    elif name in ("calculadora", "calc", "kcalc"):
        target_query = "kcalc"
    elif name in ("dolphin", "archivos", "carpetas", "explorador"):
        target_query = "dolphin"
    elif name in ("terminal", "consola", "alacritty"):
        target_query = "alacritty"

    try:
        token = uuid.uuid4().hex[:8]
        script = f"""
        var found = false;
        var windows = workspace.windowList();
        for (var i = 0; i < windows.length; i++) {{
            var w = windows[i];
            var cap = (w.caption || "").toLowerCase();
            var rc = (w.resourceClass || "").toLowerCase();
            if (cap.indexOf("{target_query}") !== -1 || rc.indexOf("{target_query}") !== -1) {{
                w.minimized = false;
                workspace.activeWindow = w;
                workspace.raiseWindow(w);
                found = true;
                break;
            }}
        }}
        console.info("JARVIS_KWIN_FOCUS:{token}:" + (found ? "FOUND" : "NOT_FOUND"));
        """
        script_file = f"/tmp/kwin_focus_{token}.js"
        plugin_name = f"focus_{token}"
        with open(script_file, "w") as f:
            f.write(script)

        load_res = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", script_file, plugin_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        sid = load_res.stdout.strip()
        if sid.isdigit() and int(sid) >= 0:
            subprocess.run(
                ["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
                capture_output=True,
                timeout=3,
            )
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin_name],
                capture_output=True,
                timeout=2,
            )
            time.sleep(0.08)
            j_res = subprocess.run(
                ["journalctl", "--user", "-u", "plasma-kwin_wayland", "-n", "25", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            for line in reversed(j_res.stdout.splitlines()):
                if f"JARVIS_KWIN_FOCUS:{token}:" in line:
                    return ":FOUND" in line
    except Exception as exc:
        logger.debug("KWin script focus failed: %s", exc)

    return False


# Comprehensive character mapping: (keycode, require_shift)
_CHAR_MAP: Dict[str, tuple[int, bool]] = {}
if _EVDEV_AVAILABLE:
    _CHAR_MAP = {
        # Lowercase
        "a": (ecodes.KEY_A, False), "b": (ecodes.KEY_B, False), "c": (ecodes.KEY_C, False),
        "d": (ecodes.KEY_D, False), "e": (ecodes.KEY_E, False), "f": (ecodes.KEY_F, False),
        "g": (ecodes.KEY_G, False), "h": (ecodes.KEY_H, False), "i": (ecodes.KEY_I, False),
        "j": (ecodes.KEY_J, False), "k": (ecodes.KEY_K, False), "l": (ecodes.KEY_L, False),
        "m": (ecodes.KEY_M, False), "n": (ecodes.KEY_N, False), "o": (ecodes.KEY_O, False),
        "p": (ecodes.KEY_P, False), "q": (ecodes.KEY_Q, False), "r": (ecodes.KEY_R, False),
        "s": (ecodes.KEY_S, False), "t": (ecodes.KEY_T, False), "u": (ecodes.KEY_U, False),
        "v": (ecodes.KEY_V, False), "w": (ecodes.KEY_W, False), "x": (ecodes.KEY_X, False),
        "y": (ecodes.KEY_Y, False), "z": (ecodes.KEY_Z, False),
        # Uppercase
        "A": (ecodes.KEY_A, True), "B": (ecodes.KEY_B, True), "C": (ecodes.KEY_C, True),
        "D": (ecodes.KEY_D, True), "E": (ecodes.KEY_E, True), "F": (ecodes.KEY_F, True),
        "G": (ecodes.KEY_G, True), "H": (ecodes.KEY_H, True), "I": (ecodes.KEY_I, True),
        "J": (ecodes.KEY_J, True), "K": (ecodes.KEY_K, True), "L": (ecodes.KEY_L, True),
        "M": (ecodes.KEY_M, True), "N": (ecodes.KEY_N, True), "O": (ecodes.KEY_O, True),
        "P": (ecodes.KEY_P, True), "Q": (ecodes.KEY_Q, True), "R": (ecodes.KEY_R, True),
        "S": (ecodes.KEY_S, True), "T": (ecodes.KEY_T, True), "U": (ecodes.KEY_U, True),
        "V": (ecodes.KEY_V, True), "W": (ecodes.KEY_W, True), "X": (ecodes.KEY_X, True),
        "Y": (ecodes.KEY_Y, True), "Z": (ecodes.KEY_Z, True),
        # Digits
        "0": (ecodes.KEY_0, False), "1": (ecodes.KEY_1, False), "2": (ecodes.KEY_2, False),
        "3": (ecodes.KEY_3, False), "4": (ecodes.KEY_4, False), "5": (ecodes.KEY_5, False),
        "6": (ecodes.KEY_6, False), "7": (ecodes.KEY_7, False), "8": (ecodes.KEY_8, False),
        "9": (ecodes.KEY_9, False),
        # Whitespace and formatting
        " ": (ecodes.KEY_SPACE, False), "\n": (ecodes.KEY_ENTER, False),
        "\t": (ecodes.KEY_TAB, False),
        # Punctuation & symbols
        ".": (ecodes.KEY_DOT, False), ",": (ecodes.KEY_COMMA, False),
        "-": (ecodes.KEY_MINUS, False), "_": (ecodes.KEY_MINUS, True),
        "=": (ecodes.KEY_EQUAL, False), "+": (ecodes.KEY_EQUAL, True),
        "/": (ecodes.KEY_SLASH, False), "\\": (ecodes.KEY_BACKSLASH, False),
        ";": (ecodes.KEY_SEMICOLON, False), ":": (ecodes.KEY_SEMICOLON, True),
        "'": (ecodes.KEY_APOSTROPHE, False), '"': (ecodes.KEY_APOSTROPHE, True),
        "`": (ecodes.KEY_GRAVE, False), "~": (ecodes.KEY_GRAVE, True),
        "[": (ecodes.KEY_LEFTBRACE, False), "{": (ecodes.KEY_LEFTBRACE, True),
        "]": (ecodes.KEY_RIGHTBRACE, False), "}": (ecodes.KEY_RIGHTBRACE, True),
        "!": (ecodes.KEY_1, True), "@": (ecodes.KEY_2, True), "#": (ecodes.KEY_3, True),
        "$": (ecodes.KEY_4, True), "%": (ecodes.KEY_5, True), "^": (ecodes.KEY_6, True),
        "&": (ecodes.KEY_7, True), "*": (ecodes.KEY_8, True), "(": (ecodes.KEY_9, True),
        ")": (ecodes.KEY_0, True), "<": (ecodes.KEY_COMMA, True), ">": (ecodes.KEY_DOT, True),
        "?": (ecodes.KEY_SLASH, True), "|": (ecodes.KEY_BACKSLASH, True),
    }

# Named special keys
_SPECIAL_KEYS: Dict[str, int] = {}
if _EVDEV_AVAILABLE:
    _SPECIAL_KEYS = {
        "enter": ecodes.KEY_ENTER,
        "return": ecodes.KEY_ENTER,
        "esc": ecodes.KEY_ESC,
        "escape": ecodes.KEY_ESC,
        "backspace": ecodes.KEY_BACKSPACE,
        "tab": ecodes.KEY_TAB,
        "space": ecodes.KEY_SPACE,
        "delete": ecodes.KEY_DELETE,
        "home": ecodes.KEY_HOME,
        "end": ecodes.KEY_END,
        "pageup": ecodes.KEY_PAGEUP,
        "pagedown": ecodes.KEY_PAGEDOWN,
        "up": ecodes.KEY_UP,
        "down": ecodes.KEY_DOWN,
        "left": ecodes.KEY_LEFT,
        "right": ecodes.KEY_RIGHT,
        "ctrl": ecodes.KEY_LEFTCTRL,
        "leftctrl": ecodes.KEY_LEFTCTRL,
        "rightctrl": ecodes.KEY_RIGHTCTRL,
        "alt": ecodes.KEY_LEFTALT,
        "leftalt": ecodes.KEY_LEFTALT,
        "rightalt": ecodes.KEY_RIGHTALT,
        "shift": ecodes.KEY_LEFTSHIFT,
        "leftshift": ecodes.KEY_LEFTSHIFT,
        "rightshift": ecodes.KEY_RIGHTSHIFT,
        "super": ecodes.KEY_LEFTMETA,
        "win": ecodes.KEY_LEFTMETA,
        "meta": ecodes.KEY_LEFTMETA,
        "f1": ecodes.KEY_F1, "f2": ecodes.KEY_F2, "f3": ecodes.KEY_F3,
        "f4": ecodes.KEY_F4, "f5": ecodes.KEY_F5, "f6": ecodes.KEY_F6,
        "f7": ecodes.KEY_F7, "f8": ecodes.KEY_F8, "f9": ecodes.KEY_F9,
        "f10": ecodes.KEY_F10, "f11": ecodes.KEY_F11, "f12": ecodes.KEY_F12,
        "volume_up": ecodes.KEY_VOLUMEUP,
        "volume_down": ecodes.KEY_VOLUMEDOWN,
        "mute": ecodes.KEY_MUTE,
        "play": ecodes.KEY_PLAYPAUSE,
        "pause": ecodes.KEY_PLAYPAUSE,
    }


def _create_virtual_device() -> Any:
    """Create a virtual UInput device supporting all standard keyboard and mouse events."""
    if not _EVDEV_AVAILABLE:
        raise RuntimeError("evdev library is not available.")

    all_keys = list(set(list(code for code, _ in _CHAR_MAP.values()) + list(_SPECIAL_KEYS.values())))
    all_keys.extend([ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE])

    cap = {
        ecodes.EV_KEY: all_keys,
        ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
    }

    return UInput(cap, name="Jarvis-Virtual-Input")


@ToolRegistry.register(_TOOL_NAME)
class GuiAutomationTool(BaseTool):
    """Tool to control keyboard and mouse natively on Linux/Wayland."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Simulate native keyboard, mouse and desktop app interaction on Linux/Wayland. Actions: "
                "'open_app' (launches or brings any desktop application to the front, e.g. 'calculadora', 'dolphin', 'chrome', 'zapzap', 'alacritty', 'antigravity-ide'; takes 'app'), "
                "'ide_open_project' (opens a project folder/directory in Antigravity IDE and brings it to front; takes 'path', e.g. 'openjarvis', 'projects', '~/Projects/app'), "
                "'ide_open_file' (opens a file at optional line in Antigravity IDE and brings it to front; takes 'path', optional 'line'), "
                "'ide_chat' (brings Antigravity IDE to front and submits an AI prompt to its inline agent; takes 'prompt' or 'text'), "
                "'whatsapp_open_chat' (ONLY opens and switches to the specified contact's chat in WhatsApp/ZapZap without sending any message; takes 'contact'), "
                "'send_desktop_message' (brings app like 'zapzap' to front, optionally switches to 'contact' first, and types text + Enter to send it; takes 'text', optional 'contact'), "
                "'focus_app' (brings app to foreground or launches it using 'app', e.g. 'zapzap', 'chrome', 'antigravity', 'telegram', 'calculadora'), "
                "'type_text' (types literal string into active window, optional 'press_enter': true), "
                "'hotkey' (presses key combination, e.g. 'ctrl+c', 'alt+tab', 'super', 'enter', 'ctrl+alt+t'), "
                "'click' (clicks mouse button: 'left', 'right', 'middle', 'double'), "
                "'move_mouse' (moves mouse relative by 'dx' and 'dy'), "
                "'scroll' (scrolls mouse wheel by 'steps', positive=up, negative=down)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open_app", "ide_open_project", "ide_open_file", "ide_chat", "whatsapp_open_chat", "send_desktop_message", "focus_app", "type_text", "hotkey", "click", "move_mouse", "scroll"],
                        "description": "Action to perform.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Project directory path or file path for 'ide_open_project' or 'ide_open_file'.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number for 'ide_open_file'.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Instruction/prompt to send to the IDE AI assistant for 'ide_chat'.",
                    },
                    "contact": {
                        "type": "string",
                        "description": "Contact or group name to open or send message to in WhatsApp/ZapZap (e.g. 'Andres Rodriguez', 'My Blood Flower').",
                    },
                    "app": {
                        "type": "string",
                        "description": "Application name for 'focus_app' or 'send_desktop_message' (e.g. 'zapzap', 'whatsapp', 'chrome', 'antigravity').",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type for 'type_text' or message for 'send_desktop_message'.",
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "Whether to press Enter after typing text.",
                        "default": False,
                    },
                    "keys": {
                        "type": "string",
                        "description": "Key or combination to press for 'hotkey' (e.g. 'ctrl+c', 'alt+tab', 'super', 'f5', 'enter', 'volume_up').",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle", "double"],
                        "description": "Mouse button for 'click'.",
                        "default": "left",
                    },
                    "dx": {
                        "type": "integer",
                        "description": "Horizontal relative pixels to move mouse.",
                        "default": 0,
                    },
                    "dy": {
                        "type": "integer",
                        "description": "Vertical relative pixels to move mouse.",
                        "default": 0,
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Scroll wheel steps (positive for up, negative for down).",
                        "default": 3,
                    },
                },
                "required": ["action"],
            },
            category="system",
            requires_confirmation=False,
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        if not _EVDEV_AVAILABLE:
            return ToolResult(tool_name=_TOOL_NAME, content="evdev no está instalado en el entorno.", success=False)

        action = params.get("action", "")
        if not action:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta parámetro 'action'.", success=False)

        try:
            if action == "open_app":
                return self._open_app(params)
            elif action == "ide_open_project":
                return self._ide_open_project(params)
            elif action == "ide_open_file":
                return self._ide_open_file(params)
            elif action == "ide_chat":
                return self._ide_chat(params)
            elif action == "whatsapp_open_chat":
                return self._whatsapp_open_chat(params)
            elif action == "send_desktop_message":
                return self._send_desktop_message(params)
            elif action == "focus_app":
                return self._focus_app(params)
            elif action == "type_text":
                return self._type_text(params)
            elif action == "hotkey":
                return self._hotkey(params)
            elif action == "click":
                return self._click(params)
            elif action == "move_mouse":
                return self._move_mouse(params)
            elif action == "scroll":
                return self._scroll(params)
            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Acción desconocida: {action}", success=False)
        except Exception as exc:
            logger.exception("GUI automation error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error en automatización GUI: {exc}", success=False)

    def _ide_open_project(self, params: Dict[str, Any]) -> ToolResult:
        p = (params.get("path") or params.get("text") or "").strip()
        if not p:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta el parámetro 'path' con el nombre o ruta del proyecto.", success=False)

        target: Optional[Path] = None
        plow = p.lower()
        if plow in ("openjarvis", "jarvis"):
            target = Path("/home/microsystem/.openjarvis/src")
        elif plow in ("projects", "proyectos"):
            target = Path("/home/microsystem/Projects")
        else:
            exp = Path(os.path.expanduser(p))
            if exp.exists():
                target = exp
            else:
                cand1 = Path("/home/microsystem/Projects") / p
                cand2 = Path("/home/microsystem") / p
                if cand1.exists():
                    target = cand1
                elif cand2.exists():
                    target = cand2

        if not target or not target.exists():
            return ToolResult(tool_name=_TOOL_NAME, content=f"No se encontró el directorio del proyecto para '{p}'.", success=False)

        try:
            subprocess.run(["antigravity-ide", "-r", str(target)], capture_output=True, timeout=8)
            time.sleep(0.4)
            _focus_desktop_app("antigravity")
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"💻 Proyecto '{target.name}' ({target}) abierto en Antigravity IDE y ventana traída al frente.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error abriendo proyecto en Antigravity IDE: {exc}", success=False)

    def _ide_open_file(self, params: Dict[str, Any]) -> ToolResult:
        fpath = (params.get("path") or params.get("file") or params.get("text") or "").strip()
        if not fpath:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta el parámetro 'path' con la ruta del archivo.", success=False)

        line = params.get("line")
        exp = Path(os.path.expanduser(fpath))
        if not exp.exists():
            cand = Path("/home/microsystem/.openjarvis/src") / fpath
            if cand.exists():
                exp = cand

        arg = f"{exp}:{line}" if line else str(exp)
        try:
            subprocess.run(["antigravity-ide", "-r", "-g", arg], capture_output=True, timeout=8)
            time.sleep(0.4)
            _focus_desktop_app("antigravity")
            line_str = f" en la línea {line}" if line else ""
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"📄 Archivo '{exp.name}' abierto{line_str} en Antigravity IDE y ventana traída al frente.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(tool_name=_TOOL_NAME, content=f"Error abriendo archivo en Antigravity IDE: {exc}", success=False)

    def _ide_chat(self, params: Dict[str, Any]) -> ToolResult:
        prompt = (params.get("prompt") or params.get("text") or params.get("message") or "").strip()
        if not prompt:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta el parámetro 'prompt' con la instrucción para la IA.", success=False)

        _focus_desktop_app("antigravity")
        time.sleep(0.4)

        # Trigger inline code/agent prompt with Ctrl + L
        hk_res = self._hotkey({"keys": "ctrl+l"})
        if not hk_res.success:
            return hk_res
        time.sleep(0.35)

        type_res = self._type_text({"text": prompt, "press_enter": True})
        if type_res.success:
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=(
                    f"🤖 Pregunta enviada a Antigravity IDE: \"{prompt}\".\n"
                    "⚠️ REGLA DE VERACIDAD CRÍTICA:\n"
                    "- La instrucción apenas se acaba de ingresar en la IDE por teclado virtual.\n"
                    "- Antigravity está procesando la solicitud y pensando/escribiendo la respuesta en tiempo real en la pantalla.\n"
                    "- TÚ NO CONOCES LA RESPUESTA DE ANTIGRAVITY TODAVÍA (tarda tiempo en generar su respuesta en la IDE).\n"
                    "- ESTÁ TERMINANTEMENTE PROHIBIDO inventar, asumir, resumir o simular lo que Antigravity respondió o va a responder.\n"
                    "- Dile a Elías con total honestidad que la pregunta ya fue enviada a Antigravity en la IDE y pídele que espere un momento a que Antigravity termine de redactar su respuesta en pantalla."
                ),
                success=True,
            )
        return type_res

    def _whatsapp_open_chat(self, params: Dict[str, Any]) -> ToolResult:
        contact = (params.get("contact", "") or params.get("text", "") or params.get("name", "")).strip()
        if not contact:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta el parámetro 'contact' con el nombre del contacto o grupo.", success=False)

        _focus_desktop_app("zapzap")

        char_lookup = {
            "a": ecodes.KEY_A, "b": ecodes.KEY_B, "c": ecodes.KEY_C, "d": ecodes.KEY_D,
            "e": ecodes.KEY_E, "f": ecodes.KEY_F, "g": ecodes.KEY_G, "h": ecodes.KEY_H,
            "i": ecodes.KEY_I, "j": ecodes.KEY_J, "k": ecodes.KEY_K, "l": ecodes.KEY_L,
            "m": ecodes.KEY_M, "n": ecodes.KEY_N, "o": ecodes.KEY_O, "p": ecodes.KEY_P,
            "q": ecodes.KEY_Q, "r": ecodes.KEY_R, "s": ecodes.KEY_S, "t": ecodes.KEY_T,
            "u": ecodes.KEY_U, "v": ecodes.KEY_V, "w": ecodes.KEY_W, "x": ecodes.KEY_X,
            "y": ecodes.KEY_Y, "z": ecodes.KEY_Z, " ": ecodes.KEY_SPACE,
            "0": ecodes.KEY_0, "1": ecodes.KEY_1, "2": ecodes.KEY_2, "3": ecodes.KEY_3,
            "4": ecodes.KEY_4, "5": ecodes.KEY_5, "6": ecodes.KEY_6, "7": ecodes.KEY_7,
            "8": ecodes.KEY_8, "9": ecodes.KEY_9,
        }

        all_keys = list(char_lookup.values()) + [ecodes.KEY_LEFTALT, ecodes.KEY_BACKSPACE, ecodes.KEY_ESC, ecodes.KEY_ENTER]
        ui = UInput({ecodes.EV_KEY: all_keys}, name="Jarvis-WA-Nav")
        time.sleep(0.08)
        try:
            # 1. Clear any active chat / modal focus
            ui.write(ecodes.EV_KEY, ecodes.KEY_ESC, 1)
            ui.syn()
            time.sleep(0.02)
            ui.write(ecodes.EV_KEY, ecodes.KEY_ESC, 0)
            ui.syn()
            time.sleep(0.2)

            # 2. Trigger Alt + K (WhatsApp Web Command Palette / Quick Switcher)
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 1)
            ui.syn()
            time.sleep(0.02)
            ui.write(ecodes.EV_KEY, ecodes.KEY_K, 1)
            ui.syn()
            time.sleep(0.04)
            ui.write(ecodes.EV_KEY, ecodes.KEY_K, 0)
            ui.syn()
            time.sleep(0.02)
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 0)
            ui.syn()
            time.sleep(0.35)

            # 3. Backspace to clear accidental 'k' in search box
            ui.write(ecodes.EV_KEY, ecodes.KEY_BACKSPACE, 1)
            ui.syn()
            time.sleep(0.02)
            ui.write(ecodes.EV_KEY, ecodes.KEY_BACKSPACE, 0)
            ui.syn()
            time.sleep(0.05)

            # 4. Type normalized contact name into search input
            normalized = unicodedata.normalize("NFKD", contact).encode("ASCII", "ignore").decode("ASCII").lower()
            for ch in normalized:
                if ch in char_lookup:
                    kc = char_lookup[ch]
                    ui.write(ecodes.EV_KEY, kc, 1)
                    ui.syn()
                    time.sleep(0.02)
                    ui.write(ecodes.EV_KEY, kc, 0)
                    ui.syn()
                    time.sleep(0.02)

            time.sleep(0.5)

            # 5. Enter to open first matching result
            ui.write(ecodes.EV_KEY, ecodes.KEY_ENTER, 1)
            ui.syn()
            time.sleep(0.04)
            ui.write(ecodes.EV_KEY, ecodes.KEY_ENTER, 0)
            ui.syn()
            time.sleep(0.5)

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"💬 Chat de '{contact}' abierto con éxito en ZapZap (WhatsApp). No se ha enviado ningún mensaje.",
                success=True,
            )
        finally:
            ui.close()

    def _open_app(self, params: Dict[str, Any]) -> ToolResult:
        app = (params.get("app") or params.get("text") or params.get("name") or "").strip()
        if not app:
            return ToolResult(tool_name=_TOOL_NAME, content="Falta el parámetro 'app' con el nombre de la aplicación.", success=False)

        # 1. If window is already open, raise it to foreground
        if _focus_desktop_app(app):
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"🖥️ La aplicación '{app}' ya estaba abierta y se trajo al primer plano.",
                success=True,
            )

        # 2. Find desktop entry and launch via gtk-launch
        stem, dpath = find_desktop_file(app)
        if stem:
            try:
                subprocess.Popen(["gtk-launch", stem], start_new_session=True)
                time.sleep(1.2)
                _focus_desktop_app(stem)
                return ToolResult(
                    tool_name=_TOOL_NAME,
                    content=f"🚀 Aplicación '{app}' ({stem}) iniciada con éxito y traída al primer plano.",
                    success=True,
                )
            except Exception as exc:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error iniciando aplicación '{app}': {exc}", success=False)

        # 3. Fallback: check if binary is in PATH
        bin_path = shutil.which(app)
        if bin_path:
            try:
                subprocess.Popen([bin_path], start_new_session=True)
                time.sleep(1.2)
                _focus_desktop_app(app)
                return ToolResult(
                    tool_name=_TOOL_NAME,
                    content=f"🚀 Aplicación '{app}' ({bin_path}) iniciada con éxito y traída al primer plano.",
                    success=True,
                )
            except Exception as exc:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Error ejecutando '{bin_path}': {exc}", success=False)

        return ToolResult(
            tool_name=_TOOL_NAME,
            content=f"❌ No se encontró la aplicación '{app}' en el sistema ni en los accesos de escritorio.",
            success=False,
        )

    def _focus_app(self, params: Dict[str, Any]) -> ToolResult:
        app = (params.get("app") or params.get("text") or params.get("name") or "").strip()
        if not app:
            return ToolResult(tool_name=_TOOL_NAME, content="'app' es requerido para focus_app.", success=False)

        ok = _focus_desktop_app(app)
        if ok:
            return ToolResult(tool_name=_TOOL_NAME, content=f"🖥️ Aplicación '{app}' traída al frente.", success=True)

        # Fallback: if not running, launch it
        return self._open_app(params)

    def _send_desktop_message(self, params: Dict[str, Any]) -> ToolResult:
        app = params.get("app", "zapzap").strip() or "zapzap"
        text = params.get("text", "").strip()
        contact = params.get("contact", "").strip()

        if not text:
            return ToolResult(tool_name=_TOOL_NAME, content="'text' es requerido para enviar mensaje.", success=False)

        # If a specific contact was targeted in WhatsApp, open their chat first
        if contact and app in ("zapzap", "whatsapp"):
            open_res = self._whatsapp_open_chat({"contact": contact})
            if not open_res.success:
                return open_res
            time.sleep(0.4)
        else:
            _focus_desktop_app(app)
            time.sleep(0.5)

        type_res = self._type_text({"text": text, "press_enter": True})
        if type_res.success:
            dest = f"a '{contact}' en {app}" if contact else f"en {app} al chat activo"
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f'💬 Mensaje enviado {dest}: "{text}"',
                success=True,
            )
        return type_res

    def _type_text(self, params: Dict[str, Any]) -> ToolResult:
        text = params.get("text", "")
        if not text:
            return ToolResult(tool_name=_TOOL_NAME, content="'text' es requerido para escribir.", success=False)

        press_enter = bool(params.get("press_enter", False))

        ui = _create_virtual_device()
        time.sleep(0.05)
        try:
            for char in text:
                target_char = char
                if target_char not in _CHAR_MAP:
                    # Normalize accented characters: á -> a, ñ -> n, etc.
                    target_char = unicodedata.normalize("NFKD", char).encode("ASCII", "ignore").decode("ASCII")

                if target_char in _CHAR_MAP:
                    code, need_shift = _CHAR_MAP[target_char]
                    if need_shift:
                        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                        ui.syn()
                    ui.write(ecodes.EV_KEY, code, 1)
                    ui.syn()
                    time.sleep(0.015)
                    ui.write(ecodes.EV_KEY, code, 0)
                    ui.syn()
                    if need_shift:
                        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                        ui.syn()
                    time.sleep(0.01)

            if press_enter:
                time.sleep(0.05)
                ui.write(ecodes.EV_KEY, ecodes.KEY_ENTER, 1)
                ui.syn()
                time.sleep(0.03)
                ui.write(ecodes.EV_KEY, ecodes.KEY_ENTER, 0)
                ui.syn()

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"⌨️ Texto escrito en la ventana activa: '{text}'" + (" (+Enter)" if press_enter else ""),
                success=True,
            )
        finally:
            ui.close()

    def _hotkey(self, params: Dict[str, Any]) -> ToolResult:
        keys_str = params.get("keys", "").strip().lower()
        if not keys_str:
            return ToolResult(tool_name=_TOOL_NAME, content="'keys' es requerido para hotkey.", success=False)

        parts = [k.strip() for k in keys_str.split("+") if k.strip()]
        keycodes: List[int] = []

        for p in parts:
            if p in _SPECIAL_KEYS:
                keycodes.append(_SPECIAL_KEYS[p])
            elif p in _CHAR_MAP:
                keycodes.append(_CHAR_MAP[p][0])
            else:
                # Try finding in ecodes directly
                attr = f"KEY_{p.upper()}"
                if hasattr(ecodes, attr):
                    keycodes.append(getattr(ecodes, attr))
                else:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Tecla desconocida: '{p}'", success=False)

        ui = _create_virtual_device()
        time.sleep(0.05)
        try:
            # Press all down in order
            for kc in keycodes:
                ui.write(ecodes.EV_KEY, kc, 1)
                ui.syn()
                time.sleep(0.01)

            time.sleep(0.05)

            # Release all in reverse order
            for kc in reversed(keycodes):
                ui.write(ecodes.EV_KEY, kc, 0)
                ui.syn()
                time.sleep(0.01)

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"⌨️ Atajo de teclado ejecutado: '{keys_str}'",
                success=True,
            )
        finally:
            ui.close()

    def _click(self, params: Dict[str, Any]) -> ToolResult:
        btn_type = params.get("button", "left").lower()
        btn_code = ecodes.BTN_LEFT
        if btn_type == "right":
            btn_code = ecodes.BTN_RIGHT
        elif btn_type == "middle":
            btn_code = ecodes.BTN_MIDDLE

        ui = _create_virtual_device()
        time.sleep(0.05)
        try:
            clicks = 2 if btn_type == "double" else 1
            for _ in range(clicks):
                ui.write(ecodes.EV_KEY, btn_code, 1)
                ui.syn()
                time.sleep(0.04)
                ui.write(ecodes.EV_KEY, btn_code, 0)
                ui.syn()
                time.sleep(0.05)

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"🖱️ Clic ejecutado: botón '{btn_type}'",
                success=True,
            )
        finally:
            ui.close()

    def _move_mouse(self, params: Dict[str, Any]) -> ToolResult:
        dx = int(params.get("dx", 0))
        dy = int(params.get("dy", 0))

        ui = _create_virtual_device()
        time.sleep(0.05)
        try:
            ui.write(ecodes.EV_REL, ecodes.REL_X, dx)
            ui.write(ecodes.EV_REL, ecodes.REL_Y, dy)
            ui.syn()

            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"🖱️ Cursor movido ({dx}px, {dy}px)",
                success=True,
            )
        finally:
            ui.close()

    def _scroll(self, params: Dict[str, Any]) -> ToolResult:
        steps = int(params.get("steps", 3))

        ui = _create_virtual_device()
        time.sleep(0.05)
        try:
            ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, steps)
            ui.syn()

            direction = "arriba" if steps > 0 else "abajo"
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"🖱️ Desplazamiento de rueda ({abs(steps)} pasos hacia {direction})",
                success=True,
            )
        finally:
            ui.close()


__all__ = ["GuiAutomationTool"]
