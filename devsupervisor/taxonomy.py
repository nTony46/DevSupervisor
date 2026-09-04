"""Blocker and failure taxonomies.

Categories are deliberately coarse. A taxonomy fine enough to be interesting is
usually too fine to be stable, and the point here is to spot a *pattern* across
many jobs, not to label any one blocker perfectly.
"""

BLOCKER_CATEGORIES = (
    ("scope", ("out of scope", "unrelated", "also changed", "beyond the contract",
               "scope creep", "touched")),
    ("correctness", ("off by one", "incorrect", "wrong", "returns", "null", "race",
                     "crash", "panic", "does not handle")),
    ("tests", ("no test", "missing test", "untested", "coverage", "does not fail without")),
    ("safety", ("force push", "secret", "credential", "destructive", "irreversible",
                "permission", "leak")),
    ("evidence", ("no evidence", "unverified", "claims", "did not run", "no log")),
    ("docs", ("docs", "documentation", "comment", "readme", "changelog")),
)

FAILURE_CATEGORIES = (
    ("timeout", ("timed out", "timeout")),
    ("provider", ("could not start", "provider", "exit code", "unparseable")),
    ("contract", ("invalid result", "missing 'status'", "did not emit", "unknown result")),
    ("blocked", ("blocked", "needs human")),
)

OTHER = "other"


def _classify(text, table):
    lowered = (text or "").lower()
    for category, needles in table:
        if any(needle in lowered for needle in needles):
            return category
    return OTHER


def classify_blocker(text):
    return _classify(text, BLOCKER_CATEGORIES)


def classify_failure(text):
    return _classify(text, FAILURE_CATEGORIES)
