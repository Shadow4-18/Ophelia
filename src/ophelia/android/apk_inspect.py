"""Look inside an APK without any Android SDK tools.

Pure Python over ``zipfile``: decode the binary AndroidManifest.xml, read the
class names out of classes*.dex, recognise the engine/framework from the files
it ships (Unity, Godot, LÖVE, Flutter, React Native, …), list and read files —
including inside nested archives such as a LÖVE game's ``game.love`` — and
search all of it. If ``jadx`` is installed, decompiled Java sources are
searchable and readable too.

Everything works on one pulled version of an app: a directory holding
``base.apk`` and any ``split_*.apk`` (see apk_extract).
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

# Nested archives you can read into with "outer!/inner" paths.
NESTED_ARCHIVE_EXT = (".love", ".zip", ".jar", ".apk", ".aar", ".obb")
MAX_READ_BYTES = 40_000

# ---------------------------------------------------------------------------
# Binary XML (AXML) — AndroidManifest.xml inside an APK
# ---------------------------------------------------------------------------

_RES_STRING_POOL = 0x0001
_RES_XML = 0x0003
_RES_XML_RESOURCE_MAP = 0x0180
_RES_XML_START_ELEMENT = 0x0102

# android: attribute resource ids, for manifests whose attribute *names* were
# stripped by obfuscators (the resource map still says which attribute it is).
_ANDROID_ATTR_IDS = {
    0x01010001: "label",
    0x01010002: "icon",
    0x01010003: "name",
    0x01010010: "exported",
    0x0101000F: "debuggable",
    0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion",
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x01010024: "value",
    0x010104EA: "extractNativeLibs",
}
_ANDROID_NS = "http://schemas.android.com/apk/res/android"


@dataclass
class XmlElement:
    tag: str
    attrs: dict[str, str]
    depth: int


def _read_string_pool(data: bytes, off: int) -> list[str]:
    header_size, size = struct.unpack_from("<HI", data, off + 2)
    count, _styles, flags, strings_start, _ = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from(f"<{count}I", data, off + header_size)
    base = off + strings_start
    out: list[str] = []
    for o in offsets:
        p = base + o
        try:
            if utf8:
                # utf-16 length, then utf-8 byte length (each 1 or 2 bytes)
                p += 2 if data[p] & 0x80 else 1
                n = data[p]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[p + 1]
                    p += 2
                else:
                    p += 1
                out.append(data[p:p + n].decode("utf-8", errors="replace"))
            else:
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                    p += 2
                out.append(data[p:p + n * 2].decode("utf-16-le", errors="replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def _typed_value(dtype: int, value: int, strings: list[str]) -> str:
    if dtype == 0x03:
        return strings[value] if 0 <= value < len(strings) else ""
    if dtype == 0x12:
        return "true" if value else "false"
    if dtype == 0x10:
        return str(struct.unpack("<i", struct.pack("<I", value))[0])
    if dtype == 0x11:
        return hex(value)
    if dtype == 0x01:
        return f"@0x{value:08x}"
    if dtype == 0x02:
        return f"?0x{value:08x}"
    if dtype == 0x04:
        return repr(struct.unpack("<f", struct.pack("<I", value))[0])
    return f"0x{value:x}"


def parse_axml(data: bytes) -> list[XmlElement]:
    """Elements of a binary XML file, in document order."""
    if len(data) < 8 or struct.unpack_from("<H", data, 0)[0] != _RES_XML:
        raise ValueError("not a binary XML file")
    strings: list[str] = []
    res_ids: list[int] = []
    elements: list[XmlElement] = []
    depth = 0
    off = struct.unpack_from("<H", data, 2)[0]
    end = min(len(data), struct.unpack_from("<I", data, 4)[0] or len(data))
    while off + 8 <= end:
        ctype, header_size, size = struct.unpack_from("<HHI", data, off)
        if size < 8:
            break
        if ctype == _RES_STRING_POOL:
            strings = _read_string_pool(data, off)
        elif ctype == _RES_XML_RESOURCE_MAP:
            n = (size - header_size) // 4
            res_ids = list(struct.unpack_from(f"<{n}I", data, off + header_size))
        elif ctype == _RES_XML_START_ELEMENT:
            body = off + header_size
            _ns, name, attr_start, attr_size, attr_count = struct.unpack_from(
                "<IIHHH", data, body
            )
            attrs: dict[str, str] = {}
            a = body + attr_start
            for _ in range(attr_count):
                a_ns, a_name, a_raw, _sz, _r, dtype, value = struct.unpack_from(
                    "<IIIHBBI", data, a
                )
                key = strings[a_name] if a_name < len(strings) else ""
                if not key and a_name < len(res_ids):
                    key = _ANDROID_ATTR_IDS.get(res_ids[a_name], f"0x{res_ids[a_name]:08x}")
                if a_ns != 0xFFFFFFFF and a_ns < len(strings) and strings[a_ns] == _ANDROID_NS:
                    key = f"android:{key}"
                if a_raw != 0xFFFFFFFF and a_raw < len(strings):
                    attrs[key] = strings[a_raw]
                else:
                    attrs[key] = _typed_value(dtype, value, strings)
                a += attr_size or 20
            tag = strings[name] if name < len(strings) else "?"
            elements.append(XmlElement(tag, attrs, depth))
            depth += 1
        elif ctype == _RES_XML_START_ELEMENT + 1:  # end element
            depth = max(0, depth - 1)
        off += size
    return elements


def axml_to_text(elements: list[XmlElement]) -> str:
    """Readable XML. Every element is self-closed on its own line; nesting is
    shown by indentation (enough to read a manifest, cheap to produce)."""
    lines = []
    for el in elements:
        attrs = "".join(f" {k}={quoteattr(v)}" for k, v in el.attrs.items())
        lines.append(f"{'  ' * el.depth}<{el.tag}{attrs}/>")
    return "\n".join(lines)


@dataclass
class ManifestSummary:
    package: str = ""
    version_name: str = ""
    version_code: str = ""
    min_sdk: str = ""
    target_sdk: str = ""
    debuggable: bool = False
    permissions: list[str] = field(default_factory=list)
    launcher: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    receivers: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    meta_data: list[str] = field(default_factory=list)
    schemes: list[str] = field(default_factory=list)


def _a(attrs: dict[str, str], name: str) -> str:
    return attrs.get(f"android:{name}") or attrs.get(name) or ""


def summarize_manifest(elements: list[XmlElement]) -> ManifestSummary:
    s = ManifestSummary()
    component: tuple[str, str] | None = None  # (kind, name) of current component
    component_depth = -1
    actions: set[str] = set()
    categories: set[str] = set()

    def close_component() -> None:
        if component and component[0] in ("activity", "activity-alias"):
            if "android.intent.action.MAIN" in actions and \
                    "android.intent.category.LAUNCHER" in categories:
                s.launcher.append(component[1])

    for el in elements:
        if component and el.depth <= component_depth:
            close_component()
            component, actions, categories = None, set(), set()
        a = el.attrs
        if el.tag == "manifest":
            s.package = a.get("package", "")
            s.version_name = _a(a, "versionName")
            s.version_code = _a(a, "versionCode")
        elif el.tag == "uses-sdk":
            s.min_sdk = _a(a, "minSdkVersion")
            s.target_sdk = _a(a, "targetSdkVersion")
        elif el.tag in ("uses-permission", "uses-permission-sdk-23"):
            s.permissions.append(_a(a, "name"))
        elif el.tag == "application":
            s.debuggable = _a(a, "debuggable") == "true"
        elif el.tag in ("activity", "activity-alias", "service", "receiver", "provider"):
            name = _a(a, "name")
            if name.startswith(".") and s.package:
                name = s.package + name
            {"activity": s.activities, "activity-alias": s.activities,
             "service": s.services, "receiver": s.receivers,
             "provider": s.providers}[el.tag].append(name)
            component, component_depth = (el.tag, name), el.depth
        elif el.tag == "action":
            actions.add(_a(a, "name"))
        elif el.tag == "category":
            categories.add(_a(a, "name"))
        elif el.tag == "data" and _a(a, "scheme"):
            scheme = _a(a, "scheme")
            host = _a(a, "host")
            s.schemes.append(f"{scheme}://{host}" if host else f"{scheme}:")
        elif el.tag == "meta-data":
            name, value = _a(a, "name"), _a(a, "value")
            s.meta_data.append(f"{name}={value}" if value else name)
    close_component()
    s.schemes = sorted(set(s.schemes))
    return s


# ---------------------------------------------------------------------------
# DEX — class names and strings
# ---------------------------------------------------------------------------


def _uleb128(data: bytes, p: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = data[p]
        p += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, p
        shift += 7


@dataclass
class DexInfo:
    strings: list[str]
    classes: list[str]  # defined in this dex, dotted names


def parse_dex(data: bytes) -> DexInfo:
    if not data.startswith(b"dex\n"):
        raise ValueError("not a dex file")
    (str_n, str_off, type_n, type_off) = struct.unpack_from("<IIII", data, 0x38)
    (cls_n, cls_off) = struct.unpack_from("<II", data, 0x60)
    strings: list[str] = []
    for i in range(str_n):
        p = struct.unpack_from("<I", data, str_off + 4 * i)[0]
        try:
            _, p = _uleb128(data, p)
            end = data.index(b"\0", p)
            strings.append(data[p:end].decode("utf-8", errors="replace"))
        except (IndexError, ValueError):
            strings.append("")
    types = [
        strings[struct.unpack_from("<I", data, type_off + 4 * i)[0]]
        for i in range(type_n)
    ]
    classes = []
    for i in range(cls_n):
        idx = struct.unpack_from("<I", data, cls_off + 32 * i)[0]
        desc = types[idx] if idx < len(types) else ""
        if desc.startswith("L") and desc.endswith(";"):
            classes.append(desc[1:-1].replace("/", "."))
    return DexInfo(strings, classes)


# ---------------------------------------------------------------------------
# What the app is built with
# ---------------------------------------------------------------------------

# (label, regex over file paths in the APK)
_ENGINE_FILES: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(rx))
    for label, rx in [
        ("Unity (IL2CPP)", r"lib/[^/]+/libil2cpp\.so$|assets/bin/Data/Managed/Metadata/global-metadata\.dat$"),
        ("Unity (Mono)", r"assets/bin/Data/Managed/[^/]+\.dll$"),
        ("Unity", r"lib/[^/]+/libunity\.so$|assets/bin/Data/data\.unity3d$"),
        ("Unreal Engine", r"lib/[^/]+/libUE[45]\.so$|lib/[^/]+/libUnreal\.so$|\.pak$"),
        ("Godot", r"lib/[^/]+/libgodot_android\.so$|assets/[^/]+\.pck$"),
        ("LÖVE (Lua) — game.love is a zip of Lua source", r"lib/[^/]+/liblove\.so$|\.love$"),
        ("Flutter", r"lib/[^/]+/libflutter\.so$|assets/flutter_assets/"),
        ("Flutter AOT (Dart code in libapp.so)", r"lib/[^/]+/libapp\.so$"),
        ("React Native", r"lib/[^/]+/libreactnativejni\.so$|assets/index\.android\.bundle$"),
        ("Hermes JS bytecode", r"lib/[^/]+/libhermes\.so$"),
        ("Cocos2d-x", r"lib/[^/]+/libcocos2d\w*\.so$|lib/[^/]+/libcocos\w*\.so$"),
        ("GameMaker", r"lib/[^/]+/libyoyo\.so$|assets/game\.droid$"),
        ("Defold", r"lib/[^/]+/libdmengine\w*\.so$"),
        ("Xamarin / .NET", r"lib/[^/]+/libmonodroid\.so$|assemblies/[^/]+\.dll$"),
        ("Cordova / Capacitor (web app)", r"assets/www/index\.html$|assets/public/index\.html$"),
        ("Ren'Py", r"lib/[^/]+/librenpython\.so$|assets/x-game/"),
        ("Solar2D / Corona", r"lib/[^/]+/libcorona\.so$"),
        ("SDL", r"lib/[^/]+/libSDL2\.so$"),
        ("libGDX", r"lib/[^/]+/libgdx\.so$"),
        ("Lua scripting", r"lib/[^/]+/lib(lua|luajit)[\w.-]*\.so$"),
    ]
]

# (label, class-name prefix)
_KNOWN_SDKS: list[tuple[str, str]] = [
    ("Unity player", "com.unity3d.player"),
    ("Unity Ads", "com.unity3d.ads"),
    ("LÖVE", "org.love2d"),
    ("SDL", "org.libsdl"),
    ("Godot", "org.godotengine"),
    ("Unreal Engine", "com.epicgames"),
    ("Cocos2d-x", "org.cocos2dx"),
    ("libGDX", "com.badlogic.gdx"),
    ("GameMaker", "com.yoyogames"),
    ("Flutter", "io.flutter"),
    ("React Native", "com.facebook.react"),
    ("Jetpack Compose", "androidx.compose"),
    ("Kotlin", "kotlin."),
    ("Firebase", "com.google.firebase"),
    ("Google Play Games", "com.google.android.gms.games"),
    ("Google Play Billing", "com.android.billingclient"),
    ("Play Integrity / SafetyNet", "com.google.android.play.core.integrity"),
    ("AdMob", "com.google.android.gms.ads"),
    ("AppLovin", "com.applovin"),
    ("ironSource", "com.ironsource"),
    ("Vungle", "com.vungle"),
    ("Chartboost", "com.chartboost"),
    ("Facebook SDK", "com.facebook.FacebookSdk"),
    ("AppsFlyer", "com.appsflyer"),
    ("Adjust", "com.adjust.sdk"),
    ("OkHttp", "okhttp3"),
    ("Retrofit", "retrofit2"),
    ("Gson", "com.google.gson"),
    ("Protobuf", "com.google.protobuf"),
    ("gRPC", "io.grpc"),
    ("Socket.IO", "io.socket"),
    ("Photon (multiplayer)", "de.exitgames"),
    ("PlayFab", "com.playfab"),
    ("Promon Shield (anti-tamper)", "no.promon"),
    ("AppSealing (anti-tamper)", "com.inka.appsealing"),
    ("Tencent anti-cheat", "com.tencent.tp"),
    ("NetEase protection", "com.netease.nis"),
    ("SecNeo / Bangcle packer", "com.secneo"),
    ("Jiagu packer", "com.stub.StubApp"),
]


@dataclass
class ApkReport:
    files: list[tuple[str, str, int]]  # (apk name, path, size)
    manifest: ManifestSummary | None
    engines: list[str]
    sdks: list[str]
    class_count: int
    top_packages: list[tuple[str, int]]
    obfuscated_share: float
    native_libs: list[str]
    abis: list[str]

    def to_text(self) -> str:
        m = self.manifest
        out: list[str] = []
        if m:
            out.append(
                f"{m.package} {m.version_name} (code {m.version_code}) · "
                f"minSdk {m.min_sdk or '?'} · targetSdk {m.target_sdk or '?'}"
                + (" · DEBUGGABLE" if m.debuggable else "")
            )
        out.append("Built with: " + (", ".join(self.engines) or "plain Android (Java/Kotlin)"))
        if self.sdks:
            out.append("Libraries/SDKs: " + ", ".join(self.sdks))
        apks = Counter(a for a, _, _ in self.files)
        total = sum(sz for _, _, sz in self.files)
        out.append(
            f"Files: {len(self.files)} across {len(apks)} apk(s), "
            f"{total / 1e6:.1f} MB uncompressed"
        )
        if self.abis:
            out.append("Native ABIs: " + ", ".join(self.abis))
        if self.native_libs:
            out.append("Native libs: " + ", ".join(self.native_libs[:25])
                       + (" …" if len(self.native_libs) > 25 else ""))
        if self.class_count:
            out.append(
                f"Code: {self.class_count} classes"
                + (f", ~{self.obfuscated_share:.0%} with obfuscated (1-2 letter) names"
                   if self.obfuscated_share >= 0.05 else "")
            )
            out.append("Biggest packages: " + ", ".join(
                f"{p} ({n})" for p, n in self.top_packages))
        if m:
            if m.launcher:
                out.append("Launcher activity: " + ", ".join(m.launcher))
            out.append(f"Activities ({len(m.activities)}): "
                       + ", ".join(m.activities[:15]) + (" …" if len(m.activities) > 15 else ""))
            if m.services:
                out.append(f"Services ({len(m.services)}): " + ", ".join(m.services[:10]))
            if m.schemes:
                out.append("Deep links: " + ", ".join(m.schemes[:15]))
            if m.permissions:
                out.append("Permissions: " + ", ".join(
                    p.replace("android.permission.", "") for p in m.permissions))
            if m.meta_data:
                out.append("Meta-data: " + "; ".join(m.meta_data[:20]))
        return "\n".join(out)


def apk_files(version_dir: Path) -> list[Path]:
    """The APKs of one pulled version, base.apk first."""
    apks = sorted(version_dir.glob("*.apk"), key=lambda p: (p.name != "base.apk", p.name))
    return apks


def _short_package(cls: str) -> str:
    parts = cls.split(".")
    depth = 3 if len(parts) > 3 and parts[0] in ("com", "org", "net", "io", "de", "jp", "cn") else 2
    return ".".join(parts[:min(depth, len(parts) - 1)]) or "(default package)"


def analyze(version_dir: Path) -> ApkReport:
    files: list[tuple[str, str, int]] = []
    manifest: ManifestSummary | None = None
    classes: list[str] = []
    for apk in apk_files(version_dir):
        with zipfile.ZipFile(apk) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                files.append((apk.name, info.filename, info.file_size))
                if info.filename == "AndroidManifest.xml" and apk.name == "base.apk":
                    try:
                        manifest = summarize_manifest(parse_axml(z.read(info)))
                    except (ValueError, struct.error, IndexError):
                        manifest = None
                elif re.fullmatch(r"classes\d*\.dex", info.filename):
                    try:
                        classes.extend(parse_dex(z.read(info)).classes)
                    except (ValueError, struct.error, IndexError):
                        pass

    paths = [p for _, p, _ in files]
    engines = []
    for label, rx in _ENGINE_FILES:
        if any(rx.search(p) for p in paths):
            engines.append(label)
    sdks = [label for label, prefix in _KNOWN_SDKS
            if any(c.startswith(prefix) for c in classes)]
    pkgs = Counter(_short_package(c) for c in classes)
    obf = sum(
        1 for c in classes if re.fullmatch(r"[a-zA-Z]{1,2}(\$\w+)?", c.rsplit(".", 1)[-1])
    )
    libs = sorted({Path(p).name for p in paths if p.startswith("lib/") and p.endswith(".so")})
    abis = sorted({p.split("/")[1] for p in paths if p.startswith("lib/") and p.count("/") >= 2})
    return ApkReport(
        files=files,
        manifest=manifest,
        engines=engines,
        sdks=sdks,
        class_count=len(classes),
        top_packages=pkgs.most_common(12),
        obfuscated_share=(obf / len(classes)) if classes else 0.0,
        native_libs=libs,
        abis=abis,
    )


# ---------------------------------------------------------------------------
# Browsing, reading, searching
# ---------------------------------------------------------------------------


def _split_nested(path: str) -> list[str]:
    return [p.strip("/") for p in path.split("!/") if p.strip("/")]


def _open_member(version_dir: Path, path: str) -> tuple[str, bytes]:
    """Bytes of `path`: "assets/x.json", "split_config.arm64_v8a.apk:lib/…",
    or nested "assets/game.love!/main.lua". Returns (resolved name, data)."""
    parts = _split_nested(path)
    if not parts:
        raise FileNotFoundError("empty path")
    first = parts[0]
    apks = apk_files(version_dir)
    if ":" in first and first.split(":", 1)[0].endswith(".apk"):
        apk_name, first = first.split(":", 1)
        apks = [a for a in apks if a.name == apk_name]
    data = None
    where = ""
    for apk in apks:
        with zipfile.ZipFile(apk) as z:
            try:
                data = z.read(first)
                where = f"{apk.name}:{first}"
                break
            except KeyError:
                continue
    if data is None:
        raise FileNotFoundError(f"{first} isn't in this app")
    for inner in parts[1:]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = z.read(inner)
        except zipfile.BadZipFile as e:
            raise FileNotFoundError(f"{where} isn't an archive I can open") from e
        except KeyError as e:
            raise FileNotFoundError(f"{inner} isn't inside {where}") from e
        where += f"!/{inner}"
    return where, data


def _list_archive(data: bytes) -> list[tuple[str, int]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]


def list_files(version_dir: Path, under: str = "", pattern: str = "", limit: int = 200) -> str:
    """Files in the app (or inside a nested archive with "x.love!/")."""
    if "!/" in under or under.endswith((".love", ".zip", ".jar", ".obb")):
        outer, _, inner_prefix = under.partition("!/")
        where, data = _open_member(version_dir, outer)
        try:
            entries = [(f"{where}!/{n}", s) for n, s in _list_archive(data)
                       if n.startswith(inner_prefix)]
        except zipfile.BadZipFile:
            return f"{where} isn't a zip archive."
    else:
        entries = []
        for apk in apk_files(version_dir):
            with zipfile.ZipFile(apk) as z:
                for i in z.infolist():
                    if not i.is_dir() and i.filename.startswith(under):
                        name = i.filename if apk.name == "base.apk" else f"{apk.name}:{i.filename}"
                        entries.append((name, i.file_size))
    if pattern:
        rx = re.compile(fnmatch_to_regex(pattern), re.I)
        entries = [e for e in entries if rx.search(e[0])]
    if not entries:
        return "No files match."
    shown = entries[:limit]
    lines = [f"{n}  ({_size(s)})" for n, s in shown]
    if len(entries) > limit:
        lines.append(f"… {len(entries) - limit} more (narrow with under= or pattern=)")
    return f"{len(entries)} file(s):\n" + "\n".join(lines)


def fnmatch_to_regex(pattern: str) -> str:
    """Glob if it looks like one ("*.lua"), otherwise a plain substring."""
    if any(c in pattern for c in "*?["):
        import fnmatch

        return fnmatch.translate(pattern).replace(r"\Z", "$")
    return re.escape(pattern)


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _looks_text(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return True
    if b"\0" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # a multi-byte character cut at the sample edge is still text
        return e.start >= len(sample) - 4


def printable_strings(data: bytes, min_len: int = 5, limit: int = 300) -> list[str]:
    found = re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)
    return [s.decode("ascii") for s in found[:limit]]


def read_file(version_dir: Path, path: str, offset: int = 0, max_chars: int = 12000) -> str:
    """Text of a file inside the app; binary XML is decoded, dex summarised,
    other binaries shown as a hex head plus their readable strings."""
    max_chars = max(500, min(int(max_chars or 12000), MAX_READ_BYTES))
    if path.startswith("src:"):
        return _read_decompiled(version_dir, path[4:], offset, max_chars)
    where, data = _open_member(version_dir, path)
    name = where.rsplit("/", 1)[-1]
    if data[:2] == b"\x03\x00" and name.endswith(".xml"):
        try:
            text = axml_to_text(parse_axml(data))
            return _window(where + " (decoded binary XML)", text, offset, max_chars)
        except (ValueError, struct.error, IndexError):
            pass
    if data.startswith(b"dex\n"):
        dex = parse_dex(data)
        text = "\n".join(sorted(dex.classes))
        return _window(f"{where}: {len(dex.classes)} classes, {len(dex.strings)} strings "
                       f"(use apk_search for strings; jadx for real source)",
                       text, offset, max_chars)
    if data[:4] == b"PK\x03\x04":
        entries = _list_archive(data)
        listing = "\n".join(f"{n}  ({_size(s)})" for n, s in entries[:300])
        return (f"{where} is an archive with {len(entries)} files — read one with "
                f"path=\"{path}!/<file>\":\n{listing}")
    if _looks_text(data):
        return _window(where, data.decode("utf-8", errors="replace"), offset, max_chars)
    head = data[:256].hex(" ")
    strings = printable_strings(data)
    return (f"{where}: binary, {_size(len(data))}. First bytes: {head[:200]}…\n"
            f"Readable strings ({len(strings)} shown):\n" + "\n".join(strings)[:max_chars])


def _window(title: str, text: str, offset: int, max_chars: int) -> str:
    offset = max(0, int(offset or 0))
    chunk = text[offset:offset + max_chars]
    more = len(text) - (offset + len(chunk))
    tail = f"\n… {more} more chars (offset={offset + len(chunk)})" if more > 0 else ""
    return f"{title} [{len(text)} chars]\n{chunk}{tail}"


def decompiled_dir(version_dir: Path) -> Path:
    return version_dir / "jadx"


def _read_decompiled(version_dir: Path, rel: str, offset: int, max_chars: int) -> str:
    root = decompiled_dir(version_dir).resolve()
    target = (root / rel.lstrip("/")).resolve()
    if root not in target.parents and target != root:
        return "That path is outside the decompiled sources."
    if target.is_dir():
        kids = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return f"src:{rel} ({len(kids)} entries):\n" + "\n".join(kids[:300])
    if not target.is_file():
        return f"No decompiled file src:{rel}. Run apk_decompile first, or check the path."
    return _window(f"src:{rel}", target.read_text(errors="replace"), offset, max_chars)


def search(version_dir: Path, query: str, limit: int = 60) -> str:
    """Find `query` (regex, case-insensitive) in file names, the manifest,
    dex strings/class names, small text assets and decompiled sources."""
    try:
        rx = re.compile(query, re.I)
    except re.error:
        rx = re.compile(re.escape(query), re.I)
    hits: list[str] = []

    def add(s: str) -> bool:
        hits.append(s)
        return len(hits) >= limit

    for apk in apk_files(version_dir):
        prefix = "" if apk.name == "base.apk" else f"{apk.name}:"
        with zipfile.ZipFile(apk) as z:
            for info in z.infolist():
                fn = info.filename
                if rx.search(fn) and add(f"[file] {prefix}{fn}"):
                    return _hits(hits, limit)
                if fn == "AndroidManifest.xml":
                    try:
                        for line in axml_to_text(parse_axml(z.read(info))).splitlines():
                            if rx.search(line) and add(f"[manifest] {line.strip()[:200]}"):
                                return _hits(hits, limit)
                    except (ValueError, struct.error, IndexError):
                        pass
                elif re.fullmatch(r"classes\d*\.dex", fn):
                    try:
                        dex = parse_dex(z.read(info))
                    except (ValueError, struct.error, IndexError):
                        continue
                    for s in dex.strings:
                        if rx.search(s) and add(f"[{fn}] {s[:200]}"):
                            return _hits(hits, limit)
                elif info.file_size <= 2_000_000 and fn.endswith(_TEXT_EXT):
                    if _search_text(rx, f"{prefix}{fn}", z.read(info), add):
                        return _hits(hits, limit)
                elif fn.endswith(".love") and info.file_size <= 300_000_000:
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(info))) as inner:
                            for ii in inner.infolist():
                                where = f"{prefix}{fn}!/{ii.filename}"
                                if rx.search(ii.filename) and add(f"[file] {where}"):
                                    return _hits(hits, limit)
                                if (ii.file_size <= 2_000_000 and ii.filename.endswith(_TEXT_EXT)
                                        and _search_text(rx, where, inner.read(ii), add)):
                                    return _hits(hits, limit)
                    except zipfile.BadZipFile:
                        pass
    src = decompiled_dir(version_dir) / "sources"
    if src.is_dir():
        for f in sorted(src.rglob("*.java")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    rel = f.relative_to(decompiled_dir(version_dir))
                    if add(f"[src:{rel}:{n}] {line.strip()[:200]}"):
                        return _hits(hits, limit)
    return _hits(hits, limit)


_TEXT_EXT = (".json", ".txt", ".xml", ".lua", ".js", ".html", ".csv", ".yaml",
             ".yml", ".cfg", ".ini", ".properties", ".bundle", ".glsl", ".fs", ".vs")


def _search_text(rx: re.Pattern[str], where: str, data: bytes, add) -> bool:
    """Add matching lines; True once the hit limit is reached."""
    if not _looks_text(data):
        return False
    for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if rx.search(line) and add(f"[{where}:{n}] {line.strip()[:200]}"):
            return True
    return False


def _hits(hits: list[str], limit: int) -> str:
    if not hits:
        return "No matches."
    more = " (stopped at the limit — be more specific)" if len(hits) >= limit else ""
    return f"{len(hits)} match(es){more}:\n" + "\n".join(hits)
