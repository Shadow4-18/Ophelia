"""Tool definitions for Shizuku / phone_control Android body."""

from __future__ import annotations

from typing import Any

ANDROID_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "phone_see_screen",
            "description": (
                "Capture screenshot + Grok vision + UI tree. Primary way to SEE the phone. "
                "Use before tap or when exploring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to look for on screen",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_ui_dump",
            "description": "Read current screen UI tree (requires Shizuku). Use before tap.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_tap",
            "description": "Tap screen coordinates (from ui-dump bounds center).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_open_app",
            "description": "Launch Android app by package name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"},
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_swipe",
            "description": "Swipe between two points (scroll, drag pieces, puzzles).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_key",
            "description": "Android navigation: home, back, volume_up, volume_down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": ["home", "back", "volume_up", "volume_down"],
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_shell",
            "description": "Run shell command on phone via Shizuku (settings, pm, input, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]
