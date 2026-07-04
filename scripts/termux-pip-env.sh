#!/data/data/com.termux/files/usr/bin/bash
# Shared Termux pip settings — source from other install scripts.
# PyPI manylinux wheels do not install on Termux; use TUR for pydantic-core etc.

TERMUX_PIP_EXTRA_INDEX="${TERMUX_PIP_EXTRA_INDEX:-https://termux-user-repository.github.io/pypi/}"
TERMUX_PYDANTIC_FALLBACK_INDEX="${TERMUX_PYDANTIC_FALLBACK_INDEX:-https://eutalix.github.io/android-pydantic-core/}"

export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-$(getprop ro.build.version.sdk 2>/dev/null || echo 28)}"

# rustup breaks Termux's patched rustc (core/std rlib not found).
termux_fix_rust_path() {
    if [[ -d "$HOME/.cargo/bin" ]] && [[ ":${PATH}:" == *":$HOME/.cargo/bin:"* ]]; then
        echo "WARNING: ~/.cargo/bin is on PATH (rustup). Removing from this session."
        export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v "$HOME/.cargo/bin" | paste -sd: -)"
    fi
    if [[ -f "$HOME/.cargo/env" ]] && grep -q 'cargo/bin' "$HOME/.cargo/env" 2>/dev/null; then
        echo "TIP: comment out the PATH line in ~/.cargo/env if rustc fails after pkg upgrade."
    fi

    arch="$(uname -m)"
    std_pkg=""
    case "$arch" in
        aarch64) std_pkg="rust-std-aarch64-linux-android" ;;
        arm)     std_pkg="rust-std-arm-linux-androideabi" ;;
        i686)    std_pkg="rust-std-i686-linux-android" ;;
        x86_64)  std_pkg="rust-std-x86_64-linux-android" ;;
    esac
    if [[ -n "$std_pkg" ]]; then
        pkg install -y rust "$std_pkg" binutils clang 2>/dev/null || true
    fi
}

termux_python_minor() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

# Termux main repo may ship Python 3.14 before Android pydantic-core wheels exist.
# Prefer 3.11–3.13 from TUR when the default interpreter is too new.
termux_resolve_python() {
    local py="python"
    local minor
    minor="$(termux_python_minor python 2>/dev/null || echo 0.0)"

    if [[ "$minor" == "3.14" ]] || [[ "$minor" == "3.15" ]]; then
        for candidate in python3.13 python3.12 python3.11; do
            if command -v "$candidate" &>/dev/null; then
                echo "Using $candidate (Termux default python is $minor — no pydantic-core wheels yet)" >&2
                py="$candidate"
                break
            fi
        done
    fi

    if [[ "$py" == "python" ]] && { [[ "$minor" == "3.14" ]] || [[ "$minor" == "3.15" ]]; }; then
        cat >&2 <<'EOF'
ERROR: Termux Python 3.14+ cannot install pydantic-core yet (no Android wheels; source builds fail).

Install Python 3.13 from TUR, then re-run this script:

  pkg install tur-repo
  pkg install python3.13
  python3.13 -m pip install -U pip setuptools wheel
  cd ~/Ophelia
  PYTHON=python3.13 bash scripts/termux-repair.sh

Or temporarily use: python3.13 -m ophelia run
EOF
        return 1
    fi

    echo "$py"
}

termux_pip_install() {
    local py="${TERMUX_PYTHON:-python}"
    "$py" -m pip install --no-cache-dir --prefer-binary \
        --extra-index-url "$TERMUX_PIP_EXTRA_INDEX" \
        "$@"
}

termux_ensure_python313() {
    local minor
    minor="$(termux_python_minor python 2>/dev/null || echo 0.0)"
    if [[ "$minor" != "3.14" && "$minor" != "3.15" ]]; then
        return 0
    fi
    if command -v python3.13 &>/dev/null; then
        return 0
    fi
    echo "Python 3.14 detected — installing Python 3.13 from TUR (required for pydantic-core)..."
    pkg install -y tur-repo
    pkg install -y python3.13
}

termux_install_ophelia_wrapper() {
    local py="$1"
    local bindir="${HOME}/.local/bin"
    mkdir -p "$bindir"
    cat >"$bindir/ophelia" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec $py -m ophelia "\$@"
EOF
    chmod +x "$bindir/ophelia"
    echo "  Installed wrapper: $bindir/ophelia -> $py -m ophelia"
    if [[ ":${PATH}:" != *":$bindir:"* ]]; then
        echo "  Add to ~/.bashrc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

termux_preinstall_native_wheels() {
    local py="${TERMUX_PYTHON:-python}"
    local minor
    minor="$("$py" -c 'import sys; print(sys.version_info.minor)')"

    echo "  -> pydantic-core for Python 3.${minor} (prebuilt wheels only)..."

    # Never compile pydantic-core on Termux — it fails with rlib / maturin errors.
    if "$py" -m pip install --no-cache-dir --prefer-binary --only-binary=:all: \
        --extra-index-url "$TERMUX_PIP_EXTRA_INDEX" \
        "pydantic-core" "pydantic>=2.10"; then
        return 0
    fi

    echo "  -> TUR had no wheel; trying android-pydantic-core index..."
    if "$py" -m pip install --no-cache-dir --prefer-binary --only-binary=:all: \
        --extra-index-url "$TERMUX_PYDANTIC_FALLBACK_INDEX" \
        "pydantic-core" "pydantic>=2.10"; then
        return 0
    fi

    cat >&2 <<EOF
ERROR: No prebuilt pydantic-core wheel for Python 3.${minor} on this device.

If you are on Python 3.14+, install Python 3.13 from TUR (see termux_resolve_python error above).
Otherwise check: https://github.com/Eutalix/android-pydantic-core/releases
EOF
    return 1
}
