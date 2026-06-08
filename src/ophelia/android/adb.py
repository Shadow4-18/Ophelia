"""Remote phone control via ADB from PC (no root) or adb root (optional)."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path


def find_adb() -> str | None:
    return shutil.which("adb")


async def adb_run(
    args: list[str],
    *,
    timeout: float = 45.0,
    binary_out: Path | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    if binary_out is not None:
        binary_out.write_bytes(stdout)
        return proc.returncode or 0, "", stderr.decode(errors="replace").strip()
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    return proc.returncode or 0, out, err


def adb_base(adb: str, device: str | None) -> list[str]:
    cmd = [adb]
    if device:
        cmd.extend(["-s", device])
    return cmd


async def connect_if_needed(adb: str, device: str | None) -> str | None:
    """Wireless pairing: OPHELIA_ADB_DEVICE=192.168.x.x:5555"""
    if not device or ":" not in device:
        return None
    code, out, err = await adb_run([adb, "connect", device], timeout=15.0)
    if code != 0:
        return err or out or f"adb connect failed ({code})"
    return None


async def ensure_root(adb: str, device: str | None) -> str | None:
    code, out, err = await adb_run(adb_base(adb, device) + ["root"], timeout=20.0)
    if code != 0 and "cannot run as root" not in (err + out).lower():
        return err or out or "adb root failed"
    await asyncio.sleep(1.5)
    return None


async def probe_adb(adb: str, device: str | None) -> bool:
    code, out, _ = await adb_run(
        adb_base(adb, device) + ["shell", "echo", "ophelia_ok"],
        timeout=10.0,
    )
    return code == 0 and "ophelia_ok" in out


async def screencap_pull(adb: str, device: str | None, dest: Path) -> str | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = await adb_run(
        adb_base(adb, device) + ["exec-out", "screencap", "-p"],
        timeout=30.0,
        binary_out=dest,
    )
    if code != 0 or not dest.is_file() or dest.stat().st_size < 100:
        return err or "screencap failed"
    return None
