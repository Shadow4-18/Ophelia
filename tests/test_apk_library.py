"""App library: pulling installed apps off the phone, importing APK files,
and looking inside them (binary manifest, dex classes, engine, nested
archives, search, notes) — all without Android SDK tools."""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from ophelia.android import apk_extract, apk_inspect

ANDROID_NS = "http://schemas.android.com/apk/res/android"


# --- tiny encoders for the real binary formats ------------------------------


def _string_pool(strings: list[str]) -> bytes:
    data = b""
    offsets = []
    for s in strings:
        offsets.append(len(data))
        b = s.encode("utf-8")
        assert len(b) < 128
        data += bytes([len(s), len(b)]) + b + b"\0"
    while len(data) % 4:
        data += b"\0"
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    body = struct.pack(f"<{len(offsets)}I", *offsets) + data
    return struct.pack("<HHIIIIII", 0x0001, header_size, header_size + len(body),
                       len(strings), 0, 0x100, strings_start, 0) + body


def make_axml(elements: list[tuple[str, dict[str, object]] | str]) -> bytes:
    """elements: ("tag", {"android:name": "x", "package": "y", "android:debuggable": True})
    to open, "/" to close the last open element."""
    strings: list[str] = []

    def idx(s: str) -> int:
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    idx(ANDROID_NS)
    chunks = b""
    for el in elements:
        if el == "/":
            chunks += struct.pack("<HHIIIII", 0x0103, 16, 24, 0, 0xFFFFFFFF, 0xFFFFFFFF, 0)
            continue
        tag, attrs = el
        attr_bytes = b""
        for key, value in attrs.items():
            ns = idx(ANDROID_NS) if key.startswith("android:") else 0xFFFFFFFF
            name = idx(key.split(":", 1)[-1])
            if isinstance(value, bool):
                raw, dtype, data = 0xFFFFFFFF, 0x12, 0xFFFFFFFF if value else 0
            elif isinstance(value, int):
                raw, dtype, data = 0xFFFFFFFF, 0x10, value
            else:
                raw = idx(str(value))
                dtype, data = 0x03, raw
            attr_bytes += struct.pack("<IIIHBBI", ns, name, raw, 8, 0, dtype, data)
        body = struct.pack("<IIHHHHHH", 0xFFFFFFFF, idx(tag), 20, 20, len(attrs), 0, 0, 0)
        size = 16 + len(body) + len(attr_bytes)
        chunks += struct.pack("<HHIII", 0x0102, 16, size, 1, 0xFFFFFFFF) + body + attr_bytes
    pool = _string_pool(strings)
    total = 8 + len(pool) + len(chunks)
    return struct.pack("<HHI", 0x0003, 8, total) + pool + chunks


def make_dex(classes: list[str], extra_strings: list[str] = ()) -> bytes:
    descs = [f"L{c.replace('.', '/')};" for c in classes]
    strings = sorted(set(descs) | set(extra_strings))
    header = bytearray(0x70)
    header[:8] = b"dex\n035\0"
    str_ids_off = 0x70
    type_ids_off = str_ids_off + 4 * len(strings)
    class_defs_off = type_ids_off + 4 * len(descs)
    data_off = class_defs_off + 32 * len(descs)
    data = b""
    str_offsets = []
    for s in strings:
        str_offsets.append(data_off + len(data))
        b = s.encode()
        data += bytes([len(s)]) + b + b"\0"
    struct.pack_into("<IIII", header, 0x38, len(strings), str_ids_off, len(descs), type_ids_off)
    struct.pack_into("<II", header, 0x60, len(descs), class_defs_off)
    out = bytes(header) + struct.pack(f"<{len(strings)}I", *str_offsets)
    out += struct.pack(f"<{len(descs)}I", *[strings.index(d) for d in descs])
    for i in range(len(descs)):
        out += struct.pack("<I", i) + b"\0" * 28
    return out + data


MANIFEST = make_axml([
    ("manifest", {"package": "com.example.cards", "android:versionName": "1.2.3",
                  "android:versionCode": 42}),
    ("uses-sdk", {"android:minSdkVersion": 24, "android:targetSdkVersion": 34}), "/",
    ("uses-permission", {"android:name": "android.permission.INTERNET"}), "/",
    ("application", {"android:debuggable": True}),
    ("activity", {"android:name": ".MainActivity"}),
    ("intent-filter", {}),
    ("action", {"android:name": "android.intent.action.MAIN"}), "/",
    ("category", {"android:name": "android.intent.category.LAUNCHER"}), "/",
    "/",
    "/",
    ("activity", {"android:name": "com.example.cards.SettingsActivity"}),
    ("intent-filter", {}),
    ("data", {"android:scheme": "cards", "android:host": "open"}), "/",
    "/",
    "/",
    ("meta-data", {"android:name": "unity.splash-mode", "android:value": "0"}), "/",
    "/",
    "/",
])

