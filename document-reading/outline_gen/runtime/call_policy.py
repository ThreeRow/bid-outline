"""Shared model-call configuration; existing retry layers remain independent."""
from dataclasses import dataclass, field, asdict
from functools import wraps
from inspect import signature
import math
from .tokenization import DEFAULT_TOKENIZER, tokenizer_scope


@dataclass(frozen=True)
class RetryPolicy:
    model_attempts: int = 2
    contract_retries: int = 1
    syntax_repairs: int = 1
    batch_transport_retries: int = 2
    invoke_attempts: int = 3
    train_generation_attempts: int = 3
    train_transport_retries: int = 3

    def __post_init__(self):
        for name, value in asdict(self).items():
            minimum = 1 if name in {'model_attempts','invoke_attempts','train_generation_attempts'} else 0
            if not isinstance(value,int) or value < minimum:
                raise ValueError(f'{name}必须为不小于{minimum}的整数')


@dataclass(frozen=True)
class CallPolicy:
    tokenizer: str = DEFAULT_TOKENIZER
    temperature: float = 0.2
    max_tokens: int = 8192
    retries: RetryPolicy = field(default_factory=RetryPolicy)
    request_max_tokens: int | None = None

    def __post_init__(self):
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError('temperature必须为有限非负数')
        if self.request_max_tokens is not None and self.request_max_tokens < 1:
            raise ValueError("request_max_tokens必须为正数")
        if self.max_tokens < 1:
            raise ValueError('max_tokens必须为正数')


# Preferences, not separate hard ceilings; every call also obeys global cap.
STAGE_OUTPUT_TOKENS = {
    'reading':8192, 'abc':4096, 'heading':1800, 'primary_heading':256,
    'project_identity':900, 'screen':5000, 'canonical':3500,
    'skeleton':8192, 'point_topics':1800, 'route':4000, 'workcard':2200,
    'router':3072, 'cognitive_map':6144, 'expand':4096,
}


def stage_tokens(stage, global_max):
    return min(global_max, STAGE_OUTPUT_TOKENS[stage])


def runtime_policy(runtime):
    return getattr(runtime,'call_policy',None) or CallPolicy()


def configured_tokens(function):
    """Use a task's tokenizer even in thread-pool stage entrypoints."""
    call_signature = signature(function)
    @wraps(function)
    def run(*args, **kwargs):
        config = kwargs.get('config')
        if config is None:
            try:
                config = call_signature.bind_partial(*args, **kwargs).arguments.get('config')
            except TypeError:
                pass
        source = getattr(config,'tokenizer',None)
        if source is None:
            runtime = kwargs.get('runtime')
            source = (kwargs.get("call_policy") or runtime_policy(runtime)).tokenizer
        with tokenizer_scope(source):
            return function(*args, **kwargs)
    return run


def add_policy_arguments(parser):
    parser.add_argument('--tokenizer', default=DEFAULT_TOKENIZER, help='CPU Fast Tokenizer本地目录或HF仓库ID；默认Qwen，远端资源自动下载并缓存')
    parser.add_argument('--request-max-tokens', type=int, default=None, help='完整消息输入token预算，不含输出；未指定时reading/ABC=12000、融合=16000，其他共享调用无额外输入门禁')
    for name, value in asdict(RetryPolicy()).items():
        parser.add_argument('--'+name.replace('_','-'),type=int,default=value,
            help='总尝试次数' if name.endswith('attempts') else '额外重试／修复次数')


def retry_from_args(args):
    return RetryPolicy(**{name:getattr(args,name) for name in RetryPolicy.__dataclass_fields__})


def validate_request_budget(request, runtime):
    from .tokenization import count_request_tokens
    from .errors import ModelContextLengthError
    policy = runtime_policy(runtime)
    kind = getattr(runtime, 'budget_runtime_kind', 'train' if getattr(runtime, 'plain_chat_only', False) else 'api')
    count = count_request_tokens(request, runtime_kind=kind, tokenizer=policy.tokenizer,
                                 chat_template_kwargs=getattr(runtime, 'extra_body', {}).get('chat_template_kwargs', {}))
    if policy.request_max_tokens is not None and count > policy.request_max_tokens:
        raise ModelContextLengthError(f'完整消息输入{count} token超过配置预算{policy.request_max_tokens}；未提交模型请求')
    return count
