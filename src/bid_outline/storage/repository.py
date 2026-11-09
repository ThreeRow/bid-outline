"""Transactional SQLite indexes for run recovery and outline adoption."""

import json
import sqlite3
from pathlib import Path
from .artifact_store import ArtifactStore, canonical
from bid_outline.domain import OutlineVersion


class VersionConflict(ValueError):
    """Raised when an outline commit no longer matches its expected parent."""
    pass


class RunRepository:
    """Maintain durable task adoption and version heads in short transactions."""
    schema_version = 1
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.run_dir / 'run.sqlite3', timeout=30)
        # Runs live on a shared NFS mount and can be inspected from other nodes.
        # WAL relies on same-host shared memory; use rollback journals instead.
        self.connection.execute('PRAGMA journal_mode=DELETE')
        self.connection.execute('PRAGMA synchronous=FULL')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, self.schema_version):
            self.connection.close()
            raise ValueError('unsupported repository schema version')
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outlines (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS head (id INTEGER PRIMARY KEY CHECK(id=1), version_id TEXT NOT NULL);
        PRAGMA user_version=1;
        ''')

    def save_run(self, manifest):
        """Persist the current run manifest atomically."""
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO run VALUES (1, ?)', (canonical(manifest).decode(),))

    def get_run(self):
        """Read the manifest, or return an empty mapping before initialization."""
        row = self.connection.execute('SELECT data FROM run WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def get_task(self, task_id):
        """Return the latest state of a logical task, if present."""
        row = self.connection.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def result_ref(self, task_id):
        task = self.get_task(task_id)
        if not task or task.get('status') != 'succeeded':
            return None
        aid = task['accepted_artifact_id']
        ArtifactStore(self.run_dir).get(aid)
        return {'task_id': task_id, 'call_id': task.get('accepted_call_id'), 'artifact_id': aid}

    def list_tasks(self):
        """Return task records in their persisted iteration order."""
        return [json.loads(r[0]) for r in self.connection.execute('SELECT data FROM tasks ORDER BY rowid')]

    def upsert_task(self, task_id, task_type, fingerprint, status, **metadata):
        """Update task state, discarding incompatible prior-fingerprint metadata."""
        old = self.get_task(task_id) or {}
        if old.get('fingerprint') != fingerprint:
            old = {}
        data = {**old, **metadata, 'task_id': task_id, 'task_type': task_type, 'fingerprint': fingerprint, 'status': status}
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)', (task_id, canonical(data).decode()))

    def record_attempt(self, call_id, task_id, data):
        """Merge audit updates while preventing cross-task attempt reassignment."""
        row = self.connection.execute('SELECT task_id, data FROM attempts WHERE id=?', (call_id,)).fetchone()
        if row and row[0] != task_id:
            raise ValueError('attempt ID belongs to another task')
        merged = {**(json.loads(row[1]) if row else {}), **data, 'call_id': call_id, 'task_id': task_id}
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)', (call_id, task_id, canonical(merged).decode()))

    def list_attempts(self, task_id=None):
        """Return all recorded attempts, optionally restricted to one task."""
        sql = 'SELECT data FROM attempts' + (' WHERE task_id=?' if task_id else '') + ' ORDER BY rowid'
        return [json.loads(r[0]) for r in self.connection.execute(sql, (task_id,) if task_id else ())]

    def commit_task(self, task_id, artifact_id):
        """Adopt only a verified artifact and prevent conflicting double adoption."""
        ArtifactStore(self.run_dir).get(artifact_id)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            task = self.get_task(task_id)
            if task is None:
                raise ValueError('unknown task')
            if task.get('status') == 'succeeded' and task.get('accepted_artifact_id') != artifact_id:
                raise ValueError('task already adopted another result')
            task.update(status='succeeded', accepted_artifact_id=artifact_id)
            self.connection.execute('UPDATE tasks SET data=? WHERE id=?', (canonical(task).decode(), task_id))

    def get_outline(self):
        """Read the currently adopted outline snapshot."""
        row = self.connection.execute('SELECT o.data FROM outlines o JOIN head h ON o.id=h.version_id WHERE h.id=1').fetchone()
        return json.loads(row[0]) if row else None

    def get_outline_version(self, version_id):
        """Read a historical snapshot without moving the adopted head."""
        row = self.connection.execute('SELECT data FROM outlines WHERE id=?', (version_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit_outline(self, version, expected_parent):
        """Validate and adopt a version using a transactional parent comparison."""
        validated = OutlineVersion.model_validate(version).model_dump(mode='json')
        if validated['parent_version_id'] != expected_parent:
            raise VersionConflict('version parent does not match precondition')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            current = self.get_outline()
            actual = current['version_id'] if current else None
            if actual != expected_parent:
                raise VersionConflict('outline head changed')
            self.connection.execute('INSERT INTO outlines VALUES (?, ?)', (validated['version_id'], canonical(validated).decode()))
            self.connection.execute('INSERT OR REPLACE INTO head VALUES (1, ?)', (validated['version_id'],))

    def close(self):
        """Close the database connection after all pending operations finish."""
        self.connection.close()
