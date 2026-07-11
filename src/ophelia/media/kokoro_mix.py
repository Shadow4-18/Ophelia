"""Kokoro voice-mix baking with L2 style-vector renormalization.

Kokoro-FastAPI blends voices with a naive weighted sum of style embeddings.
That shrinks the L2 norm of the style vector; the model was trained on
full-magnitude packs, so the result sounds muffled / noisy and often clips
into harsh high peaks.  Ophelia bakes mixes locally:

  1. Load stock voice packs (cached voices-v1.0.bin from kokoro-onnx)
  2. Weighted sum + per-row L2 renormalization to the expected norm
  3. Write a torch-compatible ``.pt`` the Kokoro server can load

Inline mix strings are resolved to a baked voice name when
``KOKORO_VOICES_DIR`` points at the server's voices folder (or when the
baked file is already installed there).
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable
from urllib.request import Request, urlopen

import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()

_VOICES_BIN_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)
_VOICES_BIN_NAME = "voices-v1.0.bin"

# af_heart(0.45)+af_bella(0.35)+bf_emma(0.2)  |  af_bella+af_sky  |  am_adam-am_michael
_MIX_PART = re.compile(
    r"^\s*([a-z][a-z0-9_]*)\s*(?:\(\s*([+-]?\d+(?:\.\d+)?)\s*\))?\s*$",
    re.IGNORECASE,
)


def is_mix_expression(voice: str) -> bool:
    """True when ``voice`` looks like a Kokoro weighted mix / subtract formula."""
    v = (voice or "").strip()
    if not v:
        return False
    if "+" in v or (v.count("-") >= 1 and not v.startswith("-") and "_" in v):
        # Single presets never contain '+' ; subtract mixes use name-name.
        if "+" in v:
            return True
        # am_adam-am_michael — hyphen between two voice ids
        return bool(re.search(r"[a-z0-9]-(?:[a-z][a-z0-9_]*)", v, re.I))
    return False


def parse_mix_expression(expression: str) -> list[tuple[str, float, str]]:
    """Parse ``af_a(0.6)+bf_b(0.4)`` into ``[(name, weight, op), ...]``.

    ``op`` is ``'+'`` for the first voice and ``'+'``/``'-'`` for the rest.
    Raises ``ValueError`` on malformed input.
    """
    expr = (expression or "").strip()
    if not expr:
        raise ValueError("Empty voice mix expression")

    # Split on + / - but keep operators; don't treat hyphen inside names
    # (voice ids use underscores, not hyphens).
    parts = re.split(r"\s*([+-])\s*", expr)
    if not parts or parts[0] == "":
        raise ValueError(f"Invalid voice mix (leading operator): {expression!r}")

    tokens: list[tuple[str, float, str]] = []
    # parts: [voice0, op1, voice1, op2, voice2, ...]
    first = parts[0]
    m = _MIX_PART.match(first)
    if not m:
        raise ValueError(f"Invalid voice component: {first!r}")
    tokens.append((m.group(1).lower(), float(m.group(2) or 1.0), "+"))

    i = 1
    while i < len(parts):
        op = parts[i]
        if op not in "+-":
            raise ValueError(f"Invalid operator in mix: {op!r}")
        if i + 1 >= len(parts):
            raise ValueError(f"Trailing operator in mix: {expression!r}")
        comp = parts[i + 1]
        m = _MIX_PART.match(comp)
        if not m:
            raise ValueError(f"Invalid voice component: {comp!r}")
        tokens.append((m.group(1).lower(), float(m.group(2) or 1.0), op))
        i += 2

    if len(tokens) < 2:
        raise ValueError(f"Mix needs at least two voices: {expression!r}")
    return tokens


def mix_cache_name(expression: str) -> str:
    """Stable short voice id for a baked mix (safe filename, no +/())."""
    tokens = parse_mix_expression(expression)
    # Readable prefix from first two voices + short hash of full formula
    digest = hashlib.sha1(expression.strip().encode("utf-8")).hexdigest()[:8]
    a = tokens[0][0]
    b = tokens[1][0]
    return f"ophelia_mix_{a}_{b}_{digest}"


def _voices_cache_dir(settings: "Settings | None" = None) -> Path:
    if settings is not None:
        return Path(settings.data_dir) / "kokoro_voices"
    from ophelia.config import OPHELIA_HOME

    return OPHELIA_HOME / "data" / "kokoro_voices"


def _ensure_voices_bin(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / _VOICES_BIN_NAME
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    log.info("kokoro_mix.download_voices_bin", url=_VOICES_BIN_URL, dest=str(path))
    req = Request(_VOICES_BIN_URL, headers={"User-Agent": "ophelia-kokoro-mix/1.0"})
    with urlopen(req, timeout=120) as resp:  # noqa: S310 — fixed upstream URL
        data = resp.read()
    path.write_bytes(data)
    return path


def _load_voice_arrays(voices_bin: Path) -> dict:
    import numpy as np

    raw = np.load(voices_bin, allow_pickle=True)
    return {k: raw[k] for k in raw.files}


def blend_style_vectors(
    components: Iterable[tuple[object, float, str]],
) -> "object":
    """Weighted blend of style packs with per-row L2 renormalization.

    ``components`` yields ``(array, weight, op)`` where ``op`` is ``'+'`` or ``'-'``.
    Arrays are ``(510, 1, 256)`` float32 (kokoro-onnx layout).
    """
    import numpy as np

    comps = list(components)
    if len(comps) < 2:
        raise ValueError("Need at least two voice tensors to blend")

    # Match Kokoro-FastAPI: divide every weight by the sum of all component
    # weights, then add or subtract.  We then restore per-row L2 magnitude.
    total_weight = sum(float(w) for _, w, _ in comps) or 1.0

    stacked = []
    signed_weights = []
    for arr, weight, op in comps:
        a = np.asarray(arr, dtype=np.float32)
        w = float(weight) / total_weight
        if op == "-":
            w = -w
        stacked.append(a)
        signed_weights.append(w)

    def row_norms(x: np.ndarray) -> np.ndarray:
        flat = x.reshape(x.shape[0], -1)
        return np.linalg.norm(flat.astype(np.float64), axis=1)

    norms = np.stack([row_norms(a) for a in stacked], axis=0)  # (n, rows)
    abs_w = np.array([abs(w) for w in signed_weights], dtype=np.float64)
    abs_sum = abs_w.sum() or 1.0
    abs_w_n = abs_w / abs_sum
    expected = (abs_w_n[:, None] * norms).sum(axis=0)

    blended = np.zeros_like(stacked[0], dtype=np.float32)
    for a, w in zip(stacked, signed_weights):
        blended = blended + (a * np.float32(w))

    flat = blended.reshape(blended.shape[0], -1).astype(np.float64)
    actual = np.linalg.norm(flat, axis=1)
    scale = np.ones_like(actual)
    mask = actual > 1e-8
    scale[mask] = expected[mask] / actual[mask]
    flat *= scale[:, None]
    return flat.reshape(blended.shape).astype(np.float32)


def write_torch_voice_pt(tensor, out_path: Path, *, archive_name: str = "voice") -> Path:
    """Write a float32 style pack as a torch-loadable ``.pt`` zip (no torch dep)."""
    import numpy as np

    arr = np.asarray(tensor, dtype=np.float32)
    if arr.ndim == 2:
        # (510, 256) -> (510, 1, 256)
        arr = arr[:, None, :]
    if arr.shape[-1] != 256 or arr.shape[0] < 100:
        raise ValueError(f"Unexpected voice tensor shape: {arr.shape}")

    # Match stock hexgrad packs: (510, 1, 256), contiguous, little-endian
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    n = int(arr.size)
    shape = tuple(int(x) for x in arr.shape)
    # strides for contiguous (510,1,256): (256, 256, 1) in element counts
    if shape == (510, 1, 256):
        stride = (256, 256, 1)
    else:
        # generic C-contiguous strides in elements
        stride_list = [1]
        for dim in reversed(shape[1:]):
            stride_list.append(stride_list[-1] * dim)
        stride = tuple(reversed(stride_list))

    # Build data.pkl identical in structure to hexgrad voice packs.
    # Manual pickle of:
    # torch._utils._rebuild_tensor_v2(storage_info, storage_offset, size, stride,
    #                                 requires_grad, backward_hooks)
    # with persistent_load id "0" for FloatStorage of size n on cpu.
    payload = bytearray()
    payload += b"\x80\x02"  # proto 2
    payload += b"ctorch._utils\n_rebuild_tensor_v2\n"
    payload += b"q\x00"
    payload += b"("
    payload += b"("
    payload += b"X\x07\x00\x00\x00storageq\x01"
    payload += b"ctorch\nFloatStorage\n"
    payload += b"q\x02"
    payload += b"X\x01\x00\x00\x000q\x03"
    payload += b"X\x03\x00\x00\x00cpuq\x04"
    # BININT (4-byte signed) for storage numel
    payload += b"J" + int(n).to_bytes(4, "little", signed=True)
    payload += b"tq\x05"
    payload += b"Q"  # BINPERSID -> storage "0"
    payload += b"K\x00"  # storage_offset = 0

    def _push_dim(buf: bytearray, value: int) -> None:
        if 0 <= value < 256:
            buf += b"K" + bytes([value])
        elif 0 <= value < 65536:
            buf += b"M" + int(value).to_bytes(2, "little", signed=False)
        else:
            buf += b"J" + int(value).to_bytes(4, "little", signed=True)

    # size tuple
    if len(shape) == 3:
        _push_dim(payload, shape[0])
        _push_dim(payload, shape[1])
        _push_dim(payload, shape[2])
        payload += b"\x87q\x06"  # TUPLE3
    else:
        payload += b"("
        for s in shape:
            _push_dim(payload, s)
        payload += b"tq\x06"

    # stride tuple
    if len(stride) == 3:
        _push_dim(payload, stride[0])
        _push_dim(payload, stride[1])
        _push_dim(payload, stride[2])
        payload += b"\x87q\x07"
    else:
        payload += b"("
        for s in stride:
            _push_dim(payload, s)
        payload += b"tq\x07"

    payload += b"\x89"  # NEWFALSE requires_grad
    payload += b"ccollections\nOrderedDict\n"
    payload += b"q\x08)Rq\t"
    payload += b"tq\nRq\x0b."

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", archive_name).strip("_") or "voice"

    with zipfile.ZipFile(out_path, "w") as zf:
        # Torch zip layout (uncompressed) matching stock packs
        def _writestr(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, data)

        _writestr(f"{safe}/data.pkl", bytes(payload))
        _writestr(f"{safe}/byteorder", b"little")
        _writestr(f"{safe}/data/0", arr.tobytes(order="C"))
        _writestr(f"{safe}/version", b"3\n")
        _writestr(
            f"{safe}/.data/serialization_id",
            hashlib.sha1(arr.tobytes()).hexdigest()[:40].encode("ascii"),
        )
    return out_path


def bake_voice_mix(
    expression: str,
    out_path: Path,
    *,
    settings: "Settings | None" = None,
    voices_bin: Path | None = None,
) -> Path:
    """Bake a mix expression to a torch ``.pt`` with L2-renormalized style vectors."""
    tokens = parse_mix_expression(expression)
    cache_dir = _voices_cache_dir(settings)
    bin_path = voices_bin or _ensure_voices_bin(cache_dir)
    voices = _load_voice_arrays(bin_path)

    missing = [name for name, _, _ in tokens if name not in voices]
    if missing:
        available = ", ".join(sorted(voices)[:12]) + ", ..."
        raise ValueError(
            f"Unknown Kokoro voice(s) {missing}. Available include: {available}"
        )

    comps = [(voices[name], weight, op) for name, weight, op in tokens]
    blended = blend_style_vectors(comps)
    name = mix_cache_name(expression)
    return write_torch_voice_pt(blended, out_path, archive_name=name)


def dominant_voice(expression: str) -> str:
    """Highest-weight '+' component — safe single-preset fallback."""
    tokens = parse_mix_expression(expression)
    best_name = tokens[0][0]
    best_w = -1.0
    for name, weight, op in tokens:
        if op != "+":
            continue
        if weight > best_w:
            best_w = weight
            best_name = name
    return best_name


def resolve_kokoro_voice(
    voice: str,
    *,
    settings: "Settings",
) -> str:
    """Return a server-safe voice id, baking mixes when a voices dir is configured.

    - Single presets pass through unchanged.
    - Mix expressions are baked with L2 renorm into ``~/.ophelia/voices/`` and,
      when ``settings.kokoro_voices_dir`` is set, installed there so Kokoro-FastAPI
      can load them by the short baked name.
    - If the mix cannot be installed for the server, fall back to the dominant
      single preset — never send a raw inline mix (those sound muffled/peaky).
    """
    voice = (voice or "").strip()
    if not voice or not is_mix_expression(voice):
        return voice

    try:
        baked_name = mix_cache_name(voice)
        fallback = dominant_voice(voice)
    except ValueError as e:
        log.warning("kokoro_mix.parse_failed", voice=voice, error=str(e))
        # Last resort: strip to first token-looking id
        return re.split(r"[+\-]", voice, maxsplit=1)[0].split("(")[0].strip() or "af_heart"

    from ophelia.config import OPHELIA_HOME

    local_dir = OPHELIA_HOME / "voices"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_pt = local_dir / f"{baked_name}.pt"

    try:
        if not local_pt.is_file():
            bake_voice_mix(voice, local_pt, settings=settings)
            log.info("kokoro_mix.baked", expression=voice, path=str(local_pt))
    except Exception as e:
        log.warning(
            "kokoro_mix.bake_failed",
            voice=voice,
            error=str(e),
            fallback=fallback,
        )
        return fallback

    voices_dir = (settings.kokoro_voices_dir or "").strip()
    if not voices_dir:
        log.warning(
            "kokoro_mix.no_voices_dir",
            hint=(
                "Refusing raw inline mix (muffled/peaky without L2 renorm). "
                f"Using dominant preset '{fallback}'. "
                "Set KOKORO_VOICES_DIR to your Kokoro-FastAPI voices folder "
                f"and re-try, or: ophelia tts combine {voice!r}"
            ),
            baked=str(local_pt),
            fallback=fallback,
        )
        return fallback

    dest_dir = Path(voices_dir).expanduser()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{baked_name}.pt"
        if not dest.is_file() or dest.stat().st_size != local_pt.stat().st_size:
            dest.write_bytes(local_pt.read_bytes())
            log.info("kokoro_mix.installed", dest=str(dest), voice=baked_name)
        return baked_name
    except OSError as e:
        log.warning(
            "kokoro_mix.install_failed",
            voices_dir=voices_dir,
            error=str(e),
            baked=str(local_pt),
            fallback=fallback,
        )
        return fallback
