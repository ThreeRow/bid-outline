from dataclasses import dataclass, field
from outline_gen.runtime.call_policy import RetryPolicy
from outline_gen.runtime.tokenization import DEFAULT_TOKENIZER


@dataclass(frozen=True)
class ReadingConfig:
    table_format: str = "md"
    model: str = "qwen35"
    max_tokens: int = 8192
    heading_max_tokens: int = 1800
    max_heading_level: int = 12
    toc_scan_blocks: int = 80
    toc_max_tokens: int = 11000
    secondary_prompt: str = "document_secondary_sys.md"
    heading_workers: int = 4
    primary_patterns: tuple[str, ...] = (
        r"^第[〇零一二三四五六七八九十百千万两\d]+(?:章|部分|篇|卷)",
    )
    secondary_patterns: tuple[str, ...] = (
        r"^第[〇零一二三四五六七八九十百千万两\d]+节",
        r"^[一二三四五六七八九十百]+[、．.]",
        r"^\d+[、．.]\s*[^\d]",
        r"^[（(][一二三四五六七八九十百]+[)）]",
    )
    workers: int = 2
    resume: bool = False
    table_chunk_tokens: int = 6000
    heading_context_tokens: int = 1800
    secondary_context_tokens: int = 1800
    tokenizer: str = DEFAULT_TOKENIZER
    temperature: float = 0.0
    extra_body: dict = field(default_factory=dict)
    request_max_tokens: int = 12000
    retries: RetryPolicy = field(default_factory=RetryPolicy)
    def __post_init__(self):
        for name, value in self.__dict__.items():
            if name.endswith('_tokens') and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError(name + "必须为正数")
        import math
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError("temperature必须为有限非负数")
        if self.request_max_tokens is not None and self.request_max_tokens < 1:
            raise ValueError("request_max_tokens必须为正数")
        if self.secondary_prompt not in {
            "document_secondary_sys.md",
            "document_secondary_compare_sys.md",
            "document_secondary_refined_sys.md",
            "document_secondary_final_sys.md",
        }:
            raise ValueError("unsupported secondary prompt")
        if self.table_format not in {"md", "html", "text"}:
            raise ValueError("table_format must be md, html or text")
        import re
        for pattern in self.primary_patterns + self.secondary_patterns:
            re.compile(pattern)
        for name in (
            "max_tokens",
            "heading_max_tokens",
            "max_heading_level",
            "toc_scan_blocks",
            "workers",
            "heading_workers",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
