"""Hardware-aware Ollama model recommendations (Cookbook-style)."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

import httpx

from ophelia.config import Settings

# name, min_ram_gb, role hint, pull name
CATALOG: list[tuple[str, float, str, str]] = [
    ("llama3.2:1b", 4, "consciousness / fast ticks", "llama3.2:1b"),
    ("llama3.2:3b", 8, "chat + consciousness", "llama3.2:3b"),
    ("llama3.2", 12, "general chat", "llama3.2"),
    ("mistral", 12, "general chat", "mistral"),
    ("qwen2.5:7b", 16, "strong chat", "qwen2.5:7b"),
    ("llava:7b", 16, "vision (phone screen)", "llava:7b"),
    ("llava:13b", 24, "vision quality", "llava:13b"),
    ("deepseek-r1:8b", 20, "reasoning", "deepseek-r1:8b"),
]


@dataclass
class SystemProfile:
    ram_gb: float
    gpu_name: str
    os_name: str
    cpu: str


def detect_system() -> SystemProfile:
    ram_gb = 8.0
    try:
        import psutil

        ram_gb = psutil.virtual_memory().total / (1024**3)
    except ImportError:
        if platform.system() == "Windows":
            try:
                out = subprocess.check_output(
                    ["wmic", "computersystem", "get", "TotalPhysicalMemory"],
                    text=True,
                    timeout=5,
                )
                for line in out.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        ram_gb = int(line) / (1024**3)
                        break
            except (OSError, subprocess.SubprocessError):
                pass

    gpu = "unknown"
    if platform.system() == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                text=True,
                timeout=5,
            )
            names = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "Name"]
            if names:
                gpu = names[0]
        except (OSError, subprocess.SubprocessError):
            pass

    return SystemProfile(
        ram_gb=round(ram_gb, 1),
        gpu_name=gpu,
        os_name=platform.system(),
        cpu=platform.processor() or platform.machine(),
    )


async def list_ollama_models(settings: Settings) -> list[str]:
    base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/tags")
            if r.status_code != 200:
                return []
            data = r.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except httpx.HTTPError:
        return []


def recommend(profile: SystemProfile) -> list[tuple[str, float, str, str]]:
    margin = 2.0  # GB headroom
    return [row for row in CATALOG if profile.ram_gb >= row[1] + margin]


def format_cookbook(settings: Settings, profile: SystemProfile, installed: list[str]) -> str:
    lines = [
        "Ophelia Cookbook — local model recommendations",
        f"System: {profile.ram_gb} GB RAM | {profile.gpu_name} | {profile.os_name}",
        f"Ollama: {settings.ollama_base_url}",
        f"Installed: {', '.join(installed) if installed else '(none — run ollama pull <model>)'}",
        "",
        "Recommended for your hardware:",
    ]
    for name, ram, role, pull in recommend(profile):
        mark = " [installed]" if any(pull in i or name in i for i in installed) else ""
        lines.append(f"  {pull:20} — {role} (needs ~{ram} GB RAM){mark}")

    rec = recommend(profile)
    chat_model = rec[-2][3] if len(rec) >= 2 else "llama3.2:3b"
    fast_model = rec[0][3] if rec else "llama3.2:1b"
    vision_model = next((r[3] for r in rec if "llava" in r[3]), "llava:7b")
    lines.extend(
        [
            "",
            "Suggested ~/.ophelia/.env (local-first, streaming-ready):",
            "  OPHELIA_PROVIDER=ollama",
            f"  OLLAMA_MODEL={chat_model}",
            "  OPHELIA_PROVIDER_CONSCIOUSNESS=ollama",
            f"  # use a smaller model for fast ticks: OLLAMA_MODEL={fast_model}",
            "  OPHELIA_PROVIDER_VISION=ollama",
            f"  OLLAMA_VISION_MODEL={vision_model}",
            "",
            f"Pull: ollama pull {chat_model} && ollama pull {vision_model}",
        ]
    )
    return "\n".join(lines)
