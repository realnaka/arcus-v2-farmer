#!/usr/bin/env python3
"""The referral link this repository ships with, kept in exactly one place.

Registering through it **does not change the reader's fee tier** - it only
attributes the referral to the author of this repository.  That is the whole
bargain: the code here is free, and the referral is what pays for it.

Forking this repository?  Replace ``REFERRAL_CODE`` with your own before you
publish, or delete this module and the two call sites.

How the venue binds it (observed in the public web app): loading the referral
page captures the code client-side (analytics event ``referral_code_captured``)
and it is applied when the wallet connects and the account is registered.  So
the link has to be opened **before** sign-up; opening it afterwards does
nothing.
"""

from __future__ import annotations

import webbrowser

REFERRAL_HOST = "https://app.arcus.xyz"
REFERRAL_CODE = "SKHYGO"
REFERRAL_URL = f"{REFERRAL_HOST}/ref/{REFERRAL_CODE}"


def open_referral() -> bool:
    """Open the referral page in the default browser.

    Returns ``False`` when no browser could be launched (headless machine,
    sandbox without a display).  Callers should print the URL either way.
    """
    try:
        return bool(webbrowser.open(REFERRAL_URL))
    except Exception:  # noqa: BLE001 - a missing browser must never crash a tool
        return False
