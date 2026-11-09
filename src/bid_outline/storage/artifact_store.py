"""Content-addressed files with atomic, non-overwriting publication."""

import hashlib
import json
import os
from pathlib import Path
import re
import uuid


def canonical(data):
    """Serialize deterministic UTF-8 bytes for stable content identities."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class ArtifactStore:
    """Publish immutable content-addressed artifacts and verify every read."""
    def __init__(self, run_dir):
        self.directory = Path(run_dir) / 'artifacts'
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, kind: str, data: dict, parents=None, task_id=None) -> str:
        """Atomically publish an artifact without overwriting existing content."""
        envelope = {'schema_version': 1, 'kind': kind, 'data': data, 'parents': parents or [], 'task_id': task_id}
        payload = canonical(envelope)
        aid = 'a-' + hashlib.sha256(payload).hexdigest()
        destination = self.directory / (aid + '.json')
        if destination.exists():
            self.get(aid)
            return aid
        # Unique temporary files are intentionally retained as crash evidence.
        temporary = self.directory / ('.pending-' + uuid.uuid4().hex)
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)  # atomic publication without overwrite
        except FileExistsError:
            self.get(aid)
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return aid

    def get(self, artifact_id) -> dict:
        """Return artifact data after validating its ID and file content hash."""
        return self.envelope(artifact_id)['data']

    def envelope(self, artifact_id) -> dict:
        """Return the verified payload and direct parent/producer metadata."""
        if not re.fullmatch(r'a-[0-9a-f]{64}', artifact_id):
            raise ValueError('invalid artifact ID')
        raw = (self.directory / (artifact_id + '.json')).read_bytes()
        if 'a-' + hashlib.sha256(raw).hexdigest() != artifact_id:
            raise ValueError('artifact hash mismatch')
        return json.loads(raw)
