"""Hardware and system sentinel tool with background telemetry alerting for CachyOS."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psutil

from openjarvis.connectors.telegram_notify import send_telegram_notification
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_TOOL_NAME = "system_status"

# Debounce tracker: alert_type -> timestamp of last sent alert
_alert_debounce: Dict[str, float] = {}
_consecutive_high_cpu = 0
_sentinel_thread: Optional[threading.Thread] = None
_sentinel_lock = threading.Lock()


def _format_bytes(b: int) -> str:
    """Format bytes into human-readable GB/MB."""
    return f"{b / (1024 ** 3):.1f} GB"


def get_system_snapshot() -> Dict[str, Any]:
    """Capture full hardware and operating system metrics."""
    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count(logical=True)
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)

    # Memory
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk
    disk_root = shutil.disk_usage("/")
    disk_home = shutil.disk_usage("/home/microsystem") if os.path.exists("/home/microsystem") else disk_root

    # Battery
    battery = psutil.sensors_battery()
    batt_info: Dict[str, Any] = {}
    if battery:
        batt_info = {
            "percent": battery.percent,
            "power_plugged": battery.power_plugged,
            "secsleft": battery.secsleft,
        }

    # Top processes
    top_procs = []
    try:
        procs = sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
            key=lambda p: (p.info.get("cpu_percent") or 0),
            reverse=True,
        )[:5]
        for p in procs:
            top_procs.append({
                "pid": p.info.get("pid"),
                "name": p.info.get("name"),
                "cpu": p.info.get("cpu_percent", 0),
                "ram": round(p.info.get("memory_percent", 0), 1),
            })
    except Exception:
        pass

    # Uptime
    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    uptime_hours = (datetime.now(timezone.utc) - boot_time).total_seconds() / 3600.0

    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "node": platform.node(),
        "uptime_hours": round(uptime_hours, 1),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_count,
            "load_1_5_15": [round(x, 2) for x in load_avg],
        },
        "memory": {
            "total_gb": round(mem.total / (1024 ** 3), 1),
            "used_gb": round(mem.used / (1024 ** 3), 1),
            "free_gb": round(mem.available / (1024 ** 3), 1),
            "percent": mem.percent,
        },
        "swap": {
            "total_gb": round(swap.total / (1024 ** 3), 1),
            "used_gb": round(swap.used / (1024 ** 3), 1),
            "percent": swap.percent,
        },
        "disk": {
            "root_used": _format_bytes(disk_root.used),
            "root_total": _format_bytes(disk_root.total),
            "root_percent": round(disk_root.used / disk_root.total * 100, 1),
            "home_used": _format_bytes(disk_home.used),
            "home_total": _format_bytes(disk_home.total),
            "home_percent": round(disk_home.used / disk_home.total * 100, 1),
        },
        "battery": batt_info,
        "top_processes": top_procs,
    }


def check_threshold_alerts(snapshot: Optional[Dict[str, Any]] = None) -> List[str]:
    """Check if any telemetry threshold is currently breached."""
    snap = snapshot or get_system_snapshot()
    alerts = []

    # Disk alert: root or home > 85%
    disk = snap.get("disk", {})
    if disk.get("root_percent", 0) > 85:
        alerts.append(f"Espacio en disco raíz (/) crítico: {disk['root_percent']}% utilizado ({disk['root_used']} / {disk['root_total']})")
    if disk.get("home_percent", 0) > 85:
        alerts.append(f"Espacio en /home crítico: {disk['home_percent']}% utilizado ({disk['home_used']} / {disk['home_total']})")

    # RAM alert: > 92%
    mem = snap.get("memory", {})
    if mem.get("percent", 0) > 92:
        alerts.append(f"Consumo de memoria RAM crítico: {mem['percent']}% ({mem['used_gb']} GB / {mem['total_gb']} GB)")

    # Battery alert: < 20% and discharging
    batt = snap.get("battery", {})
    if batt and not batt.get("power_plugged", True) and batt.get("percent", 100) < 20:
        alerts.append(f"Batería baja ({batt['percent']}%). Desconectado de la corriente.")

    return alerts


def _sentinel_background_loop() -> None:
    """Sentinel polling loop checking thresholds every 60 seconds."""
    global _consecutive_high_cpu
    logger.info("Sentinel background monitoring loop started.")

    while True:
        try:
            snap = get_system_snapshot()
            now = time.time()

            # 1. Disk Alert (cooldown: 30 minutes)
            disk = snap.get("disk", {})
            if disk.get("root_percent", 0) > 85 or disk.get("home_percent", 0) > 85:
                last_sent = _alert_debounce.get("disk", 0)
                if now - last_sent > 1800:
                    _alert_debounce["disk"] = now
                    msg = (
                        "🚨 *Jarvis Sentinel: Alerta de Almacenamiento NVMe*\n\n"
                        f"• Raíz (/): `{disk['root_percent']}%` ({disk['root_used']} / {disk['root_total']})\n"
                        f"• Home: `{disk['home_percent']}%` ({disk['home_used']} / {disk['home_total']})\n\n"
                        "_Te sugiero limpiar paquetes huérfanos o archivos temporales._"
                    )
                    send_telegram_notification(msg)

            # 2. Battery Alert (cooldown: 15 minutes)
            batt = snap.get("battery", {})
            if batt and not batt.get("power_plugged", True) and batt.get("percent", 100) <= 20:
                last_sent = _alert_debounce.get("battery", 0)
                if now - last_sent > 900:
                    _alert_debounce["battery"] = now
                    msg = (
                        f"🔋 *Jarvis Sentinel: Batería Crítica ({batt['percent']}%)*\n\n"
                        "El equipo está funcionando con batería baja y no está conectado a la corriente."
                    )
                    send_telegram_notification(msg)

            # 3. CPU runaway (3 consecutive checks > 92%, cooldown: 20 minutes)
            cpu = snap.get("cpu", {})
            if cpu.get("percent", 0) > 92:
                _consecutive_high_cpu += 1
                if _consecutive_high_cpu >= 3:
                    last_sent = _alert_debounce.get("cpu_runaway", 0)
                    if now - last_sent > 1200:
                        _alert_debounce["cpu_runaway"] = now
                        top_p = snap.get("top_processes", [])
                        proc_lines = [f"`{p['name']}` (PID {p['pid']}): {p['cpu']}% CPU" for p in top_p[:3]]
                        msg = (
                            "🔥 *Jarvis Sentinel: CPU en Carga Extrema Sostenida*\n\n"
                            f"La CPU supera el 92% por más de 3 minutos consecutivos.\n\n"
                            "*Procesos principales:*\n" + "\n".join(proc_lines)
                        )
                        send_telegram_notification(msg)
            else:
                _consecutive_high_cpu = 0

        except Exception as exc:
            logger.debug("Sentinel loop check error: %s", exc)

        time.sleep(60)


def ensure_sentinel_started() -> None:
    """Ensure background sentinel thread is running exactly once."""
    global _sentinel_thread
    with _sentinel_lock:
        if _sentinel_thread is None or not _sentinel_thread.is_alive():
            _sentinel_thread = threading.Thread(
                target=_sentinel_background_loop,
                daemon=True,
                name="jarvis-system-sentinel",
            )
            _sentinel_thread.start()


# Automatically start sentinel monitor on module load
ensure_sentinel_started()


@ToolRegistry.register(_TOOL_NAME)
class SystemStatusTool(BaseTool):
    """Tool to inspect hardware, performance, alerts and systemd services."""

    tool_id = _TOOL_NAME
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Inspect laptop hardware health, resources, systemd services, and power/session controls on CachyOS. "
                "Actions: 'get_status' for full CPU, RAM, NVMe Disk, Battery and top processes metrics; "
                "'check_alerts' to check if any threshold is exceeded; "
                "'service_status' to inspect a systemd service; "
                "'service_logs' to read recent journalctl logs for a service; "
                "'lock_screen' to lock the desktop screen (loginctl lock-session); "
                "'unlock_screen' to unlock the desktop screen (loginctl unlock-session); "
                "'suspend' to suspend/sleep the laptop (systemctl suspend); "
                "'reboot' to reboot the computer (systemctl reboot); "
                "'poweroff' or 'shutdown' to turn off the computer (systemctl poweroff)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "get_status",
                            "check_alerts",
                            "service_status",
                            "service_logs",
                            "lock_screen",
                            "unlock_screen",
                            "suspend",
                            "reboot",
                            "poweroff",
                            "shutdown",
                        ],
                        "description": "Action to perform.",
                    },
                    "service_name": {
                        "type": "string",
                        "description": "systemd service name for service_status or service_logs (e.g. 'docker', 'bluetooth').",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to retrieve (default 25).",
                        "default": 25,
                    },
                },
                "required": ["action"],
            },
            category="system",
            requires_confirmation=False,
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action") or params.get("operation") or params.get("command") or "get_status"
        action = str(action).lower().strip()

        try:
            if action == "get_status":
                snap = get_system_snapshot()
                cpu = snap["cpu"]
                mem = snap["memory"]
                disk = snap["disk"]
                batt = snap["battery"]
                procs = snap["top_processes"]

                batt_str = f"{batt.get('percent', 0)}% ({'Cargando / Conectado' if batt.get('power_plugged') else 'Batería'})" if batt else "No detectada (AC)"

                lines = [
                    f"🖥️ **Estado del Sistema ({snap['node']}):**",
                    f"• SO: {snap['os']} | Uptime: {snap['uptime_hours']} h",
                    f"• CPU: `{cpu['percent']}%` ({cpu['cores']} núcleos, Load: {cpu['load_1_5_15']})",
                    f"• RAM: `{mem['percent']}%` ({mem['used_gb']} GB usados / {mem['total_gb']} GB total, {mem['free_gb']} GB libre)",
                    f"• NVMe Raíz (/): `{disk['root_percent']}%` ({disk['root_used']} / {disk['root_total']})",
                    f"• NVMe Home (/home): `{disk['home_percent']}%` ({disk['home_used']} / {disk['home_total']})",
                    f"• Batería: {batt_str}",
                ]

                if procs:
                    lines.append("\n🔥 **Top Procesos:**")
                    for p in procs:
                        lines.append(f"  - `{p['name']}` (PID {p['pid']}): {p['cpu']}% CPU | {p['ram']}% RAM")

                return ToolResult(tool_name=_TOOL_NAME, content="\n".join(lines), success=True, metadata=snap)

            elif action == "check_alerts":
                alerts = check_threshold_alerts()
                if not alerts:
                    return ToolResult(tool_name=_TOOL_NAME, content="✅ Todos los parámetros del sistema están saludables (bajo los umbrales de alerta).", success=True)
                return ToolResult(tool_name=_TOOL_NAME, content="⚠️ **Alertas activas:**\n• " + "\n• ".join(alerts), success=True, metadata={"alerts": alerts})

            elif action == "service_status":
                svc = params.get("service_name", "").strip()
                if not svc:
                    return ToolResult(tool_name=_TOOL_NAME, content="'service_name' is required for service_status.", success=False)
                res = subprocess.run(["systemctl", "status", svc, "--no-pager"], capture_output=True, text=True, timeout=10)
                return ToolResult(tool_name=_TOOL_NAME, content=f"Estado del servicio '{svc}':\n\n```\n{res.stdout or res.stderr}\n```", success=True)

            elif action == "service_logs":
                svc = params.get("service_name", "").strip()
                lines_n = str(min(int(params.get("lines", 25)), 100))
                if not svc:
                    return ToolResult(tool_name=_TOOL_NAME, content="'service_name' is required for service_logs.", success=False)
                res = subprocess.run(["journalctl", "-u", svc, "-n", lines_n, "--no-pager"], capture_output=True, text=True, timeout=10)
                return ToolResult(tool_name=_TOOL_NAME, content=f"Logs de '{svc}' (últimas {lines_n} líneas):\n\n```\n{res.stdout or res.stderr}\n```", success=True)

            elif action in ("lock_screen", "lock", "bloquear", "bloquea"):
                try:
                    res = subprocess.run(["loginctl", "lock-session"], timeout=5)
                    if res.returncode == 0:
                        return ToolResult(tool_name=_TOOL_NAME, content="🔒 Pantalla bloqueada correctamente.", success=True)
                    return ToolResult(tool_name=_TOOL_NAME, content="loginctl lock-session falló.", success=False)
                except Exception as exc:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Error al bloquear pantalla: {exc}", success=False)

            elif action in ("unlock_screen", "unlock", "desbloquear", "desbloquea"):
                try:
                    res = subprocess.run(["loginctl", "unlock-session"], timeout=5)
                    if res.returncode == 0:
                        return ToolResult(tool_name=_TOOL_NAME, content="🔓 Pantalla desbloqueada correctamente.", success=True)
                    return ToolResult(tool_name=_TOOL_NAME, content="loginctl unlock-session falló.", success=False)
                except Exception as exc:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Error al desbloquear pantalla: {exc}", success=False)

            elif action in ("suspend", "suspender", "suspende", "dormir", "sleep"):
                try:
                    subprocess.Popen(["systemctl", "suspend"])
                    return ToolResult(tool_name=_TOOL_NAME, content="💤 Suspendiendo el equipo (sleep)...", success=True)
                except Exception as exc:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Error al suspender el equipo: {exc}", success=False)

            elif action in ("reboot", "reiniciar", "reinicia", "restart"):
                try:
                    subprocess.Popen(["systemctl", "reboot"])
                    return ToolResult(tool_name=_TOOL_NAME, content="🔄 Reiniciando el equipo...", success=True)
                except Exception as exc:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Error al reiniciar el equipo: {exc}", success=False)

            elif action in ("poweroff", "shutdown", "apagar", "apaga"):
                try:
                    subprocess.Popen(["systemctl", "poweroff"])
                    return ToolResult(tool_name=_TOOL_NAME, content="🛑 Apagando el equipo...", success=True)
                except Exception as exc:
                    return ToolResult(tool_name=_TOOL_NAME, content=f"Error al apagar el equipo: {exc}", success=False)

            else:
                return ToolResult(tool_name=_TOOL_NAME, content=f"Unknown action: {action}", success=False)

        except Exception as exc:
            logger.exception("System sentinel error")
            return ToolResult(tool_name=_TOOL_NAME, content=f"Sentinel error: {exc}", success=False)


__all__ = ["SystemStatusTool", "get_system_snapshot", "check_threshold_alerts"]
