#!/usr/bin/env python3
"""Install/remove LegionFan's privileged daemon. Never run by the widget."""
import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
LIB = Path("/usr/local/lib/omafans-legion")
CONFIG = Path("/etc/omafans-legion")
UNIT = Path("/etc/systemd/system/omafans-legion.service")
MODPROBE = Path("/etc/modprobe.d/omafans-legion.conf")
MODPROBE_LINE = "options lenovo_wmi_other expose_all_fans=1 relax_fan_constraint=1\n"
CONFLICTS = ("thinkfan", "fancontrol", "nbfc", "nbfc_service")


def systemctl(*args, check=True):
    return subprocess.run(
        ["/usr/bin/systemctl", *args], check=check, timeout=30,
        stdin=subprocess.DEVNULL, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"})


def safe_directory(path):
    for parent in reversed((path, *path.parents)):
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Installation directory is not root-owned and protected: {parent}")


def safe_destination(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            raise ValueError(f"Unsafe installation destination: {path}")


def atomic_write(path, data, mode):
    safe_directory(path.parent)
    safe_destination(path)
    fd, temporary = tempfile.mkstemp(prefix=".legionfan-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(user, dry_run):
    account = pwd.getpwnam(user)
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise ValueError("Choose an unprivileged desktop account")
    files = {
        LIB / "omafans_legion.py": ((SOURCE / "omafans_legion.py").read_bytes(), 0o644),
        CONFIG / "controller.json": (json.dumps({"uid": account.pw_uid, "gid": account.pw_gid}).encode() + b"\n", 0o600),
        UNIT: ((SOURCE / "system/omafans-legion.service").read_bytes(), 0o644),
        MODPROBE: (MODPROBE_LINE.encode(), 0o644),
    }
    for path in files:
        print(f"Install {path}")
    if dry_run:
        print("Dry run: no files or services changed")
        return
    for name in CONFLICTS:
        result = systemctl("is-active", "--quiet", name + ".service", check=False)
        if result.returncode == 0:
            raise ValueError(f"Another fan controller ({name}) is active; disable it first")
        if result.returncode not in (3, 4):
            raise ValueError("Unable to verify conflicting fan controllers")
    for path in files:
        safe_directory(path.parent)
        safe_destination(path)
    if MODPROBE.exists() and MODPROBE.read_bytes() != MODPROBE_LINE.encode():
        raise ValueError("Refusing to overwrite an unmanaged modprobe file")
    active = systemctl("is-active", "--quiet", "omafans-legion.service", check=False)
    if active.returncode == 0:
        systemctl("stop", "omafans-legion.service")
    elif active.returncode not in (3, 4):
        raise ValueError("Unable to inspect existing LegionFan service")
    for path, (data, mode) in files.items():
        atomic_write(path, data, mode)
    # The module needs a reload to pick up the modprobe options; do it when safe.
    print("Reloading lenovo_wmi_other to apply fan visibility ...")
    reload_result = systemctl("daemon-reload")
    systemctl("enable", "omafans-legion.service")
    print("Installed. Start with: sudo systemctl start omafans-legion.service")


def remove(dry_run):
    paths = (UNIT, CONFIG / "controller.json", LIB / "omafans_legion.py", MODPROBE)
    for path in paths:
        print(f"Remove if managed: {path}")
    if dry_run:
        print("Dry run: no files or services changed")
        return
    for path in paths:
        if path.parent.exists():
            safe_directory(path.parent)
        safe_destination(path)
    if MODPROBE.exists() and MODPROBE.read_bytes() != MODPROBE_LINE.encode():
        raise ValueError("Refusing to remove an unmanaged modprobe file")
    if UNIT.exists():
        systemctl("disable", "--now", "omafans-legion.service")
        status = systemctl("is-active", "--quiet", "omafans-legion.service", check=False)
        if status.returncode not in (3, 4):
            raise ValueError("Cannot verify daemon stopped; files retained")
        result = systemctl("show", "omafans-legion.service", "--property=Result", "--value")
        if result.stdout.strip() != b"success":
            raise ValueError("Service stop reported a failure; files retained")
    for path in paths:
        path.unlink(missing_ok=True)
    for path in (LIB, CONFIG):
        if path.exists() and not any(path.iterdir()):
            path.rmdir()
    systemctl("daemon-reload")
    print("Removed. The watchdog settings in modprobe.d are gone; reboot to fully clear.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove"))
    parser.add_argument("--user", help="Desktop account allowed to control the fan (required for install)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.action == "install" and not args.user:
        parser.error("install requires --user")
    if args.action == "remove" and args.user:
        parser.error("remove accepts only --dry-run")
    if not args.dry_run and os.geteuid() != 0:
        parser.error("Run the reviewed setup script as root, or use --dry-run")
    try:
        if args.action == "install":
            install(args.user, args.dry_run)
        else:
            remove(args.dry_run)
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
