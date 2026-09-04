"""Stable identifiers.

A job id is durable and human-meaningful (BUILD-line-range-001). The process,
the provider session, and any "Agent 4" label are metadata that do not survive a
restart and must never be used as identity.
"""

import re
import uuid

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug(text, max_words=4):
    """Lowercase hyphenated slug, capped so job ids stay readable."""
    cleaned = _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")
    if not cleaned:
        return "untitled"
    return "-".join(cleaned.split("-")[:max_words])


def job_id(role_prefix, subject, sequence):
    """BUILD-line-range-001"""
    return f"{role_prefix.upper()}-{slug(subject)}-{sequence:03d}"


def new_id(prefix):
    """Opaque id for rows with no meaningful natural key."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