GAME_LUA = "-- scoring\nfunction score_hand(hand)\n  return hand.chips * hand.mult\nend\n"


def build_apk(path: Path, *, splits: bool = True) -> Path:
    love = io.BytesIO()
    with zipfile.ZipFile(love, "w") as z:
        z.writestr("main.lua", "require 'game'\n")
        z.writestr("game.lua", GAME_LUA)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", MANIFEST)
        z.writestr("classes.dex", make_dex(
            ["com.example.cards.MainActivity", "org.love2d.android.GameActivity",
             "com.google.android.gms.ads.AdView", "a.a", "a.b"],
            ["https://api.example.com/v1/leaderboard"],
        ))
        z.writestr("assets/game.love", love.getvalue())
        z.writestr("assets/config.json", '{"difficulty": "hard", "ante_max": 8}\n')
        z.writestr("res/layout/main.xml", make_axml([("LinearLayout", {}), "/"]))
    if splits:
        with zipfile.ZipFile(path.parent / "split_config.arm64_v8a.apk", "w") as z:
            z.writestr("lib/arm64-v8a/liblove.so", b"\x7fELF\0\0secret_symbol_name\0")
    return path


@pytest.fixture
def lib(isolated_env):
    return isolated_env / "apks"


@pytest.fixture
def pulled(lib) -> Path:
    d = lib / "com.example.cards" / "1.2.3-42"
    d.mkdir(parents=True)
    build_apk(d / "base.apk")
    (d / "info.json").write_text(json.dumps({"package": "com.example.cards"}))
    return d


# --- parsing ----------------------------------------------------------------


def test_manifest_decodes_and_summarizes():
    m = apk_inspect.summarize_manifest(apk_inspect.parse_axml(MANIFEST))
    assert m.package == "com.example.cards"
    assert (m.version_name, m.version_code) == ("1.2.3", "42")
    assert (m.min_sdk, m.target_sdk) == ("24", "34")
    assert m.debuggable
    assert m.permissions == ["android.permission.INTERNET"]
    assert m.launcher == ["com.example.cards.MainActivity"]  # ".Main" expanded
    assert "com.example.cards.SettingsActivity" in m.activities
    assert m.launcher != m.activities  # settings isn't a launcher
    assert m.schemes == ["cards://open"]
    assert "unity.splash-mode=0" in m.meta_data


def test_dex_classes_and_strings():
    dex = apk_inspect.parse_dex(make_dex(["com.a.B", "x.Y"], ["hello"]))
    assert sorted(dex.classes) == ["com.a.B", "x.Y"]
    assert "hello" in dex.strings
    with pytest.raises(ValueError):
        apk_inspect.parse_dex(b"not dex")


def test_inspect_reports_engine_sdks_and_splits(pulled):
    text = apk_inspect.analyze(pulled).to_text()
    assert "com.example.cards 1.2.3 (code 42)" in text
    assert "DEBUGGABLE" in text
    assert "LÖVE" in text  # liblove.so in the split + game.love
    assert "AdMob" in text and "LÖVE" in text.split("Libraries/SDKs:")[1]
    assert "arm64-v8a" in text and "liblove.so" in text
    assert "obfuscated" in text  # a.a, a.b
    assert "Launcher activity: com.example.cards.MainActivity" in text


# --- reading / listing / searching ------------------------------------------


def test_read_manifest_nested_lua_split_and_binary(pulled):
    out = apk_inspect.read_file(pulled, "AndroidManifest.xml")
    assert "decoded binary XML" in out and 'android:name="android.permission.INTERNET"' in out
    assert "return hand.chips * hand.mult" in apk_inspect.read_file(
        pulled, "assets/game.love!/game.lua")
    listing = apk_inspect.read_file(pulled, "assets/game.love")
    assert "game.lua" in listing and "!/<file>" in listing
    so = apk_inspect.read_file(pulled, "split_config.arm64_v8a.apk:lib/arm64-v8a/liblove.so")
    assert "binary" in so and "secret_symbol_name" in so
    assert "MainActivity" in apk_inspect.read_file(pulled, "classes.dex")
    with pytest.raises(FileNotFoundError):
        apk_inspect.read_file(pulled, "assets/nope.txt")


def test_read_pages_long_files(pulled):
    first = apk_inspect.read_file(pulled, "assets/config.json", max_chars=500)
    assert "ante_max" in first
    big = apk_inspect._window("t", "x" * 1200, 0, 500)
    assert "offset=500" in big


def test_list_files_including_nested(pulled):
    assert "assets/config.json" in apk_inspect.list_files(pulled, under="assets/")
    assert "split_config.arm64_v8a.apk:lib/arm64-v8a/liblove.so" in apk_inspect.list_files(
        pulled, pattern="*.so")
    nested = apk_inspect.list_files(pulled, under="assets/game.love!/")
    assert "assets/game.love!/game.lua" in nested


