#!/data/data/com.termux/files/usr/bin/bash
# Shared Termux pip settings — source from other install scripts.
# PyPI manylinux wheels do not install on Termux; use TUR for pydantic-core etc.

TERMUX_PIP_EXTRA_INDEX="${TERMUX_PIP_EXTRA_INDEX:-https://termux-user-repository.github.io/pypi/}"
TERMUX_PYDANTIC_FALLBACK_INDEX="${TERMUX_PYDANTIC_FALLBACK_INDEX:-https://eutalix.github.io/android-pydantic-core/}"

export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-$(getprop ro.build.version.sdk 2>/dev/null || echo 28)}"

termux_pip_install() {
    python -m pip install --no-cache-dir --prefer-binary \
        --extra-index-url "$TERMUX_PIP_EXTRA_INDEX" \
        "$@"
}

termux_preinstall_native_wheels() {
    echo "  -> pydantic-core (Termux wheel index)..."
    if termux_pip_install pydantic-core pydantic; then
        return 0
    fi
    echo "  -> TUR failed; trying android-pydantic-core fallback..."
    python -m pip install --no-cache-dir --prefer-binary \
        --extra-index-url "$TERMUX_PYDANTIC_FALLBACK_INDEX" \
        pydantic-core pydantic
}
