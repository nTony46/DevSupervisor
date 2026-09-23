"""Local dashboard with read-only execution state and separate UI settings.

Profiles and workflow display names live in a companion database. Dashboard
edits never schedule work or modify existing jobs, leases, or routing policy.
"""

from .reader import Reader, StateUnavailable
from .server import DEFAULT_PORT, HOST, build_server, serve

__all__ = ["Reader", "StateUnavailable", "serve", "build_server", "DEFAULT_PORT", "HOST"]
