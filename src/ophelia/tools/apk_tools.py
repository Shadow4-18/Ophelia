"""App library tools: pull installed apps off the phone and look inside them.

Mixed into ToolRegistry; methods use its attributes (android, …). The heavy
lifting is in android/apk_extract.py and android/apk_inspect.py."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

from ophelia.android import apk_extract, apk_inspect

_PKG = {"type": "string", "description": "Package name, e.g. com.playstack.balatro.android"}
_VERSION = {"type": "string", "description": "Pulled version folder (default: newest)"}

APK_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "apk_list",
            "description": (
                "List apps. source=phone: apps installed on the phone (package names "
                "to pull). source=library (default): apps you've already pulled, with "
                "versions and whether you have notes on them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["library", "phone"]},
                    "query": {"type": "string", "description": "Filter by package name substring"},
                    "include_system": {"type": "boolean", "description": "phone: system apps too"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_extract",
            "description": (
                "Copy an installed app off the phone into your app library (base APK + "
                "split APKs) so you can study how it works. include_obb also copies its "
                "big game-data files. Or path= to add an .apk/.apks/.xapk file you were sent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": _PKG,
                    "include_obb": {"type": "boolean"},
                    "path": {"type": "string", "description": "Local .apk/.apks/.xapk file to import"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_inspect",
            "description": (
                "Overview of a pulled app: what engine it's built with (Unity, Godot, "
                "LÖVE, Flutter, …), libraries/SDKs, code size and obfuscation, launcher "
                "activity, screens, deep links, permissions. Start here."
            ),
            "parameters": {
                "type": "object",
                "properties": {"package": _PKG, "version": _VERSION},
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_files",
            "description": (
                "List files inside a pulled app. under=\"assets/\" for a folder, "
                "pattern=\"*.lua\" to filter; under=\"assets/game.love!/\" lists inside a "
                "nested archive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": _PKG,
                    "under": {"type": "string"},
                    "pattern": {"type": "string"},
                    "version": _VERSION,
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_read",
            "description": (
                "Read a file inside a pulled app: path=\"AndroidManifest.xml\" (decoded), "
                "\"assets/config.json\", nested \"assets/game.love!/main.lua\", a split "
                "\"split_config.arm64_v8a.apk:lib/arm64-v8a/libgame.so\" (binary: shows "
                "its readable strings), or decompiled \"src:sources/com/x/Y.java\". "
                "Long files: continue with offset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": _PKG,
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                    "version": _VERSION,
                },
                "required": ["package", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_search",
            "description": (
                "Search a pulled app (regex, case-insensitive): file names, manifest, "
                "code strings and class names, text assets, Lua inside .love games, "
                "and decompiled sources if you have them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": _PKG,
                    "query": {"type": "string"},
                    "version": _VERSION,
                },
                "required": ["package", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_decompile",
            "description": (
                "Decompile a pulled app's Java/Kotlin code to readable source with jadx "
                "(slow: minutes). Then apk_read path=\"src:...\" and apk_search cover it."
            ),
            "parameters": {
                "type": "object",
                "properties": {"package": _PKG, "version": _VERSION},
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apk_notes",
            "description": (
                "Your notebook on an app. Without text: read it. With text: add what you "
                "worked out (how screens/menus work, element ids, which screens are "
                "drawn by the engine, rules found in its code). When you play that game, "
                "these notes are shown with the screen. replace=true rewrites it whole."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": _PKG,
                    "text": {"type": "string"},
                    "replace": {"type": "boolean"},
                },
                "required": ["package"],
            },
        },
    },
]


class ApkToolsMixin:
    def _apk_version_dir(self, package: str, version: str = "") -> tuple[Path | None, str]:
        if not apk_extract.valid_package(package):
            return None, f"'{package}' doesn't look like a package name."
        d = apk_extract.resolve(package, version)
        if d is None:
            have = apk_extract.versions(package)
            if have:
                return None, f"No version '{version}' of {package}. Have: " + ", ".join(
                    v.name for v in have)
            return None, (f"{package} isn't in your app library yet. Pull it with "
                          f"apk_extract package={package}.")
        return d, ""

    async def _apk_list(self, source: str = "library", query: str = "",
                        include_system: bool = False) -> str:
        if source != "phone":
            return apk_extract.library_text()
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        apps = await apk_extract.list_installed(self.android, query, include_system)
        if not apps:
            return "No matching apps installed."
        shown = apps[:150]
        more = f"\n… {len(apps) - 150} more (filter with query=)" if len(apps) > 150 else ""
        return f"{len(apps)} app(s) on the phone:\n" + "\n".join(
            a.package for a in shown) + more

    async def _apk_extract(self, package: str = "", include_obb: bool = False,
                           path: str = "") -> str:
        if path:
            return await asyncio.to_thread(apk_extract.import_file, Path(path))
        if not package:
            return "Say which app: package=... (apk_list source=phone lists them) or path=."
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await apk_extract.extract_app(self.android, package, include_obb=include_obb)

    async def _apk_inspect(self, package: str, version: str = "") -> str:
        d, err = self._apk_version_dir(package, version)
        if not d:
            return err
        try:
            report = await asyncio.to_thread(apk_inspect.analyze, d)
        except zipfile.BadZipFile as e:
            return f"An APK in {d} is damaged ({e}). Pull it again."
        text = report.to_text()
        notes = apk_extract.read_notes(package)
        if notes:
            text += f"\n\nYou have notes on this app ({len(notes)} chars): apk_notes to read."
        return text

    async def _apk_files(self, package: str, under: str = "", pattern: str = "",
                         version: str = "") -> str:
        d, err = self._apk_version_dir(package, version)
        if not d:
            return err
        try:
            return await asyncio.to_thread(apk_inspect.list_files, d, under, pattern)
        except FileNotFoundError as e:
            return str(e)

    async def _apk_read(self, package: str, path: str, offset: int = 0,
                        max_chars: int = 12000, version: str = "") -> str:
        d, err = self._apk_version_dir(package, version)
        if not d:
            return err
        try:
            return await asyncio.to_thread(apk_inspect.read_file, d, path, offset, max_chars)
        except FileNotFoundError as e:
            return f"{e}. List files with apk_files."

    async def _apk_search(self, package: str, query: str, version: str = "") -> str:
        d, err = self._apk_version_dir(package, version)
        if not d:
            return err
        if not (query or "").strip():
            return "Give a query."
        return await asyncio.to_thread(apk_inspect.search, d, query)

    async def _apk_decompile(self, package: str, version: str = "") -> str:
        d, err = self._apk_version_dir(package, version)
        if not d:
            return err
        return await apk_extract.decompile(d)

    async def _apk_notes(self, package: str, text: str = "", replace: bool = False) -> str:
        if not text.strip():
            notes = apk_extract.read_notes(package)
            return notes or f"No notes on {package} yet."
        return apk_extract.write_notes(package, text, replace=replace)
