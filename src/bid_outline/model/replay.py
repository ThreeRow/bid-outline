"""Explicit exact-request reuse for iterative local validation, never fuzzy cache."""

import hashlib
import json
from pathlib import Path

from .client import ModelResponse


def request_key(body: dict) -> str:
    value = {key: val for key, val in body.items() if not key.startswith('_')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ReplayCacheClient:
    """Reuse only successful identical HTTP bodies from explicitly selected runs.

    Current parsing, IDs, budget and domain validation still run in ModelExecutor.
    Different prompt text, model or generation parameters always miss the cache.
    """

    def __init__(self, client, base_url: str, run_dirs: list[Path]):
        self.client = client
        self.entries = {}
        for run_dir in run_dirs:
            for path in sorted((Path(run_dir) / 'calls').rglob('meta.json')):
                metadata = json.loads(path.read_text(encoding='utf-8'))
                if (metadata.get('base_url') != base_url or metadata.get('parse_status') != 'valid'
                        or metadata.get('finish_reason') != 'stop'):
                    continue
                body_path = path.parent / 'request.http.json'
                response_path = path.parent / 'response.model.json'
                if not body_path.exists() or not response_path.exists():
                    continue
                key = request_key(json.loads(body_path.read_text(encoding='utf-8')))
                self.entries[key] = response_path.resolve()

    async def generate(self, request: dict) -> ModelResponse:
        path = self.entries.get(request_key(request))
        if path is None:
            return await self.client.generate(request)
        data = path.read_bytes()
        response = ModelResponse(**json.loads(data))
        response.reused_from = {'response_path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                                'match': 'exact_http_body_and_service'}
        return response

    async def aclose(self):
        await self.client.aclose()
