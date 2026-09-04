"""Resolve what the local agent runtime actually supports.

A marketing model string baked into source goes stale silently and fails at the
worst moment — mid-campaign, on a paid call. So the concrete model id and the
effort vocabulary are *discovered* from the installed CLI and its configuration,
and every resolution records where it came from so a run record can be audited
later.
"""

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

BINARY = "claude"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Aliases the CLI documents for "the latest model of this family".
OPUS_ALIAS = "opus"

# Fallback effort vocabulary, used only when the binary cannot be interrogated.
FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"

_EFFORT_HELP = re.compile(r"--effort\s+<level>.*?\(([^)]+)\)", re.DOTALL)
_OPUS_ID = re.compile(r"^claude-opus-[\w.\-]+$")

ENV_MODEL_OVERRIDE = "DEVSUPERVISOR_OPUS_MODEL"
ENV_EFFORT_OVERRIDE = "DEVSUPERVISOR_EFFORT"


@dataclass(frozen=True)
class ModelResolution:
    """What we will actually ask the provider for, and how we know."""

    alias: str
    model_id: str
    model_source: str
    concrete: bool
    effort: str
    effort_source: str
    supported_efforts: tuple
    binary_available: bool
    notes: tuple = ()

    def to_dict(self):
        data = asdict(self)
        data["supported_efforts"] = list(self.supported_efforts)
        data["notes"] = list(self.notes)
        return data

    def describe(self):
        concreteness = "concrete" if self.concrete else "alias only"
        return (f"{self.model_id} ({concreteness}, via {self.model_source}) "
                f"at effort {self.effort} (via {self.effort_source})")


def _load_settings(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def supported_efforts(binary=BINARY):
    """Parse the effort vocabulary out of the CLI's own help text."""
    try:
        completed = subprocess.run([binary, "--help"], capture_output=True, text=True,
                                   timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _EFFORT_HELP.search(completed.stdout or "")
    if not match:
        return None
    levels = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    return levels or None


def _resolve_model(settings, env):
    """Find the strongest Opus identifier this machine knows about."""
    override = env.get(ENV_MODEL_OVERRIDE)
    if override:
        return override, f"{ENV_MODEL_OVERRIDE} environment override", True

    # modelSettings is keyed by concrete model id, which is exactly what we want.
    opus_keys = sorted(key for key in (settings.get("modelSettings") or {})
                       if _OPUS_ID.match(str(key)))
    if opus_keys:
        return opus_keys[-1], "~/.claude/settings.json modelSettings key", True

    configured = str(settings.get("model") or "")
    if _OPUS_ID.match(configured):
        return configured, "~/.claude/settings.json model", True

    from_env = str(env.get("ANTHROPIC_MODEL") or "")
    if _OPUS_ID.match(from_env):
        return from_env, "ANTHROPIC_MODEL environment", True

    # The alias is documented and always valid; it just is not a recorded id.
    return OPUS_ALIAS, "documented CLI alias (no concrete id discoverable)", False


def _resolve_effort(settings, env, model_id, available):
    override = env.get(ENV_EFFORT_OVERRIDE)
    if override:
        return override, f"{ENV_EFFORT_OVERRIDE} environment override"

    per_model = (settings.get("modelSettings") or {}).get(model_id) or {}
    if per_model.get("effortLevel"):
        return per_model["effortLevel"], f"~/.claude/settings.json modelSettings[{model_id}]"
    if settings.get("effortLevel"):
        return settings["effortLevel"], "~/.claude/settings.json effortLevel"
    if env.get("CLAUDE_EFFORT"):
        return env["CLAUDE_EFFORT"], "CLAUDE_EFFORT environment"
    return DEFAULT_EFFORT, "DevSupervisor default"


def resolve_opus(binary=BINARY, settings_path=None, env=None, minimum_effort=DEFAULT_EFFORT):
    """Resolve the Opus model id and effort level to use for reasoning roles.

    `minimum_effort` is a floor, not a target: a machine configured for a *higher*
    effort keeps it. Only a configuration weaker than the floor is raised, and
    the raise is recorded in the notes.
    """
    env = dict(os.environ if env is None else env)
    settings = _load_settings(settings_path or DEFAULT_SETTINGS)
    notes = []

    model_id, model_source, concrete = _resolve_model(settings, env)
    effort, effort_source = _resolve_effort(settings, env, model_id, None)

    discovered = supported_efforts(binary)
    binary_available = discovered is not None
    levels = discovered or FALLBACK_EFFORTS
    if not binary_available:
        notes.append(f"{binary!r} could not be interrogated; using the documented "
                     f"effort vocabulary {list(FALLBACK_EFFORTS)}")

    if effort not in levels:
        notes.append(f"configured effort {effort!r} is not one of {list(levels)}; "
                     f"falling back to {minimum_effort!r}")
        effort, effort_source = minimum_effort, "fallback (configured value unsupported)"

    if minimum_effort in levels and levels.index(effort) < levels.index(minimum_effort):
        notes.append(f"configured effort {effort!r} is below the policy floor "
                     f"{minimum_effort!r}; raised to the floor")
        effort, effort_source = minimum_effort, f"{effort_source} raised to policy floor"

    if not concrete:
        notes.append("no concrete Opus id was discoverable; the alias will be sent and "
                     "the id the provider reports back will be recorded on the run")

    return ModelResolution(
        alias=OPUS_ALIAS, model_id=model_id, model_source=model_source, concrete=concrete,
        effort=effort, effort_source=effort_source, supported_efforts=tuple(levels),
        binary_available=binary_available, notes=tuple(notes),
    )
