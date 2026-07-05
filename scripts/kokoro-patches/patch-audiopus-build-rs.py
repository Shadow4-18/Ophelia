#!/usr/bin/env python3
"""Patch audiopus_sys build.rs for Termux Android (Lakelezz/audiopus_sys#15)."""
from __future__ import annotations

import re
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-audiopus-build-rs.py PATH/TO/build.rs", file=sys.stderr)
        return 2

    path = sys.argv[1]
    text = open(path, encoding="utf-8").read()
    if 'target_os = "android"' in text:
        print(f"already patched: {path}")
        return 0

    pat = r"fn default_library_linking\(\) -> bool \{.*?\n\}"
    m = re.search(pat, text, flags=re.DOTALL)
    if not m:
        print("ERROR: audiopus_sys build.rs layout changed — cannot patch", file=sys.stderr)
        return 1

    block = m.group(0)
    if not block.rstrip().endswith("}"):
        print("ERROR: unexpected default_library_linking block", file=sys.stderr)
        return 1

    fixed = (
        block[:-1]
        + """    #[cfg(target_os = "android")]
    {
        false
    }
}
"""
    )
    open(path, "w", encoding="utf-8").write(text[: m.start()] + fixed + text[m.end() :])
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
