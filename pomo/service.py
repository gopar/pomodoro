"""Service management for pomo agent and server (macOS launchd, Linux systemd).

Generates and deploys service files so ``pomo-agent`` / ``pomo-server`` run in
the background. The binary must already be on ``PATH`` (installed via
``pip install`` / ``uv tool install``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _binary(server: bool) -> str:
    name = "pomo-server" if server else "pomo-agent"
    path = shutil.which(name)
    if not path:
        print(f"Error: {name} not found on PATH.")
        print("Install the package first (pip install -e . or uv tool install).")
        sys.exit(1)
    return path


def _pomo_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k.startswith(("POMO_SERVER_URL", "POMO_TOKEN",
                             "POMO_PORT", "POMO_HOST", "POMO_DB_PATH"))}


# ---------------------------------------------------------------------------
# macOS — launchd
# ---------------------------------------------------------------------------

def _macos_label(server: bool) -> str:
    return "pomo.server" if server else "pomo.agent"


def _macos_plist(server: bool) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_macos_label(server)}.plist"


def _macos_log(server: bool) -> Path:
    log_dir = Path.home() / ".local" / "state" / "pomo"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / ("server.log" if server else "agent.log")


def _macos_plist_content(server: bool) -> str:
    binary = _binary(server)
    label = _macos_label(server)
    log = str(_macos_log(server))
    which = "server" if server else "agent"
    env_xml = ""
    for key, val in sorted(_pomo_env().items()):
        env_xml += f"        <key>{key}</key>\n"
        env_xml += f"        <string>{val}</string>\n"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}    </dict>
</dict>
</plist>"""


def _macos_install(server: bool) -> None:
    plist = _macos_plist(server)
    label = _macos_label(server)
    plist.parent.mkdir(parents=True, exist_ok=True)

    running = _macos_check_running(server)
    if running:
        print(f"{label}: already running.")
        return

    plist.write_text(_macos_plist_content(server), encoding="utf-8")
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        check=False,
    )
    print(f"{label}: installed and started.")
    print(f"  logs: ~/.local/state/pomo/{'server' if server else 'agent'}.log")


def _macos_uninstall(server: bool) -> None:
    plist = _macos_plist(server)
    label = _macos_label(server)
    if plist.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            check=False,
        )
        plist.unlink(missing_ok=True)
        print(f"{label}: stopped and removed.")
    else:
        print(f"{label}: not installed.")


def _macos_check_running(server: bool) -> bool:
    label = _macos_label(server)
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if label in line and "PID" not in line:
            parts = line.split()
            if len(parts) >= 2 and parts[2] == label:
                return parts[0] != "-"
    return False


def _macos_status(server: bool) -> None:
    label = _macos_label(server)
    plist = _macos_plist(server)
    if not plist.exists():
        print(f"{label}: not installed.")
        return
    if _macos_check_running(server):
        print(f"{label}: running.")
    else:
        print(f"{label}: installed but not running.")


def _macos_logs(server: bool) -> None:
    log = _macos_log(server)
    if not log.exists():
        print(f"No log file yet ({log}).")
        return
    subprocess.run(["tail", "-f", str(log)], check=False)


# ---------------------------------------------------------------------------
# Linux — systemd user
# ---------------------------------------------------------------------------

def _linux_service_name(server: bool) -> str:
    return "pomo-server" if server else "pomo-agent"


def _linux_service_path(server: bool) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{_linux_service_name(server)}.service"


def _linux_service_content(server: bool) -> str:
    binary = _binary(server)
    which = "server" if server else "agent"
    env_lines = ""
    for key, val in sorted(_pomo_env().items()):
        env_lines += f"Environment={key}={val}\n"
    return f"""[Unit]
Description=Pomodoro {'sync server' if server else 'local agent'}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary}
Restart=on-failure
RestartSec=5
{env_lines}
[Install]
WantedBy=default.target
"""


def _linux_install(server: bool) -> None:
    name = _linux_service_name(server)
    svc_path = _linux_service_path(server)
    svc_path.parent.mkdir(parents=True, exist_ok=True)

    running = _linux_check_running(server)
    if running:
        print(f"{name}: already running.")
        return

    svc_path.write_text(_linux_service_content(server), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", name], check=False,
    )
    if result.returncode == 0:
        print(f"{name}: installed and started.")
        print(f"  logs: journalctl --user -u {name} -f")
    else:
        print(f"{name}: install failed. Check systemctl --user status {name}.")


def _linux_uninstall(server: bool) -> None:
    name = _linux_service_name(server)
    svc_path = _linux_service_path(server)
    if svc_path.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", name],
                       check=False)
        svc_path.unlink(missing_ok=True)
        print(f"{name}: stopped and removed.")
    else:
        print(f"{name}: not installed.")


def _linux_check_running(server: bool) -> bool:
    name = _linux_service_name(server)
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _linux_status(server: bool) -> None:
    name = _linux_service_name(server)
    svc_path = _linux_service_path(server)
    if not svc_path.exists():
        print(f"{name}: not installed.")
        return
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name],
        capture_output=True, text=True,
    )
    status = result.stdout.strip()
    print(f"{name}: {status}.")


def _linux_logs(server: bool) -> None:
    name = _linux_service_name(server)
    subprocess.run(["journalctl", "--user", "-u", name, "-f"], check=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "linux":
        return "linux"
    print(f"Error: unsupported platform ({sys.platform}). macOS and Linux only.")
    sys.exit(1)


def install(server: bool = False) -> None:
    _binary(server)  # verify binary exists
    if _platform() == "macos":
        _macos_install(server)
    else:
        _linux_install(server)


def uninstall(server: bool = False) -> None:
    if _platform() == "macos":
        _macos_uninstall(server)
    else:
        _linux_uninstall(server)


def status(server: bool = False) -> None:
    if _platform() == "macos":
        _macos_status(server)
    else:
        _linux_status(server)


def logs(server: bool = False) -> None:
    _binary(server)  # verify binary exists
    if _platform() == "macos":
        _macos_logs(server)
    else:
        _linux_logs(server)
