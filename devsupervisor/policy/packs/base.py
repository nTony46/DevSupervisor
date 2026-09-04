"""Project policy packs.

The orchestration core is project-agnostic. Everything a specific repository
needs — protected paths, required verification, extra hard rules, risk floors,
gate triggers — lives in a pack, and packs are the only place a project name is
allowed to appear.
"""

from ...errors import PolicyViolation


class PolicyPack:
    """Base pack: permissive defaults, no project knowledge."""

    name = "generic"
    description = "No project-specific rules."

    # Extra hard rules layered on top of the immutable set. A pack may add
    # restrictions; it can never remove one.
    extra_rules = ()

    # Paths a worker must not modify without a human gate.
    protected_paths = ()

    # Commands the project considers proof, run by landing/verification jobs.
    required_verification = ()

    # Job/goal subject matter that must open a human gate before work starts.
    gate_triggers = ()

    # Data boundaries: workers in these roles must not receive these paths.
    clean_room_roles = ()
    clean_room_paths = ()

    # Decisions the repository cannot answer, raised as gates on import.
    # (kind, question, context)
    open_decisions = ()

    def risk_override(self, text="", paths=(), risk=None):
        """Return a risk floor for this subject matter, or None."""
        return None

    def check_paths(self, paths, has_gate_approval=False):
        """Refuse writes to protected paths without a recorded approval."""
        if has_gate_approval:
            return True
        for path in paths:
            text = str(path)
            for protected in self.protected_paths:
                if protected in text:
                    raise PolicyViolation(
                        f"{text} is protected by the {self.name!r} policy pack "
                        f"(matched {protected!r}); this needs a human gate"
                    )
        return True

    def check_clean_room(self, role, paths):
        """Keep held-out or oracle data out of implementation workers."""
        if role not in self.clean_room_roles:
            return True
        for path in paths:
            text = str(path)
            for forbidden in self.clean_room_paths:
                if forbidden in text:
                    raise PolicyViolation(
                        f"role {role!r} must not receive {text} "
                        f"({self.name!r} clean-room boundary)"
                    )
        return True

    def gates_for(self, subject):
        """Gate kinds this pack requires before the given subject may proceed."""
        lowered = (subject or "").lower()
        return [kind for kind, needles in self.gate_triggers
                if any(needle in lowered for needle in needles)]

    def render(self):
        lines = [f"Policy pack: {self.name}", self.description]
        if self.extra_rules:
            lines += ["", "Additional hard rules:"]
            lines += [f"- {rule}" for rule in self.extra_rules]
        if self.protected_paths:
            lines += ["", "Protected paths (human gate required):"]
            lines += [f"- {path}" for path in self.protected_paths]
        if self.required_verification:
            lines += ["", "Required verification:"]
            lines += [f"- {command}" for command in self.required_verification]
        return "\n".join(lines)
