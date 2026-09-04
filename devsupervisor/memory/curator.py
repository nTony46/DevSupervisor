"""The curation ("sleep") pass.

Reads recent memory and reports, finds duplicates, conflicts, and stale
material, and writes a *proposed* consolidation. It never touches authoritative
memory: the output is a candidate, and adoption is a separate explicit act.
"""

from .. import clock
from . import candidates
from .store import MemoryStore, _tokens

# Two documents this similar are treated as saying the same thing.
DUPLICATE_SIMILARITY = 0.6
STALE_AFTER_DAYS = 90


def _similarity(left, right):
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def analyse(project_id, stale_after_days=STALE_AFTER_DAYS):
    """Report duplicates, conflicts, and stale documents. Read-only."""
    memory = MemoryStore(project_id)
    docs = memory.list()
    duplicates, conflicts, stale = [], [], []

    for index, left in enumerate(docs):
        for right in docs[index + 1:]:
            similarity = _similarity(left["body"], right["body"])
            if similarity >= DUPLICATE_SIMILARITY:
                pair = {
                    "a": left["relative_path"], "b": right["relative_path"],
                    "similarity": round(similarity, 3),
                }
                if left["title"].strip().lower() == right["title"].strip().lower():
                    duplicates.append(pair)
                else:
                    conflicts.append(pair)

    cutoff = clock.now().timestamp() - stale_after_days * 86400
    for doc in docs:
        created = doc.get("created")
        if not created:
            continue
        try:
            if clock.parse(created).timestamp() < cutoff:
                stale.append(doc["relative_path"])
        except ValueError:  # pragma: no cover - malformed frontmatter
            continue

    return {"documents": len(docs), "duplicates": duplicates,
            "conflicts": conflicts, "stale": stale}


def curate(project_id, actor="curator"):
    """Analyse, then write the proposal as a candidate. Returns (report, path)."""
    report = analyse(project_id)
    body = _render_proposal(report)
    path = candidates.propose(
        project_id,
        title=f"Memory curation proposal {clock.now().strftime('%Y-%m-%d')}",
        body=body, area="orchestration", tags=["curation", "memory"],
        source_job=None, kind="curation",
    )
    return report, path


def _render_proposal(report):
    lines = [
        "Proposed consolidation of project memory. Nothing here has been applied:",
        "adopting this candidate is a separate, recorded act.",
        "",
        f"- documents reviewed: {report['documents']}",
        f"- duplicate pairs: {len(report['duplicates'])}",
        f"- conflicting pairs: {len(report['conflicts'])}",
        f"- stale documents: {len(report['stale'])}",
        "",
    ]
    if report["duplicates"]:
        lines.append("## Duplicates — same title, near-identical content")
        lines += [f"- `{d['a']}` ≈ `{d['b']}` ({d['similarity']})" for d in report["duplicates"]]
        lines.append("")
    if report["conflicts"]:
        lines.append("## Conflicts — near-identical content under different titles")
        lines += [f"- `{c['a']}` vs `{c['b']}` ({c['similarity']})" for c in report["conflicts"]]
        lines.append("")
    if report["stale"]:
        lines.append("## Stale — untouched past the freshness window")
        lines += [f"- `{path}`" for path in report["stale"]]
        lines.append("")
    if not (report["duplicates"] or report["conflicts"] or report["stale"]):
        lines.append("No duplicates, conflicts, or stale documents found.")
    return "\n".join(lines)