def test_search_covers_code_strings_assets_and_love(pulled):
    assert "api.example.com" in apk_inspect.search(pulled, r"api\.example")
    assert "assets/config.json:1" in apk_inspect.search(pulled, "ante_max")
    hits = apk_inspect.search(pulled, "score_hand")
    assert "assets/game.love!/game.lua:2" in hits
    assert "[manifest]" in apk_inspect.search(pulled, "INTERNET")
    assert apk_inspect.search(pulled, "zzz_nothing") == "No matches."
    assert "match" in apk_inspect.search(pulled, "[unbalanced")  # bad regex -> literal


def test_decompiled_reads_stay_inside(pulled):
    src = pulled / "jadx" / "sources" / "com"
    src.mkdir(parents=True)
    (src / "Main.java").write_text("class Main { int secretRule = 7; }\n")
    assert "secretRule" in apk_inspect.read_file(pulled, "src:sources/com/Main.java")
    assert "outside" in apk_inspect.read_file(pulled, "src:../../info.json")
    assert "src:sources/com/Main.java:1" in apk_inspect.search(pulled, "secretRule")


# --- the library ------------------------------------------------------------


def test_import_apk_and_bundle(lib, tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    apk = build_apk(src / "cards.apk", splits=False)
    msg = apk_extract.import_file(apk)
    assert msg.startswith("Imported com.example.cards 1.2.3"), msg
    assert apk_extract.resolve("com.example.cards").name == "1.2.3-42"

    bundle = tmp_path / "cards.apks"
    with zipfile.ZipFile(bundle, "w") as z:
        z.write(apk, "com.example.cards.apk")
        z.writestr("split_config.en.apk", b"PK\5\6" + b"\0" * 18)
    assert apk_extract.import_file(bundle).startswith("Imported")
    d = apk_extract.resolve("com.example.cards")
    assert sorted(p.name for p in d.glob("*.apk")) == ["base.apk", "split_config.en.apk"]
    assert "com.example.cards  1.2.3-42" in apk_extract.library_text()

    junk = tmp_path / "junk.apk"
    junk.write_text("nope")
    assert "isn't an APK" in apk_extract.import_file(junk)


def test_notes_append_replace_and_limits(lib):
    assert "doesn't look like" in apk_extract.write_notes("not a package", "x")
    apk_extract.write_notes("com.example.cards", "Hands are scored in game.lua")
    apk_extract.write_notes("com.example.cards", "Play button is drawn, no element")
    notes = apk_extract.read_notes("com.example.cards")
    assert notes.startswith("# com.example.cards") and "game.lua" in notes and "drawn" in notes
    apk_extract.write_notes("com.example.cards", "short", replace=True)
    assert apk_extract.read_notes("com.example.cards") == "short\n"
    assert "Condense" in apk_extract.write_notes(
        "com.example.cards", "x" * (apk_extract.NOTES_MAX_CHARS + 1), replace=True)


def test_parse_phone_listings():
    out = ("package:/data/app/~~ab==/com.example.cards-Xy==/base.apk=com.example.cards\n"
           "package:/system/app/Foo/Foo.apk=com.android.foo\njunk\n")
    apps = apk_extract.parse_package_list(out)
    assert [a.package for a in apps] == ["com.android.foo", "com.example.cards"]
    assert apps[1].base_path == "/data/app/~~ab==/com.example.cards-Xy==/base.apk"
    paths = apk_extract.parse_pm_path(
        "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.arm64_v8a.apk\n")
    assert len(paths) == 2
    assert apk_extract.parse_version("    versionCode=42 minSdk=24\n    versionName=1.2.3") == (
        "1.2.3", "42")
    assert not apk_extract.valid_package("com.x; rm -rf /")


class FakePhone:
    """Shell answers like a real phone; pull_file copies from a fake /data/app."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.commands: list[str] = []

    async def shell(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("pm path com.example.cards"):
            return "\n".join(f"package:{p}" for p in self.files if p.endswith(".apk"))
        if command.startswith("pm path"):
            return ""
        if command.startswith("dumpsys package"):
            return "versionCode=42 minSdk=24 targetSdk=34\nversionName=1.2.3"
        if command.startswith("stat -c %s"):
            path = command.split(" ", 3)[3].strip("'")
            return str(len(self.files[path]))
        if command.startswith("ls -1 /sdcard/Android/obb/"):
            return "\n".join(Path(p).name for p in self.files if p.endswith(".obb"))
        return ""

    async def pull_file(self, remote: str, dest: Path) -> str | None:
        dest.write_bytes(self.files[remote])
        return None


async def test_extract_from_phone(lib, tmp_path):
    src = tmp_path / "phone"
    src.mkdir()
    base = build_apk(src / "base.apk").read_bytes()
    split = (src / "split_config.arm64_v8a.apk").read_bytes()
    phone = FakePhone({
        "/data/app/~~x==/com.example.cards-y==/base.apk": base,
        "/data/app/~~x==/com.example.cards-y==/split_config.arm64_v8a.apk": split,
        "/sdcard/Android/obb/com.example.cards/main.42.com.example.cards.obb": b"obbdata",
    })
    msg = await apk_extract.extract_app(phone, "com.example.cards", include_obb=True)
    assert msg.startswith("Extracted com.example.cards 1.2.3 (2 apk file(s) + 1 obb"), msg
    d = lib / "com.example.cards" / "1.2.3-42"
    assert (d / "base.apk").read_bytes() == base
    assert (d / "obb" / "main.42.com.example.cards.obb").read_bytes() == b"obbdata"
    info = json.loads((d / "info.json").read_text())
    assert len(info["files"]) == 3 and all(len(f["sha256"]) == 64 for f in info["files"])
    # and it's immediately inspectable, splits included
    assert "liblove.so" in apk_inspect.analyze(d).to_text()

    assert "isn't installed" in await apk_extract.extract_app(phone, "com.not.there")
    assert "doesn't look like" in await apk_extract.extract_app(phone, "x; reboot")
    assert not any("reboot" in c for c in phone.commands)


async def test_extract_refuses_when_disk_is_short(lib, tmp_path, monkeypatch):
    phone = FakePhone({"/data/app/x/base.apk": b"x" * 10})
    monkeypatch.setattr(apk_extract.shutil, "disk_usage",
                        lambda _: type("U", (), {"free": 100})())
    assert "Not enough space" in await apk_extract.extract_app(phone, "com.example.cards")


async def test_extract_detects_short_copy(lib):
    phone = FakePhone({"/data/app/x/base.apk": b"x" * 10})

    async def short_pull(remote, dest):
        dest.write_bytes(b"x")

    phone.pull_file = short_pull
    assert "incomplete" in await apk_extract.extract_app(phone, "com.example.cards")


# --- tools ------------------------------------------------------------------


async def test_tools_end_to_end(pulled, settings, tmp_path):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry(settings, tmp_path / "artifacts")
    reg.set_owner(True)
    out = await reg.dispatch("apk_inspect", json.dumps({"package": "com.example.cards"}))
    assert "LÖVE" in out
    out = await reg.dispatch("apk_read", json.dumps(
        {"package": "com.example.cards", "path": "assets/game.love!/game.lua"}))
    assert "score_hand" in out
    out = await reg.dispatch("apk_notes", json.dumps(
        {"package": "com.example.cards", "text": "score = chips * mult"}))
    assert out.startswith("Saved")
    assert "notes" in await reg.dispatch("apk_list", "{}")
    assert "isn't in your app library" in await reg.dispatch(
        "apk_inspect", json.dumps({"package": "com.other.app"}))
    # no phone body: extracting says why instead of failing obscurely
    assert "disabled" in (await reg.dispatch(
        "apk_extract", json.dumps({"package": "com.other.app"}))).lower()


async def test_apk_tools_are_owner_only(settings, tmp_path):
    from ophelia.tools.registry import GUEST_DENIED_TOOLS, ToolRegistry

    assert {"apk_extract", "apk_read", "apk_notes"} <= GUEST_DENIED_TOOLS
    reg = ToolRegistry(settings, tmp_path / "artifacts")
    reg.set_owner(False)
    assert "owner-only" in await reg.dispatch("apk_list", "{}")


def test_apk_tools_have_their_own_tier():
    from ophelia.tools import tiers

    assert tiers.group_of("apk_search") == "apps"
    assert "apps" in tiers.triggered_groups("can you decompile that game and see how it works")
    assert "apps" in tiers.triggered_groups("pull the apk for balatro")


def test_import_folder_like_the_extractor_app_writes(lib, tmp_path):
    """The App Extractor app saves .apk / .apks (base.apk + splits + info.json)."""
    out = tmp_path / "Extracted apps"
    out.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    build_apk(src / "base.apk")
    with zipfile.ZipFile(out / "Cards_com.example.cards_1.2.3.apks", "w") as z:
        z.write(src / "base.apk", "base.apk")
        z.write(src / "split_config.arm64_v8a.apk", "split_config.arm64_v8a.apk")
        z.writestr("info.json", '{"package": "com.example.cards"}')
    (out / "notes.txt").write_text("ignored")
    msg = apk_extract.import_path(out)
    assert msg.startswith("Imported com.example.cards 1.2.3 (2 apk file(s))"), msg
    d = apk_extract.resolve("com.example.cards")
    assert "liblove.so" in apk_inspect.analyze(d).to_text()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "No .apk" in apk_extract.import_path(empty)
