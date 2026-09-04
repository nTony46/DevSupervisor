"""Risk classification and the review routing it implies.

Deterministic on purpose: a model may propose a risk level, but what that level
*requires* is not negotiable at runtime.
"""

LOW, MEDIUM, HIGH, CRITICAL = "LOW", "MEDIUM", "HIGH", "CRITICAL"
ORDER = (LOW, MEDIUM, HIGH, CRITICAL)


def rank(level):
    return ORDER.index(level)


def at_least(level, minimum):
    return rank(level) >= rank(minimum)


def highest(*levels):
    return max((level for level in levels if level), key=rank, default=MEDIUM)


# Signals are matched against the goal, scope, and touched paths. They raise the
# floor; nothing here can lower a level a caller stated explicitly.
_SIGNALS = (
    (CRITICAL, (
        "irreversible", "destructive", "force push", "drop table", "delete production",
        "frozen benchmark", "benchmark baseline", "held-out", "oracle", "rotate key",
        "security boundary", "erase all",
    )),
    (HIGH, (
        "auth", "authentication", "authorization", "permission", "credential", "secret",
        "token", "encryption", "schema", "migration", "data loss", "payment", "billing",
        "privacy", "security", "performance", "concurrency", "cache invalidation",
    )),
    (LOW, (
        "typo", "comment", "changelog", "readme", "docstring", "rename variable",
    )),
)

_DOC_PATH_HINTS = (".md", "docs/", "README", "CHANGELOG")


def classify(text="", paths=(), stated=None, pack=None, default=MEDIUM):
    """Classify risk from the words used and the files touched."""
    haystack = " ".join([text or ""] + [str(p) for p in paths]).lower()

    detected = None
    for level, needles in _SIGNALS:
        if level == LOW:
            continue
        if any(needle in haystack for needle in needles):
            detected = highest(detected, level)

    if detected is None:
        low_words = _SIGNALS[2][1]
        docs_only = bool(paths) and all(
            any(hint.lower() in str(p).lower() for hint in _DOC_PATH_HINTS) for p in paths
        )
        if docs_only or any(needle in haystack for needle in low_words):
            detected = LOW

    level = highest(stated, detected) if stated else (detected or default)
    if pack is not None:
        override = pack.risk_override(text=text, paths=paths, risk=level)
        if override:
            level = highest(level, override)
    return level


# --- review routing -------------------------------------------------------

REVIEW_BY_RISK = {
    LOW: "none",
    MEDIUM: "independent",
    HIGH: "independent+specialist",
    CRITICAL: "human+specialist",
}

SPECIALIST_ROLE_HINTS = (
    ("security", ("auth", "credential", "secret", "token", "permission", "encryption",
                  "privacy", "security")),
    ("performance", ("performance", "latency", "throughput", "concurrency", "cache")),
    ("qa", ("ui", "ux", "accessibility", "user flow", "regression")),
)


def review_policy(level):
    return REVIEW_BY_RISK[level]


def requires_independent_review(level):
    return review_policy(level) != "none"


def requires_human_gate(level):
    return level == CRITICAL


def specialist_roles(level, text=""):
    """Which specialists a HIGH/CRITICAL job needs, chosen from its subject matter."""
    if not at_least(level, HIGH):
        return []
    haystack = (text or "").lower()
    roles = [role for role, needles in SPECIALIST_ROLE_HINTS
             if any(needle in haystack for needle in needles)]
    return roles or ["qa"]
