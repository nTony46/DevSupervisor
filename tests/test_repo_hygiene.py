"""The repository itself is a deliverable, so it gets assertions too."""

import subprocess

from tests.support import ROOT, HarnessTestCase

# Files that contain credential-shaped strings on purpose: the redactor's own
# pattern table, and the tests that prove redaction happens. Every other file in
# the repository must be clean.
DELIBERATE_FIXTURES = frozenset({
    "devsupervisor/memory/redact.py",
    "devsupervisor/policy/immutable.py",
    "tests/test_memory.py",
    "tests/test_artifacts.py",
    "tests/test_context_compiler.py",
})


class RepoHygieneTests(HarnessTestCase):
    def test_this_is_a_standalone_git_repository(self):
        toplevel = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True)
        self.assertEqual(toplevel.returncode, 0)
        self.assertEqual(toplevel.stdout.strip(), str(ROOT))

    def test_no_secret_shaped_content_is_committed(self):
        from devsupervisor.memory import redact
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                                 capture_output=True, text=True).stdout.split()
        offenders = []
        for relative in tracked:
            path = ROOT / relative
            if not path.is_file() or path.suffix in (".png", ".jpg", ".ico"):
                continue
            text = path.read_text(errors="ignore")
            if relative in DELIBERATE_FIXTURES:
                continue
            if redact.contains_secret(text):
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_the_package_imports_without_side_effects(self):
        result = subprocess.run(
            ["python3", "-c", "import sys; sys.path.insert(0, '.'); import devsupervisor"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
