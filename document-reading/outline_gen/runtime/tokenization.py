"""CPU Fast Tokenizer counting and lossless token-budget text operations.

The selected Qwen tokenizer is a budgeting reference, not proof of a remote
server's model/template. Only tokenizer assets are loaded, on CPU; Hugging Face
repository sources may download assets into the standard HF cache.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from hashlib import sha256
import os
from threading import RLock

DEFAULT_TOKENIZER = os.environ.get('OUTLINE_GEN_TOKENIZER') or 'Qwen/Qwen3.5-27B'
_SELECTED = ContextVar('outline_tokenizer', default=None)
_LOCK = RLock()


@lru_cache(maxsize=8)
def get_tokenizer(source=None):
    from transformers import AutoTokenizer
    with _LOCK:
        tokenizer = AutoTokenizer.from_pretrained(source or DEFAULT_TOKENIZER, use_fast=True,
            trust_remote_code=False)
        if not tokenizer.is_fast:
            raise ValueError('需要CPU Fast Tokenizer；不允许静默使用slow tokenizer')
        if not tokenizer.chat_template:
            raise ValueError('tokenizer必须提供chat_template以计算消息包装')
        return tokenizer


def selected_tokenizer():
    return _SELECTED.get() or DEFAULT_TOKENIZER


def set_thread_tokenizer(source):
    """ThreadPoolExecutor initializer; task-local context doesn't cross threads."""
    _SELECTED.set(source)


@contextmanager
def tokenizer_scope(source=None):
    token = _SELECTED.set(source or selected_tokenizer())
    try:
        yield
    finally:
        _SELECTED.reset(token)


@lru_cache(maxsize=8)
def _identity(source):
    tokenizer = get_tokenizer(source)
    # Hash complete tokenizer assets, including special tokens and template.
    assets = tokenizer.backend_tokenizer.to_str() + str(tokenizer.chat_template)
    return {'source': str(source), 'is_fast': True, 'device': 'cpu',
            'fingerprint': sha256(assets.encode()).hexdigest(), 'policy': 2}


def tokenizer_identity(source=None):
    return dict(_identity(source or selected_tokenizer()))


@lru_cache(maxsize=2048)
def _count(source, text):
    return len(get_tokenizer(source).encode(text, add_special_tokens=False))


def count_text_tokens(text, *, tokenizer=None):
    source, value = tokenizer or selected_tokenizer(), str(text or '')
    if len(value) > 32768:
        return len(get_tokenizer(source).encode(value, add_special_tokens=False))
    return _count(source, value)


def train_query(system, user):
    return f'【系统指令】\n{system.strip()}\n\n【用户请求】\n{user}' if system and system.strip() else user


def count_request_tokens(request=None, *, system=None, user='', runtime_kind='api', tokenizer=None,
                         chat_template_kwargs=None):
    if request is not None:
        system, user = request.system, request.user
    source = tokenizer or selected_tokenizer()
    tok = get_tokenizer(source)
    if runtime_kind in {'train', 'chat_train'}:
        messages = [{'role': 'user', 'content': train_query(system, user)}]
    else:
        messages = ([{'role':'system','content':system}] if system else []) + [{'role':'user','content':user}]
    return len(tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                       **(chat_template_kwargs or {})))


def token_spans(text, max_tokens, *, tokenizer=None):
    """Contiguous character spans bounded by actual BPE count, without loss.

    Offset boundaries avoid partial Unicode tokens. Each candidate is re-encoded;
    shrinking does not assume BPE prefix counts are monotonic.
    """
    if max_tokens < 1:
        raise ValueError('token预算必须为正数')
    source = tokenizer or selected_tokenizer()
    tok = get_tokenizer(source)
    start = 0
    while start < len(text):
        remaining = text[start:]
        encoded = tok(remaining, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded['offset_mapping']
        if len(encoded['input_ids']) <= max_tokens:
            yield start, len(text)
            break
        end = offsets[max_tokens - 1][1]
        while end > 0 and count_text_tokens(remaining[:end], tokenizer=source) > max_tokens:
            end -= 1
        if end <= 0:
            raise ValueError('下一个原文字符无法放入token预算；不能拆坏Unicode字符')
        yield start, start + end
        start += end


def clip_tokens(text, max_tokens, *, tail=False, marker=''):
    text = str(text or '')
    if max_tokens < 1:
        return ''
    if count_text_tokens(text) <= max_tokens:
        return text
    if tail:
        # Take a suffix candidate from offsets, then recheck assembled output.
        tok = get_tokenizer(selected_tokenizer())
        offsets = tok(text, add_special_tokens=False, return_offsets_mapping=True)['offset_mapping']
        start = offsets[max(0,len(offsets)-max_tokens)][0]
        result = text[start:]
        while result and count_text_tokens(marker + result) > max_tokens:
            result = result[1:]
        return marker + result if count_text_tokens(marker + result) <= max_tokens else ''
    budget = max_tokens - count_text_tokens(marker)
    if budget < 1:
        return ''
    end = next(token_spans(text, budget))[1]
    result = text[:end]
    while result and count_text_tokens(result + marker) > max_tokens:
        result = result[:-1]
    return result + marker
