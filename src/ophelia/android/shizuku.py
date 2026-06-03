"""Android body via Shizuku (rish) — OpenClaw-style, no root."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import structlog

log = structlog.get_logger()


class AndroidBody:
    """Execute phone actions through Shizuku/rish or phone_control.sh."""

    def __init__(
        self,
        phone_control: Path | None = None,
        rish_path: Path | None = None,
    ) -> None:
        self.phone_control = phone_control or Path.home() / "phone_control.sh"
        self.rish_path = rish_path or self._find_rish()

    def _find_rish(self) -> Path | None:
        for candidate in (
            Path.home() / "rish",
            Path.home() / "bin" / "rish",
            Path("/data/data/com.termux/files/home/rish"),
        ):
            if candidate.is_file():
                return candidate
        return shutil.which("rish") and Path(shutil.which("rish"))  # type: ignore

    @property
    def mode(self) -> str:
        if self.phone_control.is_file():
            return "phone_control"
        if self.rish_path:
            return "rish"
        return "termux_only"

    async def _run(self, cmd: list[str], timeout: float = 45.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": os.environ.get("PATH", "") + ":/data/data/com.termux/files/usr/bin"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return -1, "", "timeout"
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        return proc.returncode or 0, out, err

    async def shell(self, command: str) -> str:
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

        # Termux-only fallback (no UI tap)
        code, out, err = await self._run(["sh", "-c", command])
        if code == 0:
            return out or "(ok)"
        return f"shell error {code}: {err or out}"

    async def ui_dump(self) -> str:
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
            return err or "ui-dump failed — install phone_control.sh or use Shizuku"

        return (
            "No Shizuku body. Setup: Shizuku → Export to Termux → "
            "bash scripts/termux-shizuku-setup.sh"
        )

    async def tap(self, x: int, y: int) -> str:
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), "tap", str(x), str(y)]
            )
            return out or err or ("ok" if code == 0 else f"tap failed {code}")
        if self.rish_path:
            return await self.shell(f"input tap {x} {y}")
        return "tap requires Shizuku (rish) or ~/phone_control.sh"

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
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
        return "swipe requires Shizuku (rish) or ~/phone_control.sh"

    async def key(self, name: str) -> str:
        key = name.lower().replace("-", "_")
        allowed = {"home", "back", "volume_up", "volume_down"}
        if key not in allowed:
            return f"Unknown key '{name}'. Use: {', '.join(sorted(allowed))}"
        if self.phone_control.is_file():
            code, out, err = await self._run(
                ["bash", str(self.phone_control), key.replace("_", "-")]
            )
            return out or err or ("ok" if code == 0 else f"key failed {code}")
        events = {
            "home": 3,
            "back": 4,
            "volume_up": 24,
            "volume_down": 25,
        }
        if self.rish_path:
            return await self.shell(f"input keyevent {events[key]}")
        return "key requires Shizuku (rish) or ~/phone_control.sh"

    async def open_app(self, package: str) -> str:
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
            return "screenshot requires Shizuku"
        if code != 0:
            return err or "screencap failed"
        pull_code, _, _ = await self._run(["cp", remote, str(dest)])
        if pull_code == 0 and dest.is_file():
            return str(dest)
        return f"screenshot pull failed (captured at {remote})"

    def status_line(self) -> str:
        return f"Android body: {self.mode} (Shizuku=rish, OpenClaw-style=phone_control.sh)"
