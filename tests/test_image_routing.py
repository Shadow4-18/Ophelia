"""Every "where does this image go" decision, tested without any backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ophelia.providers.image_job import FAILED, REFUSED, ImageJob
from ophelia.providers.image_routing import prepare_job, route


def _settings(**kw):
    base = dict(image_nsfw_allowed=True, civitai_api_key="k",
                image_backend_configured=lambda name: False)
    base.update(kw)
    return SimpleNamespace(**base)


class _Stack:
    def __init__(self, sfw="xai", nsfw="civitai"):
        self.sfw, self.nsfw = sfw, nsfw

    def image_provider_for(self, nsfw=False):
        return self.nsfw if nsfw else self.sfw

    def image_model_for(self, provider, nsfw=False):
        return "grok-imagine" if provider == "xai" else ""


@pytest.mark.parametrize("prompt, nsfw_in, owner, nsfw_out", [
    ("goth woman, topless", False, True, True),       # explicit without the flag
    ("her in black lingerie", True, True, False),     # suggestive: Grok first
    ("goth woman, topless", False, False, False),     # guests never auto-upgraded
    ("fully nude on a bed", True, True, True),
])
def test_prepare_job_nsfw(prompt, nsfw_in, owner, nsfw_out):
    job = prepare_job(_settings(), ImageJob(prompt=prompt, nsfw=nsfw_in), is_owner=owner)
    assert job.nsfw is nsfw_out
    assert job.fallback_mature is owner


def test_prepare_job_guest_limits_and_pinned_choice():
    big = ImageJob(prompt="x", aspect_ratio="16:9", quality="max", count=4, attempts=5,
                   pose_image="/p.png")
    g = prepare_job(_settings(), big, is_owner=False)
    assert (g.aspect_ratio, g.quality, g.count, g.attempts, g.pose_image) == \
        ("1:1", "standard", 1, 1, None)
    o = prepare_job(_settings(), big, is_owner=True)
    assert (o.aspect_ratio, o.count, o.pose_image) == ("16:9", 4, "/p.png")
    # A pinned model or an edit keeps nsfw as she set it.
    pinned = prepare_job(_settings(), ImageJob(prompt="portrait", nsfw=True, model="pony"),
                         is_owner=True)
    assert pinned.nsfw and not pinned.auto_pick
    assert prepare_job(_settings(), ImageJob(prompt="x"), is_owner=True).auto_pick


def test_route_basics():
    s, st = _settings(), _Stack()
    sfw = route(s, st, ImageJob(prompt="a cat"))
    assert (sfw.provider, sfw.model) == ("xai", "grok-imagine")
    assert sfw.fallbacks == ["pollinations"]
    nsfw = route(s, st, ImageJob(prompt="x", nsfw=True))
    assert nsfw.provider == "civitai" and nsfw.fallbacks == [] and nsfw.auto_pick


def test_route_refusals():
    r = route(_settings(image_nsfw_allowed=False), _Stack(), ImageJob(prompt="x", nsfw=True))
    assert r.refused and r.refusal.status == REFUSED
    r = route(_settings(), _Stack(nsfw="xai"), ImageJob(prompt="x", nsfw=True))
    assert r.refused and "refuses NSFW" in r.refusal.text
    r = route(_settings(civitai_api_key=""), _Stack(sfw="pollinations"),
              ImageJob(prompt="x", image="/a.png"))
    assert r.refused and r.refusal.status == FAILED


@pytest.mark.parametrize("job, provider", [
    (ImageJob(prompt="x", image="/a.png"), "xai"),                 # SFW edit on Grok
    (ImageJob(prompt="x", pose_image="/p.png"), "civitai"),        # pose → Civitai
    (ImageJob(prompt="x", model="hassaku"), "civitai"),            # her recipe name
    (ImageJob(prompt="x", model="urn:air:sdxl:checkpoint:civitai:1@2"), "civitai"),
    (ImageJob(prompt="x", model="grok-imagine-image-2.0"), "xai"),
])
def test_route_reroutes(job, provider):
    r = route(_settings(), _Stack(), job)
    assert r.provider == provider
    if job.image:
        assert r.fallbacks == []  # an edit never silently becomes text-to-image


def test_without_civitai_key_its_options_are_dropped_not_misrouted():
    s = _settings(civitai_api_key="")
    r = route(s, _Stack(), ImageJob(prompt="x", pose_image="/p.png", model="hassaku"))
    assert r.provider == "xai" and r.job.pose_image is None and r.model == "grok-imagine"
