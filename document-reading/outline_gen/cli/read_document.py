"""Independent reading CLI; does not launch the legacy generation pipeline."""

from __future__ import annotations
import argparse
from pathlib import Path
from outline_gen.runtime.call_policy import (CallPolicy, add_policy_arguments, retry_from_args)
from outline_gen.config import PROVIDERS, resolve_provider, resolve_runtime_kind
from outline_gen.document_reading import ReadingConfig, read_document
from outline_gen.runtime.factory import create_runtime
from outline_gen.runtime.port import RuntimeConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立 DOCX 保真结构解析与阅读文本生成")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="train")
    parser.add_argument("--runtime", choices=["auto", "api", "train"], default="auto")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--table-format", choices=["md", "html", "text"], default="md")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--heading-max-tokens", type=int, default=1800)
    parser.add_argument("--thinking", choices=["auto", "on", "off"], default="auto",
                        help="vLLM chat_template_kwargs.enable_thinking; auto follows deployment")
    parser.add_argument("--workers", type=int, default=2, help="表格并发")
    parser.add_argument(
        "--heading-workers", type=int, default=4, help="一级目标组／二级章节的并发上限"
    )
    parser.add_argument(
        "--primary-pattern",
        action="append",
        help="一级默认形式正则，可重复；提供时替换默认池",
    )
    parser.add_argument(
        "--secondary-pattern",
        action="append",
        help="二级默认形式正则，可重复；提供时替换默认池",
    )
    parser.add_argument("--table-chunk-tokens", type=int, default=6000)
    parser.add_argument("--heading-context-tokens", type=int, default=256)
    parser.add_argument("--secondary-context-tokens", type=int, default=96)
    parser.add_argument("--toc-max-tokens", type=int, default=11000)
    parser.add_argument("--resume", action="store_true")
    add_policy_arguments(parser)
    args = parser.parse_args(argv)
    if args.thinking != "auto" and args.provider not in ("vllm", "api"):
        parser.error("--thinking requires the OpenAI-compatible vLLM/API runtime")
    extra_body = {} if args.thinking == "auto" else {
        "chat_template_kwargs": {"enable_thinking": args.thinking == "on"}}
    if args.request_max_tokens is None:
        args.request_max_tokens = 12000
    provider = resolve_provider(
        args.provider, model=args.model, api_key=args.api_key, base_url=args.base_url
    )
    config = ReadingConfig(
        table_format=args.table_format,
        table_chunk_tokens=args.table_chunk_tokens,
        toc_max_tokens=args.toc_max_tokens,
        heading_context_tokens=args.heading_context_tokens,
        secondary_context_tokens=args.secondary_context_tokens,
        tokenizer=args.tokenizer, temperature=args.temperature,
        request_max_tokens=args.request_max_tokens, retries=retry_from_args(args),
        model=provider["model"],
        max_tokens=args.max_tokens,
        heading_max_tokens=args.heading_max_tokens,
        extra_body=extra_body,

        workers=args.workers,
        heading_workers=args.heading_workers,
        resume=args.resume,
        primary_patterns=(
            tuple(args.primary_pattern)
            if args.primary_pattern
            else ReadingConfig.primary_patterns
        ),
        secondary_patterns=(
            tuple(args.secondary_pattern)
            if args.secondary_pattern
            else ReadingConfig.secondary_patterns
        ),
    )
    runtime = create_runtime(
        RuntimeConfig(call_policy=CallPolicy(args.tokenizer, args.temperature, args.max_tokens, retry_from_args(args), args.request_max_tokens),
            kind=resolve_runtime_kind(provider, args.runtime),
            model=provider["model"],
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            max_in_flight=max(args.workers, args.heading_workers),
            extra_body=extra_body,
        )
    )
    result = read_document(args.input, args.output_dir, runtime=runtime, config=config)
    print(f"{result['status']}: {args.output_dir.resolve() / 'reading.md'}")
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
