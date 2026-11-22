"""Root on her phone body, and asking the owner before anything dangerous."""

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.core import danger, owner_approval


@pytest.mark.parametrize("cmd", [
    "reboot", "reboot bootloader", "svc power shutdown",
    "dd if=/sdcard/x.img of=/dev/block/by-name/boot", "fastboot flash boot x.img",
    "mkfs.ext4 /dev/block/sda1", "rm -rf /", "rm -rf /system", "rm -fr /sdcard",
    "rm -rf /data/data/*", "rm -r /storage/emulated/0", "rm -rf ~",
    "mount -o rw,remount /system", "pm uninstall com.whatsapp", "pm clear com.termux",
    "cmd package uninstall -k com.x", "pm disable-user com.android.systemui",
    "setenforce 0", "resetprop ro.debuggable 1", "setprop persist.x 1",
    "magisk --install-module /sdcard/m.zip", "ksud module install x.zip",
    "rm -rf /data/adb/modules", "cp evil.so /system/lib64/",
    "settings put global adb_enabled 0", "killall system_server",
    "am broadcast -a android.intent.action.MASTER_CLEAR",
])
def test_dangerous(cmd):
    assert danger.dangerous(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "input tap 540 1200", "input swipe 1 2 3 4 300", "input keyevent 3",
    "screencap -p /sdcard/ophelia_screen.png",
    "uiautomator dump /sdcard/ophelia_ui.xml && cat /sdcard/ophelia_ui.xml",
    "monkey -p com.spotify.music -c android.intent.category.LAUNCHER 1",
    "mkdir -p /sdcard/Download/.ophelia_stage && cp /data/app/x/base.apk "
    "/sdcard/Download/.ophelia_stage/x.apk && echo staged",
    "rm -f /sdcard/Download/.ophelia_stage/x.apk", "rm -rf /sdcard/Download/old_shots",
    "dumpsys battery", "dumpsys power", "cat /proc/meminfo", "pm list packages",
    "settings get system screen_brightness", "ls /data/data", "getprop ro.product.model",
])
def test_everyday_commands_are_fine(cmd):
    assert danger.dangerous(cmd) == "", cmd


def test_run_code_only_counts_with_a_privileged_shell():
    assert danger.dangerous_code("import os\nos.system('su -c reboot')")
    assert danger.dangerous_code("subprocess.run(['su', '-c', 'pm uninstall com.x'])")
    assert danger.dangerous_code("subprocess.run(['rish', '-c', 'reboot'])")
    assert danger.dangerous_code("print('please reboot your brain')") == ""
    assert danger.dangerous_code("subprocess.run(['su', '-c', 'dumpsys battery'])") == ""


@pytest.fixture
def asker():
    asked = []

    async def ask(approval_id, question):
        asked.append((approval_id, question))
        return True

    owner_approval.set_asker(ask)
    yield asked
    owner_approval.set_asker(None)


@pytest.mark.asyncio
async def test_ask_allowed_denied_timeout(asker):
    async def press(allowed):
        while not asker:
            await asyncio.sleep(0.01)
        owner_approval.answer(asker[-1][0], allowed)

    for allowed, want in ((True, "allowed"), (False, "denied")):
        asker.clear()
        outcome, _ = await asyncio.gather(owner_approval.ask("ok?"), press(allowed))
        assert outcome == want
    assert await owner_approval.ask("ok?", timeout=0.05) == "timeout"
    assert not owner_approval.answer(asker[-1][0], True)  # expired


@pytest.mark.asyncio
async def test_nobody_to_ask_means_no():
    owner_approval.set_asker(None)
    assert await owner_approval.ask("ok?") == "unavailable"
    msg = await owner_approval.permit("reboot", "restarts the phone")
    assert msg.startswith("Not run") and "no way to ask" in msg


def _phone_tools(shell_result="done"):
    from ophelia.tools.phone_tools import PhoneToolsMixin

    t = PhoneToolsMixin.__new__(PhoneToolsMixin)
    t.android = MagicMock(mode="root")
    t.android.shell = AsyncMock(return_value=shell_result)
    t.settings = MagicMock()
    t._phone_unavailable_reason = lambda: None
    return t


