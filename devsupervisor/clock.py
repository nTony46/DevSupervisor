"""One source of time, so tests can control it without patching stdlib."""

from datetime import datetime, timedelta, timezone

_frozen = None


def now():
    """Current UTC time, or the frozen time a test installed."""
    return _frozen if _frozen is not None else datetime.now(timezone.utc)


def now_iso():
    return now().isoformat()


def iso(moment):
    return moment.isoformat()


def parse(text):
    return datetime.fromisoformat(text)


def plus_seconds(seconds):
    return now() + timedelta(seconds=seconds)


def freeze(moment):
    """Pin the clock. Tests only."""
    global _frozen
    _frozen = moment


def advance(seconds):
    """Move a frozen clock forward. Tests only."""
    global _frozen
    if _frozen is None:
        raise RuntimeError("clock is not frozen; call freeze() first")
    _frozen = _frozen + timedelta(seconds=seconds)
    return _frozen


def unfreeze():
    global _frozen
    _frozen = None
