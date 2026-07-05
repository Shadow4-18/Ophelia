#!/usr/bin/env python3
"""Patch espeak-rs-sys build.rs for Termux Android.

Kokoros only needs espeak-ng for phonemes, not live audio output. Termux may
have libpcaudio installed; espeak-ng then compiles with USE_LIBPCAUDIO but the
final koko link does not pull in -lpcaudio. Disable pcaudio on Android and
ensure libsonic is linked (wavegen uses sonic for speech rate).
"""
from __future__ import annotations

import sys

MARKER = 'target_os = "android") {\n        config.define("USE_LIBPCAUDIO", "OFF");'


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-espeak-rs-sys-build-rs.py PATH/TO/build.rs", file=sys.stderr)
        return 2

    path = sys.argv[1]
    text = open(path, encoding="utf-8").read()
    if MARKER in text:
        print(f"already patched: {path}")
        return 0

    macos_block = (
        '    if cfg!(target_os = "macos") {\n'
        '        config.define("USE_LIBPCAUDIO", "OFF");\n'
        "    }\n"
    )
    if macos_block not in text:
        print("ERROR: espeak-rs-sys macos USE_LIBPCAUDIO block not found", file=sys.stderr)
        return 1

    android_block = (
        macos_block
        + "\n"
        + "    if cfg!(target_os = \"android\") {\n"
        + '        config.define("USE_LIBPCAUDIO", "OFF");\n'
        + "    }\n"
    )
    text = text.replace(macos_block, android_block, 1)

    build_marker = "    let bindings_dir = config.build();\n"
    if build_marker not in text:
        print("ERROR: espeak-rs-sys bindings_dir assignment not found", file=sys.stderr)
        return 1

    android_link = """    let bindings_dir = config.build();

    if cfg!(target_os = "android") {
        println!("cargo:rerun-if-env-changed=OPHELIA_SONIC_LIB_DIR");
        println!("cargo:rustc-link-lib=c++_shared");
        let mut linked_sonic = false;
        if let Ok(dir) = std::env::var("OPHELIA_SONIC_LIB_DIR") {
            if !dir.is_empty() {
                println!("cargo:rustc-link-search=native={dir}");
                println!("cargo:rustc-link-lib=static=sonic");
                linked_sonic = true;
            }
        }
        if !linked_sonic {
            for entry in glob(&format!("{}/**/libsonic.a", out_dir.display())).unwrap() {
                if let Ok(path) = entry {
                    if let Some(parent) = path.parent() {
                        println!("cargo:rustc-link-search=native={}", parent.display());
                        println!("cargo:rustc-link-lib=static=sonic");
                        break;
                    }
                }
            }
        }
    }
"""
    text = text.replace(build_marker, android_link, 1)
    open(path, "w", encoding="utf-8").write(text)
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
