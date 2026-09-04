"""`devsup doctor` — is this installation in a state you can trust?"""

from . import config, gitfacts, providers
from .memory import redact
from .memory.store import MemoryStore
from .state import db, leases

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _check(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


def run(store):
    checks = [_home(), _database(store), _lock(store), _default_provider(), _paid_provider(),
              _routing(store)]
    checks += _projects(store)
    checks += [_gates(store), _leases(store)]
    return checks


def worst(checks):
    for level in (FAIL, WARN):
        if any(check["status"] == level for check in checks):
            return level
    return OK


def _home():
    home = config.home()
    if not home.exists():
        return _check("runtime root", FAIL, f"{home} does not exist; run `devsup init`")
    probe = home / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return _check("runtime root", FAIL, f"{home} is not writable: {exc}")
    return _check("runtime root", OK, str(home))


def _database(store):
    try:
        version = db.one(store.conn, "SELECT MAX(version) AS v FROM schema_version")["v"]
    except Exception as exc:                                   # pragma: no cover
        return _check("database", FAIL, str(exc))
    expected = db.SCHEMA_VERSION
    if version != expected:
        return _check("database", WARN, f"schema v{version}, expected v{expected}")
    return _check("database", OK, f"{config.db_path()} (schema v{version})")


def _lock(store):
    lock = leases.supervisor_lock_status(store)
    if lock is None:
        return _check("supervisor lock", OK, "free")
    if lock["expired"]:
        return _check("supervisor lock", WARN,
                      f"stale lock from {lock['owner']} (pid {lock['pid']}); "
                      f"the next run will take it over")
    return _check("supervisor lock", OK, f"held by {lock['owner']} until {lock['expires_at']}")


def _default_provider():
    provider = providers.resolve()
    status = OK if not provider.is_paid else FAIL
    return _check("default provider", status,
                  f"{provider.name} (paid={provider.is_paid})")


def _paid_provider():
    available = providers.ClaudeCLIProvider.available()
    return _check("claude adapter", OK if available else WARN,
                  "`claude` found on PATH" if available
                  else "`claude` not on PATH; only the mock provider can run")


def _routing(store):
    """The resolved model routing, and whether critical roles sit at the floor."""
    from .policy import immutable
    from .policy.routing import ModelRouter
    try:
        router = ModelRouter(store)
        resolution = router.resolution
        decisions = router.table()
    except Exception as exc:                                   # pragma: no cover
        return _check("model routing", FAIL, str(exc))

    critical = [d for d in decisions if d.critical]
    below = [d.role for d in critical
             if d.effort != immutable.ROUTING_FLOOR["effort"]
             and immutable.EFFORT_LEVELS.index(d.effort)
             < immutable.EFFORT_LEVELS.index(immutable.ROUTING_FLOOR["effort"])]
    if below:                                                  # pragma: no cover
        return _check("model routing", FAIL,
                      f"critical roles below the floor: {', '.join(below)}")
    status = OK if resolution.concrete else WARN
    detail = (f"{resolution.model_id} @ effort {resolution.effort} for all "
              f"{len(decisions)} roles ({resolution.model_source})")
    if not resolution.concrete:
        detail += "; no concrete id discoverable, the alias will be sent"
    return _check("model routing", status, detail)


def _projects(store):
    checks = []
    for project in store.list_projects():
        facts = gitfacts.facts(project["repo_path"])
        if not facts["exists"]:
            checks.append(_check(f"project {project['name']}", FAIL,
                                 f"{project['repo_path']} does not exist"))
        elif not facts["is_git"]:
            checks.append(_check(f"project {project['name']}", WARN,
                                 f"{project['repo_path']} is not a git repository; "
                                 f"SHA-based review and landing do not apply"))
        else:
            checks.append(_check(f"project {project['name']}", OK,
                                 f"{facts['branch']} @ {(facts['head'] or '')[:12]}"
                                 f"{'' if facts['clean'] else ' (dirty)'}"))
        checks.append(_memory_secrets(project))
    return checks


def _memory_secrets(project):
    findings = []
    for document in MemoryStore(project["id"]).list():
        if redact.contains_secret(document["body"]):
            findings.append(document["relative_path"])
    if findings:
        return _check(f"memory hygiene {project['name']}", FAIL,
                      f"credential-shaped content in {', '.join(findings)}")
    return _check(f"memory hygiene {project['name']}", OK, "no credential-shaped content")


def _gates(store):
    from . import gates
    open_gates = gates.open_gates(store)
    if not open_gates:
        return _check("human gates", OK, "none open")
    return _check("human gates", WARN,
                  f"{len(open_gates)} open: " +
                  ", ".join(f"{g['id']} ({g['kind']})" for g in open_gates[:5]))


def _leases(store):
    stale = [row for row in db.all_rows(store.conn, "SELECT * FROM leases")
             if leases.is_expired(dict(row))]
    if not stale:
        return _check("job leases", OK, "no stale leases")
    return _check("job leases", WARN,
                  f"{len(stale)} expired lease(s); the next run reclaims them")


def render(checks):
    width = max((len(c["name"]) for c in checks), default=10)
    lines = []
    for check in checks:
        marker = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}[check["status"]]
        lines.append(f"[{marker}] {check['name'].ljust(width)}  {check['detail']}")
    return "\n".join(lines)
