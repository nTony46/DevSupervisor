"""Versioned library profiles and display labels, separate from execution state.

No scheduler reads this database implicitly. Saving a reusable profile must not
change a queued/running job or adopt a routing policy behind an operator's back.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..policy.routing import ROUTED_ROLES


class Conflict(ValueError):
    pass


class Settings:
    def __init__(self, execution_db):
        self.path = Path(execution_db).with_suffix('.dashboard.sqlite3')
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS settings (kind TEXT, id TEXT, '
                         'revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id))')
            conn.execute('CREATE TABLE IF NOT EXISTS revisions (kind TEXT, id TEXT, '
                         'revision INTEGER, payload TEXT NOT NULL, PRIMARY KEY(kind,id,revision))')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def all(self, kind):
        with self.connect() as conn:
            return {row[0]: json.loads(row[1]) for row in conn.execute(
                'SELECT id, payload FROM settings WHERE kind=? ORDER BY id', (kind,))}

    def save(self, kind, identifier, payload, expected):
        if type(expected) is not int or expected < 0:
            raise ValueError('A valid revision is required. Reload and try again.')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT revision FROM settings WHERE kind=? AND id=?',
                                 (kind, identifier)).fetchone()
            if (prior[0] if prior else 0) != expected:
                raise Conflict('This item changed in another window. Reopen it before saving.')
            result = {**payload, 'id': identifier, 'revision': expected + 1}
            encoded = json.dumps(result)
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?,?,?)',
                         (kind, identifier, expected + 1, encoded))
            conn.execute('INSERT INTO revisions VALUES (?,?,?,?)',
                         (kind, identifier, expected + 1, encoded))
        return result

    def profile(self, data):
        identifier = data.get('id') or 'agent-' + uuid.uuid4().hex
        if not isinstance(identifier, str) or len(identifier) > 100:
            raise ValueError('Invalid profile id')
        role = data.get('role')
        if role not in ROUTED_ROLES:
            raise ValueError('Choose a supported role')
        if identifier.startswith('preset-') and identifier != 'preset-' + role:
            raise ValueError('A preset must keep its original role; create another agent instead')
        provider = data.get('provider')
        if provider not in ('policy', 'claude-cli', 'codex'):
            raise ValueError('Choose Claude, Codex, or routing policy')
        effort = data.get('effort')
        if effort not in ('default', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('Invalid thinking level')
        values = {key: _text(data.get(key), key, limit, required) for key, limit, required in (
            ('name', 100, True), ('model', 150, False), ('instructions', 16000, False))}
        return self.save('profile', identifier, {**values, 'role': role, 'provider': provider,
                                               'effort': effort}, data.get('revision', 0))

    def rename(self, key, data):
        return self.save('workflow', key, {'name': _text(data.get('name'), 'name', 160, True)},
                         data.get('revision', 0))


def _text(value, field, limit, required):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'Invalid {field} (maximum {limit} characters)')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{field.capitalize()} is required')
    return value
