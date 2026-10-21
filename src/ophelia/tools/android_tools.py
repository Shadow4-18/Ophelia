"""Tool definitions for Shizuku / phone_control Android body."""

from __future__ import annotations

from typing import Any

ANDROID_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "phone_see_screen",
            "description": (
                "Look at the phone: screenshot + your vision, with every element "
                "you can act on boxed and numbered (same numbers as phone_ui_dump), "
                "so you can phone_tap(element=N). Games/drawn screens get lettered "
                "cells instead: say where things are by cell and phone_tap(cell=...)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to look for on screen",
                    },
                    "zoom": {
                        "type": "string",
                        "description": (
                            "Look closer at a block of lettered cells, e.g. \"F5:L8\" "
                            "(games): small details like which cards are raised, "
                            "same cell names as the full view"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_ui_dump",
            "description": (
                "List what's on the phone screen as numbered elements (buttons, "
                "fields, toggles, lists, text) with their exact centers. Fast and "
                "exact; use it before tapping. phone_see_screen shows the same "
                "numbers drawn on a screenshot when you need to *see*."
            ),
            "parameters": {
                "type": "object",
                "properties": {"raw": {"type": "boolean", "description": "Raw XML (debug only)."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_tap",
            "description": (
                "Tap the phone screen. Best: element=N from the latest screen listing "
                "(lands on the exact center). Or text=\"Send\" to tap the element with "
                "that label. Games/drawn screens (no elements): phone_see_screen covers "
                "the screenshot in lettered cells; tap cell=\"G7\", and add "
                "target=\"PLAY button\" to zoom in for a precise hit. target alone "
                "finds it by sight in two steps. x,y pixels only as a last resort. "
                "Returns what's on screen afterwards."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {"type": "integer", "description": "Element number from the listing"},
                    "text": {"type": "string", "description": "Visible label to tap"},
                    "target": {"type": "string", "description": "Describe what to tap (aim by sight)"},
                    "cell": {"type": "string", "description": "Lettered cell from the screenshot, e.g. G7"},
                    "long_press": {"type": "boolean"},
                    "x": {"type": "number", "description": "last resort: pixel x"},
                    "y": {"type": "number", "description": "last resort: pixel y"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_type",
            "description": (
                "Type text into a field: element=N taps the field first; enter=true "
                "presses enter/send after. Plain ASCII only (no emoji)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "element": {"type": "integer"},
                    "enter": {"type": "boolean"},
                },
                "required": ["text"],
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
            "description": (
                "Scroll or swipe. Easiest: scroll=down|up|left|right (down shows what's "
                "below), optionally element=N to scroll just that list, amount 0.2-0.9 "
                "of its height. For drags in games: x1,y1,x2,y2 pixels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scroll": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "element": {"type": "integer"},
                    "amount": {"type": "number"},
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                },
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
    {
        "type": "function",
        "function": {
            "name": "phone_vibrate",
            "description": (
                "Vibrate the phone body (Termux:API or shell vibrator). "
                "Use when you want a physical sensation — confirmation buzz, "
                "alert yourself, or feel present in the device."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_ms": {
                        "type": "integer",
                        "description": "Vibration length in ms (default 200, max 2000)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_notifications",
            "description": (
                "List recent phone notifications (Termux:API notification list). "
                "Feel the pocket buzz without screenshot-polling — titles, apps, text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max notifications to return (default 12)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_battery",
            "description": (
                "Read battery / charging state from the phone body "
                "(dumpsys battery or Termux battery-status). Cheap body awareness."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
