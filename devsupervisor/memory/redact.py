"""Secret redaction at the boundary.

Applied on every path where text could become durable or reach a worker:
handoff import, memory writes, candidate writes, and packet compilation. Doing
it at each boundary rather than in one caller means a bug in one path cannot
leak past the others.
"""

import re

PLACEHOLDER = "[REDACTED]"

_SENSITIVE_KEY = (
    r"(?:[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY"
    r"|CREDENTIAL|AUTH|SESSION_?KEY|CLIENT_?SECRET|DSN)[A-Z0-9_]*)"
)

_PATTERNS = (
    # KEY=value / KEY: value in .env-shaped text
    (re.compile(rf"(?im)^(\s*(?:export\s+)?{_SENSITIVE_KEY}\s*[:=]\s*)(.+)$"), r"\1" + PLACEHOLDER),
    # Inline quoted assignment anywhere in a line
    (re.compile(rf"(?i)\b({_SENSITIVE_KEY})(\s*[:=]\s*)([\"']?)[^\s\"',;]+\3"),
     r"\1\2" + PLACEHOLDER),
    # PEM blocks
    (re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
     PLACEHOLDER),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{12,}"), r"\1 " + PLACEHOLDER),
    # Well-known vendor token shapes
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), PLACEHOLDER),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), PLACEHOLDER),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{16,}"), PLACEHOLDER),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), PLACEHOLDER),
    # Credentials embedded in a connection URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s/@]+@"), r"\1:" + PLACEHOLDER + "@"),
)


def redact(text):
    """Return text with anything credential-shaped replaced."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def contains_secret(text):
    """True when redaction would change the text — used to refuse an import."""
    return redact(text) != text


def findings(text):
    """Line numbers that would be redacted. For doctor output and audit."""
    hits = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        if redact(line) != line:
            hits.append(number)
    return hits
