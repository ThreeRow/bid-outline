"""Validated runtime configuration; secrets are environment references only."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AppConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    provider: Literal['openai', 'train'] = 'openai'
    base_url: str = 'http://127.0.0.1:8000/v1'
    model: str = ''
    api_key_env: str = 'BID_OUTLINE_API_KEY'
    context_tokens: int = Field(20480, gt=0)
    service_context_tokens: int = Field(20480, gt=0,
        description='Service total model window; sent as max_tokens by the train API')
    max_tokens: int = Field(8192, gt=0,
        description='Local generation reserve; OpenAI output cap; not train wire max_tokens')
    safety_tokens: int = Field(1024, ge=0)
    temperature: float = Field(0, ge=0, le=2)
    max_in_flight: int = Field(2, gt=0)
    workers: int = Field(4, gt=0)
    queue_capacity: int = Field(16, gt=0)
    timeout_seconds: float = Field(120, gt=0)
    transport_retries: int = Field(2, ge=0)
    format_retries: int = Field(1, ge=0)
    supplement_rounds: int = Field(2, ge=0)
    repair_rounds: int = Field(2, ge=0)
    max_task_calls: int = Field(8, gt=0)
    max_run_calls: int = Field(2000, gt=0)
    chunk_chars: int = Field(5000, gt=0)
    text_batch_tokens: int = Field(3000, gt=0)
    long_input_threshold_tokens: int = Field(9000, gt=0)
    split_target_tokens: int = Field(4000, gt=0)
    extraction_mode: Literal['direct', 'anchors'] = 'direct'
    directory_assistance: bool = True
    local_context_chars: int = Field(1200, gt=0)
    stop_after: Literal['candidates', 'organized', 'directory'] = 'directory'
    summary_levels: int = Field(3, ge=1, le=5)
    train_batch_size: int = Field(1, gt=0)
    train_batch_wait_ms: int = Field(20, ge=0, le=1000)
    train_max_batches: int = Field(1, gt=0)
    tokenizer: str | None = None
    screening_reserve_tokens: int = Field(256, ge=0)
    extra_body: dict = Field(default_factory=dict)
    prompt_view: Literal['legacy-json', 'readable-v1'] = 'readable-v1'
    prompt_protocol: Literal['strict', 'compact-v1'] = 'strict'
    directory_protocol: Literal['strict', 'markdown-v1'] = 'strict'
    thinking_default: bool | None = None
    thinking_by_task: dict[str, bool] = Field(default_factory=dict)
    train_enable_thinking: bool = True
    train_level: int = Field(0, ge=0, le=2)
    train_timeout_seconds: int = Field(600, gt=0)
    train_poll_interval_seconds: float = Field(2, gt=0)
    train_poll_timeout_seconds: float = Field(1200, gt=0)
    train_poll_retries: int = Field(2, ge=0)

    def thinking_for(self, task_type: str | None = None) -> bool | None:
        """Task override > explicit default > provider-compatible legacy setting."""
        if task_type in self.thinking_by_task:
            return self.thinking_by_task[task_type]
        if self.thinking_default is not None:
            return self.thinking_default
        if self.provider == 'train':
            return self.train_enable_thinking
        return self.extra_body.get('chat_template_kwargs', {}).get('enable_thinking')

    def protocol_for(self, task_type: str) -> str:
        from .model.text_tasks import TASKS
        if task_type in TASKS:
            return 'text-v1'
        from .model.compact_protocol import effective_protocol
        return effective_protocol(task_type, self.prompt_protocol, self.directory_protocol)

    @model_validator(mode='after')
    def valid(self):
        from .model.task_specs import TASK_TYPES
        unknown = set(self.thinking_by_task) - set(TASK_TYPES)
        if unknown:
            raise ValueError(f'unknown thinking task overrides: {sorted(unknown)}')
        u = urlsplit(self.base_url)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('base_url must be an HTTP(S) URL without credentials/query/fragment')
        self.base_url = self.base_url.rstrip('/')
        if self.provider == 'train':
            # The train routes live at the service root, not below /v1.
            if self.base_url.endswith('/v1'):
                self.base_url = self.base_url[:-3]
            if urlsplit(self.base_url).path not in ('', '/'):
                raise ValueError('train base_url must be the service root (optional /v1)')
            if self.extra_body:
                raise ValueError('train uses train_enable_thinking/level/timeout; extra_body is OpenAI-only')
        if self.max_tokens + self.safety_tokens >= min(self.context_tokens, self.service_context_tokens):
            raise ValueError('generation and safety budget must leave input capacity')
        reserved = {'model', 'messages', 'max_tokens', 'stream', 'temperature', 'authorization', 'api_key', 'headers'}
        if any(k.lower() in reserved or k.startswith('_') for k in self.extra_body):
            raise ValueError('extra_body must not override standard request or authentication fields')
        return self


def load_config(path: Path | None = None) -> AppConfig:
    data = json.loads(Path(path).read_text(encoding='utf-8')) if path else {}
    if data.get('layout_version') == 2:
        data = data['config']
    return AppConfig.model_validate(data)
