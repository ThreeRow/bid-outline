"""Complete message budget with explicit unverified-tokenizer provenance."""
import json
import re
from collections import OrderedDict
from pathlib import Path
from bid_outline.config import AppConfig


class TaskExecutionError(RuntimeError):
    pass


class BudgetExceeded(TaskExecutionError):
    pass


class TokenBudget:
    """Reject oversized complete prompts without dropping any source material."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.tokenizer = None
        if config.tokenizer:
            if not Path(config.tokenizer).exists():
                raise ValueError('tokenizer must be an existing local path; downloads are forbidden')
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(
                config.tokenizer, local_files_only=True, trust_remote_code=False, use_fast=True)
            if not self.tokenizer.is_fast:
                raise ValueError('generation requires a CPU fast tokenizer')
        self._counts = OrderedDict()

    @property
    def window(self):
        return min(self.config.context_tokens, self.config.service_context_tokens)

    def text_tokens(self, text):
        """Cheap cached unit measurement; complete rendered requests remain authoritative."""
        key = ('text', text)
        if key not in self._counts:
            count = (len(self.tokenizer.encode(text, add_special_tokens=False))
                     if self.tokenizer else len(text.encode('utf-8')))
            self._cache(key, count)
        return self._counts[key]

    def _cache(self, key, count):
        self._counts[key] = count
        if len(self._counts) > 1024:
            self._counts.popitem(last=False)

    def count(self, messages, *, task_type=None):
        if self.config.provider == 'train':
            from .train_client import train_query
            messages = [{'role': 'user', 'content': train_query(messages)}]
        raw_kwargs = self.config.extra_body.get('chat_template_kwargs', {})
        if not isinstance(raw_kwargs, dict):
            raise ValueError('chat_template_kwargs must be a dictionary')
        kwargs = dict(raw_kwargs)
        thinking = self.config.thinking_for(task_type)
        if thinking is not None:
            kwargs['enable_thinking'] = thinking
        if {'tokenize', 'add_generation_prompt', 'conversation'} & kwargs.keys():
            raise ValueError('chat_template_kwargs cannot override budget tokenization controls')
        key = ('request', json.dumps([messages, kwargs], ensure_ascii=False, sort_keys=True))
        if key not in self._counts:
            if self.tokenizer:
                count = len(self.tokenizer.apply_chat_template(
                    messages, tokenize=True, add_generation_prompt=True, **kwargs))
            else:
                # Retained only for explicit offline/fake clients; the production
                # client factory rejects a missing tokenizer before any request.
                count = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 64 * len(messages) + 64
            self._cache(key, count)
        return self._counts[key]

    def allowance(self, empty_body_messages, max_tokens, *, task_type=None):
        """Subtract task instructions, wrappers AND the candidate's dynamic context first."""
        return max(0, self.window - max_tokens - self.config.safety_tokens -
                   self.count(empty_body_messages, task_type=task_type))

    def breakdown(self, messages, *, task_type=None):
        """Diagnostic component counts, explicitly non-additive at token boundaries."""
        sections = {}
        fixed = []
        pattern = re.compile(r'(\[\[输入数据开始(?P<tag>:[^\]]+|)\]\]\n)'
                             r'(?P<label>[^\n]+)：\n(?P<body>.*?)'
                             r'(\n\[\[输入数据结束(?P=tag)\]\])', re.S)
        for message in messages:
            def remove(match):
                sections[match['label']] = self.text_tokens(match['body'])
                return match[1] + match['label'] + '：\n' + match[5]
            fixed.append({**message, 'content': pattern.sub(remove, message['content'])})
        return {'sections_text_tokens_nonadditive': sections,
                'prompt_and_empty_wrappers_tokens': self.count(fixed, task_type=task_type)}

    def check(self, messages: list[dict], max_tokens: int, *, task_type=None) -> dict:
        """Return budget provenance or raise before a model request is attempted."""
        count = self.count(messages, task_type=task_type)
        if self.tokenizer:
            method = 'local_chat_template; server compatibility unverified'
        else:
            method = 'conservative_estimate_utf8_bytes_plus_message_overhead; not exact'
        total = count + max_tokens + self.config.safety_tokens
        report = dict(input_tokens=count, generation_tokens=max_tokens, safety_tokens=self.config.safety_tokens,
            total_tokens=total, context_tokens=self.window,
            logical_context_tokens=self.config.context_tokens,
            service_context_tokens=self.config.service_context_tokens,
            generation_includes_thinking=True,
            enable_thinking=self.config.thinking_for(task_type),
            remaining_tokens=self.window-total, method=method, exact=False)
        if total > self.window:
            raise BudgetExceeded(f'complete request budget {total} exceeds {self.window}; {method}')
        return report