@pytest.mark.asyncio
async def test_phone_shell_asks_first(asker, monkeypatch):
    tools = _phone_tools()
    # Safe: runs, nobody asked.
    assert await tools._phone_shell("dumpsys battery") == "done"
    assert asker == []

    async def deny_soon():
        while not asker:
            await asyncio.sleep(0.01)
        owner_approval.answer(asker[-1][0], False)

    out, _ = await asyncio.gather(tools._phone_shell("pm uninstall com.whatsapp"), deny_soon())
    assert "Okia said no" in out
    tools.android.shell.assert_awaited_once_with("dumpsys battery")
    assert "pm uninstall com.whatsapp" in asker[-1][1]

    async def allow_soon():
        while len(asker) < 2:
            await asyncio.sleep(0.01)
        owner_approval.answer(asker[-1][0], True)

    out, _ = await asyncio.gather(tools._phone_shell("reboot"), allow_soon())
    assert out == "done"
    tools.android.shell.assert_awaited_with("reboot")


@pytest.mark.asyncio
async def test_run_code_asks_first(asker, tmp_path):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = MagicMock(data_dir=tmp_path)
    out = await asyncio.wait_for(asyncio.gather(
        reg._run_code("import subprocess\nsubprocess.run(['su','-c','reboot'])"),
        _deny_first(asker)), 5)
    assert "Okia said no" in out[0]


async def _deny_first(asker):
    while not asker:
        await asyncio.sleep(0.01)
    owner_approval.answer(asker[-1][0], False)


def _fake_su(tmp_path, root=True):
    """An `su` on PATH that records each command (and is root, or isn't)."""
    log = tmp_path / "su.log"
    su = tmp_path / "bin" / "su"
    su.parent.mkdir(parents=True)
    su.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = "-c" ] && shift\n'
        f'echo "$*" >> {log}\n'
        + ('case "$1" in id) echo "uid=0(root) gid=0(root)";; *) echo "ok: $1";; esac\n'
           if root else 'echo "Permission denied" >&2; exit 1\n'))
    su.chmod(su.stat().st_mode | stat.S_IEXEC)
    return su.parent, log


@pytest.fixture
def termux(monkeypatch):
    from ophelia.android import shizuku

    monkeypatch.setattr(shizuku, "is_termux", lambda: True)
    monkeypatch.setattr(shizuku.adb_util, "find_adb", lambda: None)
    return shizuku


@pytest.mark.asyncio
async def test_root_runs_everything_through_su(tmp_path, monkeypatch, termux):
    bindir, log = _fake_su(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    body = termux.AndroidBody(phone_control=tmp_path / "none.sh", rish_path=None)
    assert body.mode == "root"  # known before any tool call
    assert "root" in body.status_line()
    assert await body.tap(10, 20) == "ok: input tap 10 20"
    await body.key("home")
    await body.open_app("com.spotify.music")
    assert await body.shell("dumpsys battery") == "ok: dumpsys battery"
    ran = log.read_text().splitlines()
    assert ran[0] == "id"
    assert ran[1:] == ["input tap 10 20", "input keyevent 3",
                       "monkey -p com.spotify.music -c android.intent.category.LAUNCHER 1",
                       "dumpsys battery"]


@pytest.mark.asyncio
async def test_no_root_falls_back_and_shizuku_mode_skips_it(tmp_path, monkeypatch, termux):
    bindir, log = _fake_su(tmp_path, root=False)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    body = termux.AndroidBody(phone_control=tmp_path / "none.sh", rish_path=None)
    assert body.mode == "termux_only"
    bindir2, log2 = _fake_su(tmp_path / "r")
    monkeypatch.setenv("PATH", f"{bindir2}:{os.environ['PATH']}")
    forced = termux.AndroidBody(phone_control=tmp_path / "none.sh", rish_path=None,
                                shell_mode="shizuku")
    assert forced.mode != "root"
    assert not log2.exists()  # never even asked su


def test_settings_default_is_auto():
    from ophelia.config import Settings

    assert Settings(_env_file=None).phone_shell_mode == "auto"


@pytest.mark.asyncio
async def test_telegram_buttons_answer_the_question(asker):
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = MagicMock()
    gw.settings.is_owner_channel.side_effect = lambda ch: ch == "telegram:1"

    def press(uid, data):
        q = SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data, answer=AsyncMock(),
                            edit_message_text=AsyncMock(),
                            message=SimpleNamespace(text="⚠️ Ophelia wants to run: reboot"))
        return q, SimpleNamespace(callback_query=q)

    task = asyncio.create_task(owner_approval.ask("reboot?"))
    while not asker:
        await asyncio.sleep(0.01)
    aid = asker[-1][0]
    q, upd = press(9, f"ophelia:perm:allow:{aid}")  # not the owner
    await gw.on_permission_callback(upd, None)
    assert not task.done()
    q, upd = press(1, f"ophelia:perm:deny:{aid}")
    await gw.on_permission_callback(upd, None)
    assert await task == "denied"
    assert "Denied" in q.edit_message_text.await_args.args[0]
