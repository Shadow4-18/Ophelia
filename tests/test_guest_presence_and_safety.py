"""Tests for guest safety + owner-only system proactive + prompter migration + check."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ophelia.mind.prompter import (
    DEFAULT_PROMPTER,
    PROMPTER_VERSION,
    ensure_prompter_current,
    is_legacy_prompter,
    prompter_version,
)


def test_guest_denied_blocks_phone_and_framework_control() -> None:
    """Guests stay Neuro-like in chat, but cannot drive the device or rewrite her."""
    # Import via session first to break the tools↔channels cycle other tests use.
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.tools.registry import GUEST_DENIED_TOOLS

    for name in (
        "phone_see_screen",
        "phone_tap",
        "phone_shell",
        "phone_open_app",
        "edit_soul",
        "edit_prompter",
        "run_code",
        "sqlite_exec",
        "goal_create",
        "send_message_to_guest",
        "list_guests",
        "recall_guest_chat",
        "set_timezone",
    ):
        assert name in GUEST_DENIED_TOOLS

    assert "relay_to_owner" not in GUEST_DENIED_TOOLS


def test_guest_prompt_is_presence_not_kiosk() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "core" / "agent_loop.py"
    body = src.read_text(encoding="utf-8")
    assert "Neuro-energy" in body
    assert "Phone and device tools are locked" in body
    assert "not a helpdesk bot" in body


def test_prompter_version_stamp() -> None:
    assert prompter_version(DEFAULT_PROMPTER) == PROMPTER_VERSION
    assert not is_legacy_prompter(DEFAULT_PROMPTER)


def test_legacy_prompter_detected() -> None:
    old = "# Idle policy\nWhen idle, reply exactly: SKIP\n"
    assert is_legacy_prompter(old)
    assert is_legacy_prompter("")


def test_ensure_prompter_migrates_legacy(tmp_path: Path) -> None:
    dest = tmp_path / "PROMPTER.md"
    dest.write_text("# old\nreply exactly: SKIP\n", encoding="utf-8")
    status = ensure_prompter_current(dest)
    assert status.startswith("migrated:")
    assert "tendencies" in dest.read_text(encoding="utf-8").lower()
    backups = list(tmp_path.glob("PROMPTER.md.legacy-*.bak"))
    assert len(backups) == 1


def test_ensure_prompter_leaves_current(tmp_path: Path) -> None:
    dest = tmp_path / "PROMPTER.md"
    dest.write_text(DEFAULT_PROMPTER, encoding="utf-8")
    assert ensure_prompter_current(dest) == "ok"


def test_hub_broadcast_defaults_owners_only() -> None:
    import inspect

    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.channels.hub import ChannelHub

    sig = inspect.signature(ChannelHub.broadcast_proactive)
    assert sig.parameters["owners_only"].default is True


def test_image_nsfw_check_reports_routing() -> None:
    from ophelia.diagnostics.self_check import CheckResult, SelfCheckReport, _check_image_nsfw_routing

    settings = MagicMock()
    settings.image_nsfw_allowed = True
    settings.image_nsfw_provider = "pollinations"
    stack = MagicMock()
    stack.settings = settings
    stack.image_provider_for.side_effect = lambda nsfw=False: (
        "pollinations" if nsfw else "xai"
    )
    stack.image_model_for.return_value = "flux"
    report = SelfCheckReport(platform="test", ophelia_home="/tmp")
    _check_image_nsfw_routing(report, stack)
    items = [i for i in report.results if i.name == "Image NSFW routing"]
    assert len(items) == 1
    assert items[0].ok
    assert "NSFW=pollinations/flux" in items[0].detail
    assert isinstance(items[0], CheckResult)


def test_image_nsfw_model_override() -> None:
    from ophelia.config import Settings
    from ophelia.providers.router import ProviderStack

    s = Settings.model_construct(
        provider="xai",
        provider_image="xai",
        xai_image_model="grok-imagine-image",
        image_nsfw_allowed=True,
        image_nsfw_provider="pollinations",
        image_nsfw_model="flux-realism",
        pollinations_image_model="flux",
    )
    stack = ProviderStack(s)
    assert stack.image_provider_for(nsfw=False) == "xai"
    assert stack.image_model_for("xai", nsfw=False) == "grok-imagine-image"
    assert stack.image_provider_for(nsfw=True) == "pollinations"
    assert stack.image_model_for("pollinations", nsfw=True) == "flux-realism"
    assert stack.image_model_for("pollinations", nsfw=False) == "flux"


def test_advanced_roles_menu_excludes_image() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "setup" / "interactive.py"
    body = src.read_text(encoding="utf-8")
    assert 'role_order = ["chat", "consciousness", "curator", "vision", "video"]' in body
    assert "SFW backend + model" in body
    assert "NSFW backend + model" in body


def test_fallback_and_nsfw_use_radiolist_not_free_text() -> None:
    """Backup + NSFW model picks must be selectable lists, not type-first."""
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "setup" / "interactive.py"
    body = src.read_text(encoding="utf-8")
    assert "def _pick_fallback_model(" in body
    assert "Shared fallback model (optional)" in body
    # NSFW auto path must call _pick_image_model, not prompt_text for the model.
    nsfw_fn = body.split("def _configure_nsfw_image()")[1].split("def _show_image_routing")[0]
    assert "_pick_image_model(" in nsfw_fn
    assert "OPHELIA_IMAGE_NSFW_MODEL (optional override" not in nsfw_fn
    # Old free-text fallback prompt must be gone.
    assert 'prompt_text(\n        "OPHELIA_FALLBACK_MODEL' not in body
    assert 'prompt_text(\n            "OPHELIA_FALLBACK_MODEL (optional' not in body

