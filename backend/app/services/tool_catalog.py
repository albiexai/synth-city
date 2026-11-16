"""The Mail tool catalog a running agent was started with, compared with the deployed one (#960).

The shim sends a fingerprint of the tools it registered when its session registers. The backend compares it with the
fingerprint of the deployed list. The result is a display fact: it grants no authority and changes no wake or delivery
rule. A stale member keeps working with the list it started with; restarting it in resume mode loads the new one.
"""
from __future__ import annotations

import hashlib

STALE_REASON = "tool catalog stale: restart in resume mode"


def catalog_fingerprint(names) -> str:
    """A short digest of a set of tool names. It ignores order and repeats. The shim computes the same digest."""
    return hashlib.sha256("\n".join(sorted(set(names))).encode()).hexdigest()[:32]


def current_catalog_fingerprint() -> str:
    """The digest of the deployed list, from the names the provider upgrade expects (a test ties them to the shim)."""
    from app.services.provider_upgrade import _SUFFIXES

    return catalog_fingerprint("synth_city_" + suffix for suffix in _SUFFIXES)


def catalog_state(reported: list) -> str:
    """current, stale or unknown for the fingerprints that a member's connected sessions reported.

    Conservative: ``current`` only when every connected session reported the deployed list; ``stale`` when any of them
    reported another list; otherwise ``unknown`` (no connected session, or one that never reported, such as an older shim).
    """
    if not reported:
        return "unknown"
    deployed = current_catalog_fingerprint()
    if any(value is not None and value != deployed for value in reported):
        return "stale"
    if all(value == deployed for value in reported):
        return "current"
    return "unknown"
