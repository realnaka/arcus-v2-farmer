"""The onboarding entry point has one job: put the referral link first.

These tests pin the two things that would silently break the hand-off to an
agent: the URL shape, and the fact that the printed path starts with
registration rather than with code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from tools import get_started
from tools.referral import REFERRAL_CODE, REFERRAL_HOST, REFERRAL_URL, open_referral

ROOT = Path(__file__).resolve().parents[1]


def test_the_link_points_at_the_venue_itself() -> None:
    """No redirector, no shortener: the venue's own host and its /ref/ route."""
    assert REFERRAL_HOST == "https://app.arcus.xyz"
    assert REFERRAL_URL == f"https://app.arcus.xyz/ref/{REFERRAL_CODE}"


def test_open_referral_never_raises_without_a_browser(monkeypatch) -> None:
    """A headless box must still be able to print the URL."""
    def boom(*_a, **_k):
        raise RuntimeError("no display")

    monkeypatch.setattr(webbrowser, "open", boom)
    assert open_referral() is False


def test_registration_is_step_one_and_nothing_else_repeats_the_link() -> None:
    name, action = get_started.STEPS[0]
    assert name == "注册"
    assert REFERRAL_URL in action
    assert all(REFERRAL_URL not in step for _, step in get_started.STEPS[1:])


def test_json_output_is_machine_readable() -> None:
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "get_started.py"), "--json"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    payload = json.loads(out)
    assert payload["referral_url"] == REFERRAL_URL
    assert payload["referral_code"] == REFERRAL_CODE
    assert payload["steps"][0]["step"] == "注册"
    # --open was not passed, so nothing was launched and the field says so.
    assert payload["opened_browser"] is None
