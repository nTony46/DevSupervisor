"""Read-only localhost dashboard for DevSupervisor.

Observes durable state. It never writes, never schedules, and never becomes a
second source of truth: every number on the page is a query away from SQLite.
"""

from .reader import Reader, StateUnavailable
from .server import DEFAULT_PORT, HOST, build_server, serve

__all__ = ["Reader", "StateUnavailable", "serve", "build_server", "DEFAULT_PORT", "HOST"]
