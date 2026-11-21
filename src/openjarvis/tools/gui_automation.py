"""Native Linux/Wayland GUI automation tool for OpenJarvis via evdev/uinput."""

from __future__ import annotations

import logging
import subprocess
import time
import unicodedata
from typing import Any, Dict, List

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


def _focus_desktop_app(app_name: str) -> bool:
    """Bring specified application window to the foreground under KDE/Wayland."""
    name = app_name.lower().strip()
    desktop_map = {
        "zapzap": "com.rtosta.zapzap",
        "whatsapp": "com.rtosta.zapzap",
        "chrome": "google-chrome",
        "browser": "google-chrome",
        "firefox": "firefox",
        "telegram": "org.telegram.desktop",
        "code": "antigravity",
        "ide": "antigravity",
    }
    target = desktop_map.get(name, name)
    try:
        res = subprocess.run(["gtk-launch", target], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception as exc:
        logger.warning("focus_app failed: %s", exc)
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
                "'send_desktop_message' (brings app like 'zapzap'/'whatsapp' to front and types text + Enter to send it), "
                "'focus_app' (brings app to foreground using 'app', e.g. 'zapzap', 'chrome', 'telegram'), "
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
                        "enum": ["send_desktop_message", "focus_app", "type_text", "hotkey", "click", "move_mouse", "scroll"],
                        "description": "Action to perform.",
                    },
                    "app": {
                        "type": "string",
                        "description": "Application name for 'focus_app' or 'send_desktop_message' (e.g. 'zapzap', 'whatsapp', 'chrome').",
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
            if action == "send_desktop_message":
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

    def _focus_app(self, params: Dict[str, Any]) -> ToolResult:
        app = params.get("app", "").strip()
        if not app:
            return ToolResult(tool_name=_TOOL_NAME, content="'app' es requerido para focus_app.", success=False)

        ok = _focus_desktop_app(app)
        if ok:
            return ToolResult(tool_name=_TOOL_NAME, content=f"🖥️ Aplicación '{app}' traída al frente.", success=True)
        return ToolResult(tool_name=_TOOL_NAME, content=f"No se pudo enfocar la aplicación '{app}'.", success=False)

    def _send_desktop_message(self, params: Dict[str, Any]) -> ToolResult:
        app = params.get("app", "zapzap").strip() or "zapzap"
        text = params.get("text", "").strip()
        if not text:
            return ToolResult(tool_name=_TOOL_NAME, content="'text' es requerido para enviar mensaje.", success=False)

        _focus_desktop_app(app)
        time.sleep(0.6)  # Allow window manager to switch focus

        type_res = self._type_text({"text": text, "press_enter": True})
        if type_res.success:
            return ToolResult(
                tool_name=_TOOL_NAME,
                content=f"💬 Mensaje enviado en {app} al chat activo: \"{text}\"",
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
