"""Pack registry. Packs are loaded by name; the core never imports one directly."""

import threading

from ...errors import NotFound
from .base import PolicyPack

_REGISTRY = {}
_loaded = False
# Pack loading is lazy, and the parallel dispatcher resolves packs from several
# threads at once. Setting the flag before the import let a second thread see
# "already loaded" against an empty registry and fail to find its own project.
_LOAD_LOCK = threading.Lock()


def register(pack_class):
    _REGISTRY[pack_class.name] = pack_class
    return pack_class


def load(name):
    """Instantiate a pack by name. Unknown names fail loudly rather than silently
    falling back — a project running under the wrong rules is worse than a stop."""
    if not name or name == "generic":
        return PolicyPack()
    _load_builtin()
    if name not in _REGISTRY:
        raise NotFound(f"no policy pack named {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def available():
    _load_builtin()
    return sorted({"generic", *_REGISTRY})


def _load_builtin():
    """Import shipped packs lazily so the core stays free of project imports."""
    global _loaded
    if _loaded:
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        from . import example  # noqa: F401  (self-registers)
        _loaded = True
