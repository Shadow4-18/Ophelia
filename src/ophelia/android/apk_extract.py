"""Copy installed apps off the phone as APK files — her app library.

Works over whatever phone body is wired: ADB from a PC (``adb pull``), or on
the phone itself (Termux + Shizuku/rish), where the APK is read directly or
staged through shared storage. No root needed: installed APKs are readable
by the shell user.

Library layout (``~/.ophelia/apks``)::

    <package>/
      notes.md                  what she has worked out about the app
      <versionName>-<code>/
        base.apk
        split_config.arm64_v8a.apk ...   (split installs)
        obb/main.123.<package>.obb       (only with include_obb)
        info.json               package, version, files, sha256, pulled_at
        jadx/                   decompiled sources (apk_decompile)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ophelia.android import apk_inspect

log = structlog.get_logger()

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
NOTES_MAX_CHARS = 20_000
# Leave this much disk free after a pull.
DISK_HEADROOM = 500 * 1024 * 1024


def library_root() -> Path:
    home = Path(os.environ.get("OPHELIA_HOME") or (Path.home() / ".ophelia"))
    return home / "apks"


def valid_package(package: str) -> bool:
    return bool(PACKAGE_RE.match(package or ""))


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._") or "unknown"


# ---------------------------------------------------------------------------
# On the phone
# ---------------------------------------------------------------------------


@dataclass
class InstalledApp:
    package: str
    base_path: str


def parse_package_list(out: str) -> list[InstalledApp]:
    """`pm list packages -f` lines: package:/data/app/~~x==/com.a-y==/base.apk=com.a"""
    apps = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        path, sep, pkg = line[len("package:"):].rpartition("=")
        if sep and valid_package(pkg):
            apps.append(InstalledApp(pkg, path))
    return sorted(apps, key=lambda a: a.package)


def parse_pm_path(out: str) -> list[str]:
    return [
        line.strip()[len("package:"):]
        for line in (out or "").splitlines()
        if line.strip().startswith("package:") and line.strip().endswith(".apk")
    ]


def parse_version(dumpsys: str) -> tuple[str, str]:
    name = re.search(r"versionName=(\S+)", dumpsys or "")
    code = re.search(r"versionCode=(\d+)", dumpsys or "")
    return (name.group(1) if name else "", code.group(1) if code else "")


async def list_installed(android: Any, query: str = "", include_system: bool = False) -> list[InstalledApp]:
    flag = "" if include_system else " -3"
    out = await android.shell(f"pm list packages -f{flag}")
    apps = parse_package_list(out)
    if query:
        q = query.lower()
        apps = [a for a in apps if q in a.package.lower()]
    return apps


async def _remote_size(android: Any, path: str) -> int:
    out = await android.shell(f"stat -c %s {shlex.quote(path)}")
    m = re.search(r"\d+", out or "")
    return int(m.group(0)) if m else 0


async def _list_obb(android: Any, package: str) -> list[str]:
    d = f"/sdcard/Android/obb/{package}"
    out = await android.shell(f"ls -1 {d} 2>/dev/null")
    return [f"{d}/{n.strip()}" for n in (out or "").splitlines()
            if n.strip().endswith(".obb")]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


async def extract_app(
    android: Any,
    package: str,
    *,
    include_obb: bool = False,
    root: Path | None = None,
) -> str:
    """Pull every APK of an installed package into the library."""
    if not valid_package(package):
        return f"'{package}' doesn't look like a package name (e.g. com.example.game)."
    remote = parse_pm_path(await android.shell(f"pm path {package}"))
    if not remote:
        return (f"{package} isn't installed (or the phone shell can't see it). "
                f"Find it with apk_list_installed.")
    name, code = parse_version(
        await android.shell(f"dumpsys package {package} | grep -m2 -E 'versionName=|versionCode='")
    )
    obbs = await _list_obb(android, package) if include_obb else []

    sizes = {p: await _remote_size(android, p) for p in remote + obbs}
    root = root or library_root()
    root.mkdir(parents=True, exist_ok=True)
    need = sum(sizes.values())
    free = shutil.disk_usage(root).free
    if need and need + DISK_HEADROOM > free:
        return (f"Not enough space: {package} is {need / 1e6:.0f} MB and only "
                f"{free / 1e6:.0f} MB is free here.")

    dest = root / package / _safe_name(f"{name or 'unknown'}-{code or '0'}")
    dest.mkdir(parents=True, exist_ok=True)
    pulled: list[dict[str, Any]] = []
    for path in remote + obbs:
        local = dest / ("obb" if path in obbs else "") / _safe_name(Path(path).name)
        local.parent.mkdir(parents=True, exist_ok=True)
        err = await android.pull_file(path, local)
        if err:
            return f"Couldn't copy {path}: {err}"
        if sizes.get(path) and local.stat().st_size != sizes[path]:
            return (f"Copy of {path} is incomplete ({local.stat().st_size} of "
                    f"{sizes[path]} bytes). Try again.")
        pulled.append({
            "remote": path,
            "file": str(local.relative_to(dest)),
            "size": local.stat().st_size,
            "sha256": await asyncio.to_thread(_sha256, local),
        })

    info = {
        "package": package,
        "version_name": name,
        "version_code": code,
        "files": pulled,
        "source": "phone",
        "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (dest / "info.json").write_text(json.dumps(info, indent=2))
    log.info("apk.extracted", package=package, version=name, files=len(pulled), dir=str(dest))
    total = sum(f["size"] for f in pulled)
    splits = len([f for f in pulled if f["file"].endswith(".apk")])
    return (f"Extracted {package} {name} ({splits} apk file(s)"
            + (f" + {len(obbs)} obb" if obbs else "")
            + f", {total / 1e6:.1f} MB) to {dest}")


# ---------------------------------------------------------------------------
# Importing an APK that came some other way (sent to her, downloaded, …)
# ---------------------------------------------------------------------------


def import_file(src: Path, root: Path | None = None) -> str:
    """Add a local .apk, or an .apks/.xapk bundle (a zip of APKs), to the library."""
    src = src.expanduser()
    if not src.is_file():
        return f"No file at {src}."
    if not zipfile.is_zipfile(src):
        return f"{src.name} isn't an APK (not a zip archive)."
    root = root or library_root()
    staging = root / ".import" / _safe_name(src.stem)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "AndroidManifest.xml" in names:
            shutil.copyfile(src, staging / "base.apk")
        else:
            inner = [n for n in names if n.endswith(".apk")]
            if not inner:
                shutil.rmtree(staging, ignore_errors=True)
                return f"{src.name} has neither a manifest nor APKs inside."
            for n in inner:
                with z.open(n) as fin, (staging / _safe_name(Path(n).name)).open("wb") as fout:
                    shutil.copyfileobj(fin, fout)
            if not (staging / "base.apk").exists():
                # the one carrying the main manifest (splits name themselves split_*)
                main = next((p for p in staging.glob("*.apk")
                             if not p.name.startswith(("split_", "config."))), None)
                if main:
                    main.rename(staging / "base.apk")
    try:
        with zipfile.ZipFile(staging / "base.apk") as z:
            m = apk_inspect.summarize_manifest(apk_inspect.parse_axml(z.read("AndroidManifest.xml")))
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return f"Couldn't read the manifest of {src.name}: {e}"
    if not valid_package(m.package):
        shutil.rmtree(staging, ignore_errors=True)
        return f"{src.name}: manifest has no usable package name."
    dest = root / m.package / _safe_name(f"{m.version_name or 'unknown'}-{m.version_code or '0'}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(dest)
    files = [{"file": p.name, "size": p.stat().st_size, "sha256": _sha256(p)}
             for p in sorted(dest.glob("*.apk"))]
    (dest / "info.json").write_text(json.dumps({
        "package": m.package,
        "version_name": m.version_name,
        "version_code": m.version_code,
        "files": files,
        "source": str(src),
        "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))
    return f"Imported {m.package} {m.version_name} ({len(files)} apk file(s)) to {dest}"


IMPORTABLE_EXT = (".apk", ".apks", ".xapk")


def import_path(src: Path, root: Path | None = None) -> str:
    """import_file for one file, or every .apk/.apks/.xapk in a folder (such
    as the one the App Extractor app saves into)."""
    src = src.expanduser()
    if not src.is_dir():
        return import_file(src, root)
    files = sorted(f for f in src.iterdir() if f.is_file() and f.suffix.lower() in IMPORTABLE_EXT)
    if not files:
        return f"No .apk/.apks/.xapk files in {src}."
    return "\n".join(import_file(f, root) for f in files)


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------


def versions(package: str, root: Path | None = None) -> list[Path]:
    """Pulled versions of a package, newest pull first."""
    base = (root or library_root()) / package
    if not valid_package(package) or not base.is_dir():
        return []
    found = [d for d in base.iterdir() if (d / "info.json").is_file()]
    return sorted(found, key=lambda d: (d / "info.json").stat().st_mtime, reverse=True)


def resolve(package: str, version: str = "", root: Path | None = None) -> Path | None:
    vs = versions(package, root)
    if version:
        vs = [d for d in vs if d.name == version or d.name.startswith(version + "-")]
    return vs[0] if vs else None


def library_text(root: Path | None = None) -> str:
    root = root or library_root()
    if not root.is_dir():
        return "Your app library is empty. Pull one with apk_extract."
    lines = []
    for pkg_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        vs = versions(pkg_dir.name, root)
        if not vs:
            continue
        size = sum(f.stat().st_size for f in vs[0].glob("*.apk"))
        extras = []
        if (pkg_dir / "notes.md").is_file():
            extras.append("notes")
        if apk_inspect.decompiled_dir(vs[0]).is_dir():
            extras.append("decompiled")
        lines.append(
            f"- {pkg_dir.name}  {', '.join(v.name for v in vs)}  ({size / 1e6:.1f} MB)"
            + (f"  [{', '.join(extras)}]" if extras else "")
        )
    if not lines:
        return "Your app library is empty. Pull one with apk_extract."
    return f"App library ({root}):\n" + "\n".join(lines)


def notes_path(package: str, root: Path | None = None) -> Path:
    return (root or library_root()) / package / "notes.md"


def read_notes(package: str, root: Path | None = None) -> str:
    p = notes_path(package, root)
    return p.read_text(errors="replace") if valid_package(package) and p.is_file() else ""


def write_notes(package: str, text: str, *, replace: bool = False, root: Path | None = None) -> str:
    if not valid_package(package):
        return f"'{package}' doesn't look like a package name."
    p = notes_path(package, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = (text or "").strip()
    if replace:
        body = text + "\n"
    else:
        old = p.read_text(errors="replace") if p.is_file() else f"# {package}\n"
        body = old.rstrip() + f"\n\n## {time.strftime('%Y-%m-%d')}\n{text}\n"
    if len(body) > NOTES_MAX_CHARS:
        return (f"Notes would be {len(body)} chars (max {NOTES_MAX_CHARS}). "
                f"Condense them and save with replace=true.")
    p.write_text(body)
    return f"Saved notes for {package} ({len(body)} chars)."


# ---------------------------------------------------------------------------
# Decompiling (optional — needs jadx)
# ---------------------------------------------------------------------------


def find_jadx() -> str | None:
    return shutil.which("jadx")


async def decompile(version_dir: Path, timeout: float = 1800.0) -> str:
    jadx = find_jadx()
    if not jadx:
        return ("jadx isn't installed. PC: download it from "
                "https://github.com/skylot/jadx/releases and put bin/jadx on PATH. "
                "Termux: pkg install jadx. Until then apk_inspect / apk_search / "
                "apk_read still work on the raw app.")
    base = version_dir / "base.apk"
    out = apk_inspect.decompiled_dir(version_dir)
    shutil.rmtree(out, ignore_errors=True)
    proc = await asyncio.create_subprocess_exec(
        jadx, "--no-res", "--show-bad-code", "-d", str(out), str(base),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return f"jadx took longer than {timeout / 60:.0f} min and was stopped."
    sources = list((out / "sources").rglob("*.java")) if (out / "sources").is_dir() else []
    if not sources:
        tail = (stdout or b"").decode(errors="replace")[-600:]
        return f"jadx produced no sources (exit {proc.returncode}). {tail}"
    note = "" if proc.returncode == 0 else " (jadx reported errors on some classes — normal)"
    return (f"Decompiled {len(sources)} Java files to {out}/sources{note}. "
            f"Read them with apk_read path=\"src:sources/...\"; apk_search covers them now.")
