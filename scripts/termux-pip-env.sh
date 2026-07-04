#!/data/data/com.termux/files/usr/bin/bash
# Shared Termux pip settings — source from other install scripts.
# PyPI manylinux wheels do not install on Termux; use TUR for pydantic-core etc.

TERMUX_PIP_EXTRA_INDEX="${TERMUX_PIP_EXTRA_INDEX:-https://termux-user-repository.github.io/pypi/}"
TERMUX_PYDANTIC_FALLBACK_INDEX="${TERMUX_PYDANTIC_FALLBACK_INDEX:-https://eutalix.github.io/android-pydantic-core/}"
TERMUX_PYDANTIC_INSTALLER="${TERMUX_PYDANTIC_INSTALLER:-https://raw.githubusercontent.com/Eutalix/android-pydantic-core/main/install_pydantic_core.sh}"

# Maturin on Termux expects API 24 for cross-target builds (not the device SDK).
export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-24}"

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

    if [[ -x "${PREFIX:-/data/data/com.termux/files/usr}/bin/rustc" ]]; then
        export RUSTC="${PREFIX}/bin/rustc"
        export CARGO="${PREFIX}/bin/cargo"
    fi
}

termux_python_minor() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

# TUR mirrors differ: some ship python3.11 binaries, others only python-is-python3.11 meta-packages.
termux_tur_python_bins() {
    printf '%s\n' python3.12 python3.11 python3.10 python python3
}

termux_install_tur_python() {
    echo "Trying to install an older Python from TUR (for pydantic-core wheels)..."
    pkg install -y tur-repo 2>/dev/null || true
    pkg update -y 2>/dev/null || true

    # Meta-packages: switch default `python` to 3.11 / 3.10 (seen on many TUR mirrors).
    local meta
    for meta in python-is-python3.11 python-is-python3.10; do
        echo "  -> pkg install $meta ..."
        if pkg install -y "$meta" 2>/dev/null; then
            if python --version 2>&1 | grep -qE '3\.(10|11)\.'; then
                echo "  Default python is now: $(python --version 2>&1)"
                return 0
            fi
        fi
    done

    # Standalone versioned interpreters (other mirrors).
    local pkg bin
    for pkg in python3.11 python3.10 python3.12; do
        echo "  -> pkg install $pkg ..."
        if pkg install -y "$pkg" 2>/dev/null; then
            bin="${pkg}"
            if command -v "$bin" &>/dev/null; then
                echo "  Installed $bin ($("$bin" --version 2>&1))"
                return 0
            fi
        fi
    done

    echo "  No compatible TUR Python found."
    echo "  Run: pkg search python | grep -E 'python-is-python|python3\\.'"
    return 1
}

termux_ensure_compatible_python() {
    local minor
    minor="$(termux_python_minor python 2>/dev/null || echo 0.0)"
    # Need older Python when default is 3.13+ (no pydantic-core wheels for 3.14 yet; 3.13 often lacks wheels too).
    if [[ "$minor" == "3.10" ]] || [[ "$minor" == "3.11" ]] || [[ "$minor" == "3.12" ]]; then
        return 0
    fi

    local bin
    for bin in python3.11 python3.10 python3.12; do
        if command -v "$bin" &>/dev/null; then
            return 0
        fi
    done

    termux_install_tur_python || true
}

# Prefer a Python version with pydantic-core wheels (3.10–3.12).
termux_resolve_python() {
    local py="python"
    local minor
    minor="$(termux_python_minor python 2>/dev/null || echo 0.0)"

    if [[ "$minor" == "3.10" ]] || [[ "$minor" == "3.11" ]] || [[ "$minor" == "3.12" ]]; then
        echo "$py"
        return 0
    fi

    local candidate
    for candidate in python3.11 python3.10 python3.12; do
        if command -v "$candidate" &>/dev/null; then
            echo "Using $candidate (default python is $minor)" >&2
            echo "$candidate"
            return 0
        fi
    done

    echo "$py"
}

termux_pip_install() {
    local py="${TERMUX_PYTHON:-python}"
    "$py" -m pip install --no-cache-dir --prefer-binary \
        --extra-index-url "$TERMUX_PIP_EXTRA_INDEX" \
        "$@"
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

    echo "  -> pydantic-core for Python 3.${minor} (prebuilt wheels first)..."

    if "$py" -m pip install --no-cache-dir --prefer-binary --only-binary=:all: \
        --extra-index-url "$TERMUX_PIP_EXTRA_INDEX" \
        "pydantic-core" "pydantic>=2.10"; then
        return 0
    fi

    echo "  -> TUR index had no wheel; trying android-pydantic-core..."
    if "$py" -m pip install --no-cache-dir --prefer-binary --only-binary=:all: \
        --extra-index-url "$TERMUX_PYDANTIC_FALLBACK_INDEX" \
        "pydantic-core" "pydantic>=2.10"; then
        return 0
    fi

    echo "  -> Trying Eutalix auto-installer script..."
    local _pydantic_installer="/tmp/ophelia-install-pydantic-core.sh"
    if curl -fsSL "$TERMUX_PYDANTIC_INSTALLER" -o "$_pydantic_installer"; then
        chmod +x "$_pydantic_installer"
        # Installer uses python3/pip from PATH — prefer our chosen interpreter.
        local _pydir
        _pydir="$(dirname "$(command -v "$py")")"
        if PATH="$_pydir:$PATH" bash "$_pydantic_installer"; then
            "$py" -m pip install --no-cache-dir "pydantic>=2.10" && return 0
        fi
    fi

    # Python 3.14+: no Android wheels yet — compile from source with Termux rustc.
    if [[ "$minor" -ge 14 ]]; then
        echo "  -> No wheel for 3.${minor}; compiling pydantic-core (slow, ~10 min)..."
        termux_fix_rust_path
        "$py" -m pip install -U setuptools wheel maturin 2>/dev/null || true
        if "$py" -m pip install --no-cache-dir "pydantic-core" "pydantic>=2.10"; then
            return 0
        fi
    fi

    cat >&2 <<EOF
ERROR: Could not install pydantic-core for Python 3.${minor}.

If you are on Python 3.14+, install an older Python from TUR first:

  pkg install tur-repo
  pkg install python-is-python3.11    # or python-is-python3.10
  pkg search python | grep -E 'python-is-python|python3\.'
  bash scripts/termux-repair.sh

Wheels: https://github.com/Eutalix/android-pydantic-core/releases (Python 3.9–3.13 only)
EOF
    return 1
}
