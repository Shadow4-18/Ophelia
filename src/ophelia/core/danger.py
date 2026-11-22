"""Phone commands that need the owner's OK before they run.

With root on her phone she can do anything, including things that can't be
undone or that take her offline. Those ask Okia first (core/owner_approval);
everything else runs as before. This is a list of the obviously dangerous,
not a sandbox: it catches a mistake, not a determined attempt.
"""

from __future__ import annotations

import re

# (pattern, what it would do). Checked against the command text.
_RULES: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), why) for p, why in [
    (r"\bdd\b[^\n;|&]*\bof=/dev/", "writes raw data straight to a disk partition"),
    (r"/dev/block/", "touches the phone's storage partitions directly"),
    (r"\b(fastboot|flash_image|flashall)\b", "flashes the phone's firmware"),
    (r"\bmkfs(\.\w+)?\b|\bblkdiscard\b|\bmke2fs\b|\bmake_ext4fs\b", "formats storage"),
    (r"\brm\s+(-\w*\s+)*-\w*[rR]\w*\s+(-\w+\s+)*(/|/\*|/(system|vendor|product|data|sdcard|"
     r"storage|apex|odm|metadata|persist|efs|mnt)(/(\*)?)?|/data/(data|adb|app|user|media)"
     r"(/\*?)?|/storage/emulated(/0)?(/\*?)?|~|\$HOME)(\s|$|;|&|\|)",
     "deletes a whole system or storage folder"),
    (r"\bmount\b[^\n;]*\b(rw|remount)\b|\bremount\b", "makes system partitions writable"),
    (r"\breboot\b|\bsvc\s+power\s+(reboot|shutdown)\b|\bsetprop\s+sys\.powerctl\b",
     "restarts or shuts down the phone (she goes offline)"),
    (r"\bam\s+broadcast\b[^\n;]*MASTER_CLEAR|\brecovery\s+--wipe|\bwipe\s+(data|cache|"
     r"userdata)\b|\bfactory[\s_-]?reset\b", "factory-resets the phone"),
    (r"\bpm\s+(uninstall|clear)\b|\bcmd\s+package\s+(uninstall|clear)\b",
     "uninstalls an app or erases its data"),
    (r"\bpm\s+disable(-user)?\b|\bcmd\s+package\s+disable",
     "disables an app (system apps included)"),
    (r"\bsetenforce\s+0\b", "turns off SELinux protection"),
    (r"\b(resetprop|setprop)\b", "changes system properties"),
    (r"\b(magisk|ksud|apd)\b[^\n;]*(\s--install-module|\smodule\s+install|\s--remove-modules|"
     r"\s--uninstall|\smodule\s+uninstall)\b", "installs or removes a root module"),
    (r"/data/adb\b", "changes the root setup itself (Magisk/KernelSU files)"),
    (r"\b(chmod|chown|chcon|mv|cp|tee|>|>>)\s*[^\n;]*\s(/system|/vendor|/product|/odm|/apex)/",
     "writes to the read-only system"),
    (r"\bsettings\s+put\s+(global|secure)\s+(adb_enabled|development_settings_enabled|"
     r"airplane_mode_on|package_verifier)", "changes a security or connectivity setting"),
    (r"\bkill(all)?\b[^\n;]*\b(system_server|zygote|init|surfaceflinger)\b|\bstop\s*$|"
     r"\bstop\s+(zygote|surfaceflinger)\b", "kills a core Android process"),
]]

# For run_code: only Python that reaches for a privileged shell is checked.
_PRIVILEGED = re.compile(r"""(?:["'\s(\[,]|^)(su|rish|tsu|sudo)(?:["'\s)\],]|$)|\bsu\s+-c\b""",
                         re.M)


def dangerous(command: str) -> str:
    """What makes ``command`` dangerous ("" when it isn't)."""
    text = command or ""
    for pattern, why in _RULES:
        if pattern.search(text):
            return why
    return ""


def dangerous_code(code: str) -> str:
    """run_code: dangerous only when the code runs a privileged shell and a
    dangerous command."""
    if not _PRIVILEGED.search(code or ""):
        return ""
    return dangerous(code)
