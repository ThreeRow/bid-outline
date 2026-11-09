"""Local CLI for generation, resume, status, export and explicit reader invocation."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx

from .config import AppConfig, load_config
from .document import read_document
from .export import export_result
from .model.client import FakeModelClient, VllmChatClient, make_model_client
from .storage import ArtifactStore
from .workflow import WorkflowRunner


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another process owns this run directory") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_state(directory: Path) -> tuple[dict, list, list]:
    db = directory / "run.sqlite3"
    if not db.is_file():
        raise ValueError("run database does not exist")
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT data FROM run WHERE id=1").fetchone()
        if row is None:
            raise ValueError("run manifest does not exist")
        tasks = [json.loads(row[0]) for row in connection.execute("SELECT data FROM tasks")]
        attempts = [json.loads(row[0]) for row in connection.execute("SELECT data FROM attempts")]
        return json.loads(row[0]), tasks, attempts
    finally:
        connection.close()


def make_client(config: AppConfig, fake_path: Path | None):
    if fake_path:
        return FakeModelClient(json.loads(fake_path.read_text(encoding="utf-8")))
    return make_model_client(config)


def invoke_reader(args, config: AppConfig) -> Path:
    """Reuse the migrated module in its own import environment, retaining its audit."""
    if config.provider == 'train':
        raise ValueError('train provider currently applies to generation; use --reader reading with existing reading outputs')
    module = args.reading_module.resolve()
    if not (module / "outline_gen/cli/read_document.py").is_file():
        raise ValueError("reading module path is unavailable; pass --reading-module")
    if not config.model or config.model == "fake":
        raise ValueError("DOCX reading requires actual model configuration")
    output = args.run.resolve() / "reading"
    command = [sys.executable, "-m", "outline_gen.cli.read_document", "--input", str(args.input.resolve()),
               "--output-dir", str(output), "--provider", "vllm", "--runtime", "api", "--model", config.model,
               "--base-url", config.base_url, "--workers", str(config.max_in_flight),
               "--heading-workers", str(config.max_in_flight), "--max-tokens", str(config.max_tokens)]
    if config.tokenizer:
        command += ["--tokenizer", config.tokenizer]
    thinking = config.thinking_for()
    if isinstance(thinking, bool):
        command += ["--thinking", "on" if thinking else "off"]
    environment = dict(os.environ)
    if os.environ.get(config.api_key_env):
        environment["OUTLINE_GEN_API_KEY"] = os.environ[config.api_key_env]
    # It is a separate, explicit upstream stage. Read output files, never parse stdout.
    completed = subprocess.run(command, cwd=module, env=environment, check=False)
    if completed.returncode not in (0, 2):
        raise RuntimeError(f"upstream reading failed with exit code {completed.returncode}; inspect {output}")
    return output


async def execute(args) -> dict:
    directory = args.run.resolve()
    if args.command == "resume":
        manifest, _, _ = read_state(directory)
        config = load_config(args.config) if args.config else AppConfig.model_validate(manifest["config_snapshot"])
    else:
        config = load_config(args.config)
    with run_lock(directory):
        document = None
        if args.command == "run":
            input_path = invoke_reader(args, config) if args.reader == "docx" else args.input
            document = read_document(input_path, reader="reading" if args.reader == "docx" else args.reader)
        if args.reuse_calls_from and config.provider == 'train':
            raise ValueError('train does not support cross-run --reuse-calls-from; resume polls its saved ticket')
        client = make_client(config, args.fake_responses)
        if args.reuse_calls_from:
            from .model.replay import ReplayCacheClient
            client = ReplayCacheClient(client, config.base_url, args.reuse_calls_from)
        runner = WorkflowRunner(directory, config, client)
        try:
            result = await runner.run(document, resume=args.command == "resume")
            output = args.output or directory / "exports"
            exports = export_result(result, output)
            return {"status": result["status"], "run": str(directory), "exports": exports,
                    "semantic_quality": "not_evaluated", "simulation": bool(args.fake_responses)}
        finally:
            await runner.aclose()


def doctor(args) -> dict:
    config = load_config(args.config)
    result = {"python": sys.version.split()[0], "executable": sys.executable,
              "provider": config.provider,
              "config_valid": True, "model_configured": bool(config.model and config.model != "fake"
                                                              and not config.model.startswith("REPLACE_")),
              "token_counting": "local_tokenizer" if config.tokenizer else "conservative_estimate",
              "service_checked": False, "semantic_quality": "not_evaluated"}
    if config.provider == 'train':
        result.update(train_enable_thinking=config.train_enable_thinking,
                      thinking_default=config.thinking_default,
                      thinking_by_task=config.thinking_by_task,
                      directory_protocol=config.directory_protocol,
                      model_selection="service-controlled; config.model is an audit label, not sent in train payload",
                      submit_path="/api/v2/chat_train", poll_path="/api/v2/get_resp_data")
        if args.check_service:
            raise ValueError('train has no documented non-generating health endpoint; doctor validates local config only')
        return result
    if args.check_service:
        headers = {}
        if os.environ.get(config.api_key_env):
            headers["Authorization"] = "Bearer " + os.environ[config.api_key_env]
        with httpx.Client(timeout=config.timeout_seconds, headers=headers) as client:
            response = client.get(config.base_url.rstrip("/") + "/models")
            response.raise_for_status()
            model_ids = [model["id"] for model in response.json().get("data", [])]
            result.update(service_checked=True, models=model_ids, configured_model_present=config.model in model_ids)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="可审计的技术标骨干目录工作流")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("doctor", help="检查配置；仅 --check-service 发出连通请求")
    check.add_argument("--config", type=Path)
    check.add_argument("--check-service", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--reader", choices=["reading", "sample", "docx"], default="reading")
    run.add_argument("--reading-module", type=Path, default=Path(__file__).resolve().parents[2] / "document-reading")
    for command in (run, sub.add_parser("resume")):
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--output", type=Path, help="最终结果目录")
        command.add_argument("--config", type=Path)
        command.add_argument("--fake-responses", type=Path, help="预设文本响应JSON；只验证流程，不证明语义质量")
        command.add_argument("--reuse-calls-from", type=Path, action="append", default=[],
                             help="显式复用指定旧运行中完全相同HTTP请求的成功响应；当前契约重新校验")
    for name in ("status", "export"):
        command = sub.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        if name == "export":
            command.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {"run", "resume"}:
            result = asyncio.run(execute(args))
        elif args.command == "doctor":
            result = doctor(args)
        else:
            manifest, tasks, attempts = read_state(args.run)
            if args.command == "export":
                if not manifest.get("result_artifact_id"):
                    raise ValueError("no adopted final result; resume the run first")
                result = export_result(ArtifactStore(args.run).get(manifest["result_artifact_id"]), args.output or args.run / "exports")
            else:
                runtime_path = args.run / "runtime.json"
                runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
                counts = {}
                for task in tasks:
                    stage = counts.setdefault(task["task_type"], {})
                    stage[task["status"]] = stage.get(task["status"], 0) + 1
                result = {"run_id": manifest["run_id"], "status": manifest["status"], "stage": manifest.get("stage"),
                          "tasks": counts, "recorded_attempts": len(attempts),
                          "unprocessed_ranges": manifest.get("unprocessed_ranges", []),
                          "issue_count": manifest.get("issue_count"), "model_stats": runtime or manifest.get("model_stats", {}),
                          "in_flight_note": "持久化状态是最近快照；中断后running任务不代表仍在HTTP请求中"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in {"incomplete", "completed_with_issues"} else (1 if result.get("status") == "failed" else 0)
    except KeyboardInterrupt:
        print("已取消；可使用 resume 恢复。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error, httpx.HTTPError, ExceptionGroup) as exc:
        import traceback
        traceback.print_exc()
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
