"""Bounded, read-only observations of explicitly matched Claude subagents.

This is activity evidence, not a scheduler lease. Never imports transcript text
into the API, never changes the execution ledger, and never launches a process.
"""
import json
import re
import threading
from pathlib import Path

from .. import clock

RECENT_SECONDS = 900


def _records(raw):
    for line in raw.decode('utf-8', errors='replace').splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except ValueError:
            continue  # A concurrently appended or bounded partial line.


class ClaudeActivity:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home() / '.claude' / 'projects'
        self.cache = {}
        self.lock = threading.Lock()

    def observe(self, repo, job_ids):
        key = (str(repo), tuple(sorted(job_ids)))
        now = clock.now()
        with self.lock:
            cached = self.cache.get(key)
            if cached and 0 <= (now - cached[0]).total_seconds() < 5:
                return cached[1]
            try:
                result = self._scan(repo, set(job_ids), now)
            except OSError:
                result = {}
            # Bound memory across project/workflow changes.
            if len(self.cache) > 32:
                self.cache.clear()
            self.cache[key] = (now, result)
            return result

    def _scan(self, repo, job_ids, now):
        directory = self.root / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo))
        if not directory.is_dir() or not job_ids:
            return {}
        result = {}
        files = sorted(directory.glob('*/subagents/agent-*.jsonl'),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:64]
        for path in files:
            if now.timestamp() - path.stat().st_mtime > 86400:
                continue
            with path.open('rb') as stream:
                first = list(_records(stream.read(65536)))
                stream.seek(max(0, path.stat().st_size - 262144))
                tail = list(_records(stream.read(262144)))
            initial = next((r for r in first if r.get('type') == 'user'), {})
            if initial.get('cwd') != str(repo):
                continue
            content = initial.get('message', {}).get('content', '')
            if isinstance(content, list):
                content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
            if not isinstance(content, str):
                continue
            matches = [j for j in job_ids if '/prompts/' + j + '.md' in content]
            if len(matches) != 1:
                continue
            job_id = matches[0]
            messages = [r for r in tail if r.get('type') in ('assistant', 'user') and r.get('timestamp')]
            if not messages:
                continue
            last = messages[-1]
            try:
                age = (now - clock.parse(last['timestamp'])).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < 0 or age > 86400:
                continue
            ended = last.get('message', {}).get('stop_reason') == 'end_turn'
            status = 'COMPLETE' if ended else 'ACTIVE' if age <= RECENT_SECONDS else 'STALE'
            model = next((r.get('message', {}).get('model') for r in reversed(messages)
                          if r.get('message', {}).get('model')), None)
            evidence = {'status': status, 'last_seen': last['timestamp'],
                        'source': 'Claude subagent transcript', 'provider': 'claude-cli',
                        'model': model, 'started_at': initial.get('timestamp')}
            prior = result.get(job_id)
            if not prior or clock.parse(evidence['last_seen']) > clock.parse(prior['last_seen']):
                result[job_id] = evidence
        return result
