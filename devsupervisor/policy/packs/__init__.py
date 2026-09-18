"""Pack registry. Packs are loaded by name; the core never imports one directly.

Two places supply packs: the `example` pack shipped in this package, and any
`*.py` under `$DEVSUPERVISOR_HOME/packs/` (see `config.packs_dir`). A pack for a
real project belongs in the second place — it names that project and its rules,
and the repository should not have to.
"""

import importlib.util
import threading

from ... import config
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
    """Import packs lazily so the core stays free of project imports."""
    global _loaded
    if _loaded:
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        from . import example  # noqa: F401  (self-registers)
        load_from(config.packs_dir())
        _loaded = True


def load_from(directory):
    """Import every pack module in a directory. Each registers itself with
    `@register`, exactly as a shipped pack does. Returns the names found."""
    directory = directory.expanduser()
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith(("_", "test_")):
            continue
        before = set(_REGISTRY)
        spec = importlib.util.spec_from_file_location(
            f"devsupervisor_packs_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found.extend(sorted(set(_REGISTRY) - before))
    return found
