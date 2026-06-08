"""Android body — Shizuku on phone, ADB from PC (root optional)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import structlog

from ophelia.android import adb as adb_util
from ophelia.platform import is_termux

log = structlog.get_logger()


class AndroidBody:
    """Execute phone actions via Shizuku (Termux) or ADB (PC → phone)."""

    def __init__(
        self,
        phone_control: Path | None = None,
        rish_path: Path | None = None,
        *,
        adb_device: str | None = None,
        adb_root: bool = False,
    ) -> None:
        self.phone_control = phone_control or Path.home() / "phone_control.sh"
        self.rish_path = rish_path or self._find_rish()
        self.adb_device = (adb_device or "").strip() or None
        self.adb_root = adb_root
        self._adb_path = adb_util.find_adb()
        self._adb_ready = False
        self._adb_mode = "none"

    def _find_rish(self) -> Path | None:
        for candidate in (
            Path.home() / "rish",
            Path.home() / "bin" / "rish",
            Path("/data/data/com.termux/files/home/rish"),
        ):
            if candidate.is_file():
                return candidate
        return shutil.which("rish") and Path(shutil.which("rish"))  # type: ignore

    async def ensure_ready(self) -> None:
        if self._adb_ready or self.mode != "adb":
            return
        assert self._adb_path
        err = await adb_util.connect_if_needed(self._adb_path, self.adb_device)
        if err:
            log.warning("adb.connect", error=err)
        if self.adb_root:
            root_err = await adb_util.ensure_root(self._adb_path, self.adb_device)
            if root_err:
                log.warning("adb.root", error=root_err)
            else:
                self._adb_mode = "adb_root"
        ok = await adb_util.probe_adb(self._adb_path, self.adb_device)
        self._adb_ready = ok
        if ok and self._adb_mode == "none":
            self._adb_mode = "adb"

    @property
    def mode(self) -> str:
        if not is_termux() and self._adb_path:
            return self._adb_mode if self._adb_mode != "none" else "adb"
        if self.phone_control.is_file() and is_termux():
            return "phone_control"
        if self.rish_path:
            return "rish"
        if self._adb_path and self._use_adb():
            if self._adb_mode == "adb_root":
                return "adb_root"
            return "adb"
        return "termux_only"

    async def _run(self, cmd: list[str], timeout: float = 45.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PATH": os.environ.get("PATH", "")
                + ":/data/data/com.termux/files/usr/bin",
            },
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return -1, "", "timeout"
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        return proc.returncode or 0, out, err

    def _use_adb(self) -> bool:
        if is_termux() and self.phone_control.is_file():
            return False
        return bool(self._adb_path and (self.adb_device or not is_termux()))

    async def _adb_shell(self, command: str) -> str:
        await self.ensure_ready()
        assert self._adb_path
        code, out, err = await adb_util.adb_run(
            adb_util.adb_base(self._adb_path, self.adb_device) + ["shell", command],
            timeout=45.0,
        )
        if code == 0:
            return out or "(ok)"
        return f"adb error {code}: {err or out}"

    async def shell(self, command: str) -> str:
        if self._use_adb():
            return await self._adb_shell(command)
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), "shell", command]
            )
            if code == 0:
                return out or "(ok)"
            return f"phone_control error {code}: {err or out}"
        if self.rish_path:
            code, out, err = await self._run(
                ["sh", str(self.rish_path), "-c", command]
            )
            if code == 0:
                return out or "(ok)"
            return f"rish error {code}: {err or out}"
        code, out, err = await self._run(["sh", "-c", command])
        if code == 0:
            return out or "(ok)"
        return f"shell error {code}: {err or out}"

    async def ui_dump(self) -> str:
        if self._use_adb():
            await self.ensure_ready()
            assert self._adb_path
            remote = "/sdcard/ophelia_ui.xml"
            await self._adb_shell(f"uiautomator dump {remote}")
            code, out, err = await adb_util.adb_run(
                adb_util.adb_base(self._adb_path, self.adb_device)
                + ["shell", f"cat {remote}"],
                timeout=60.0,
            )
            if code == 0 and out:
                return out[:12000]
            return err or out or "ui-dump failed"

        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), "ui-dump"], timeout=60.0
            )
            if code == 0 and out:
                return out[:12000]
            return err or out or "ui-dump failed"

        if self.rish_path:
            code, out, err = await self._run(
                [
                    "sh",
                    str(self.rish_path),
                    "-c",
                    "uiautomator dump /sdcard/ophelia_ui.xml && cat /sdcard/ophelia_ui.xml",
                ],
                timeout=60.0,
            )
            if code == 0 and out:
                return out[:12000]
            return err or "ui-dump failed"

        return (
            "No phone body. PC: set OPHELIA_ADB_DEVICE=ip:5555 + wireless debugging. "
            "Phone: Shizuku + phone_control.sh"
        )

    async def tap(self, x: int, y: int) -> str:
        if self._use_adb():
            return await self._adb_shell(f"input tap {x} {y}")
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), "tap", str(x), str(y)]
            )
            return out or err or ("ok" if code == 0 else f"tap failed {code}")
        if self.rish_path:
            return await self.shell(f"input tap {x} {y}")
        return "tap requires ADB or Shizuku"

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
        if self._use_adb():
            return await self._adb_shell(
                f"input swipe {x1} {y1} {x2} {y2} {duration_ms}"
            )
        if self.phone_control.is_file():
            code, out, err = await self._run(
                [
                    "bash",
                    str(self.phone_control),
                    "swipe",
                    str(x1),
                    str(y1),
                    str(x2),
                    str(y2),
                    str(duration_ms),
                ]
            )
            return out or err or ("ok" if code == 0 else f"swipe failed {code}")
        if self.rish_path:
            return await self.shell(
                f"input swipe {x1} {y1} {x2} {y2} {duration_ms}"
            )
        return "swipe requires ADB or Shizuku"

    async def key(self, name: str) -> str:
        key = name.lower().replace("-", "_")
        allowed = {"home", "back", "volume_up", "volume_down"}
        if key not in allowed:
            return f"Unknown key '{name}'. Use: {', '.join(sorted(allowed))}"
        events = {
            "home": 3,
            "back": 4,
            "volume_up": 24,
            "volume_down": 25,
        }
        if self._use_adb():
            return await self._adb_shell(f"input keyevent {events[key]}")
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), key.replace("_", "-")]
            )
            return out or err or ("ok" if code == 0 else f"key failed {code}")
        if self.rish_path:
            return await self.shell(f"input keyevent {events[key]}")
        return "key requires ADB or Shizuku"

    async def open_app(self, package: str) -> str:
        if self._use_adb():
            return await self._adb_shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
            )
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), "open-app", package]
            )
            return out or err or ("ok" if code == 0 else f"open failed {code}")
        return await self.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
        )

    async def screenshot_path(self, dest: Path) -> str:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._use_adb():
            await self.ensure_ready()
            assert self._adb_path
            err = await adb_util.screencap_pull(
                self._adb_path, self.adb_device, dest
            )
            if err:
                return err
            return str(dest)

        remote = "/sdcard/ophelia_screen.png"
        code = 1
        err = ""
        if self.phone_control.is_file():
            code, _, err = await self._run(
                ["bash", str(self.phone_control), "screenshot", remote]
            )
        elif self.rish_path:
            code, _, err = await self._run(
                ["sh", str(self.rish_path), "-c", f"screencap -p {remote}"]
            )
        else:
            return "screenshot requires ADB or Shizuku"
        if code != 0:
            return err or "screencap failed"
        pull_code, _, _ = await self._run(["cp", remote, str(dest)])
        if pull_code == 0 and dest.is_file():
            return str(dest)
        return f"screenshot pull failed (captured at {remote})"

    def status_line(self) -> str:
        if self._use_adb():
            root = " + root" if self.adb_root else " (no root)"
            target = self.adb_device or "default USB/wireless"
            return f"Phone body: adb → {target}{root}"
        return f"Phone body: {self.mode} (Shizuku=rish, OpenClaw-style=phone_control.sh)"
