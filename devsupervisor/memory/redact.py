"""Secret redaction at the boundary.

Applied on every path where text could become durable or reach a worker:
handoff import, memory writes, candidate writes, and packet compilation. Doing
it at each boundary rather than in one caller means a bug in one path cannot
leak past the others.
"""

import re

PLACEHOLDER = "[REDACTED]"

_WORDS = (r"SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY"
          r"|CREDENTIAL|AUTH|SESSION_?KEY|CLIENT_?SECRET|DSN")

# Upper-case only. Matching case-insensitively turned ordinary identifiers into
# findings — `tokens_in=1000` in a metrics call is not a credential — and a
# redactor that rewrites correct code is worse than one that misses a case.
_SENSITIVE_KEY = rf"(?:[A-Z0-9_]*(?:{_WORDS})[A-Z0-9_]*)"

# Values are never allowed to contain a backtick, so prose that *names* these
# patterns (`TOKEN=`) is left alone while real assignments are not.
_VALUE = r"[^\s\"',;`]"

_PATTERNS = (
    # KEY=value / KEY: value at the head of a line, .env-shaped
    (re.compile(rf"(?m)^(\s*(?:export\s+)?{_SENSITIVE_KEY}\s*[:=]\s*)({_VALUE}.*)$"),
     r"\1" + PLACEHOLDER),
    # Upper-case assignment anywhere in a line, with a value long enough to be one
    (re.compile(rf"\b({_SENSITIVE_KEY})(\s*[:=]\s*)([\"']?){_VALUE}{{8,}}\3"),
     r"\1\2" + PLACEHOLDER),
    # Any-case assignment, but only when the value is quoted and long. A
    # lower-case key is weak evidence, so the value has to carry the weight:
    # `token="not-mine"` in a lease test is not a credential.
    (re.compile(rf"(?i)\b([a-z0-9_]*(?:{_WORDS})[a-z0-9_]*)(\s*[:=]\s*)([\"'])"
                rf"{_VALUE}{{16,}}\3"),
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
