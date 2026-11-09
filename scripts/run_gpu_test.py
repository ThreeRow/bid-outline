"""Run one real DOCX case on the node hosting vLLM, retaining stage logs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import fcntl
import socket
import sqlite3
import re
import time
import math
import traceback
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

MANIFEST_FIELDS = {'request.json': 'request', 'config.json': 'config',
                   'environment.json': 'environment', 'models.json': 'service',
                   'execution.json': 'execution', 'stages.json': 'stages',
                   'status.latest.json': 'progress'}


def layout_v2(root: Path) -> bool:
    path = root / 'manifest.json'
    if not path.exists():
        return False
    return json.loads(path.read_text(encoding='utf-8')).get('layout_version') == 2


def overview(root: Path, manifest: dict) -> None:
    execution = manifest.get('execution', {})
    lines = ['# Run overview', '', f"Status: **{execution.get('status', 'created')}**", '',
             f"Input: {manifest.get('request', {}).get('input', '')}", '',
             '[Manifest](manifest.json) · [Events](events.jsonl) · [Calls](generation/calls.md)', '',
             '[Backbone](results/backbone.md) · [Unresolved](results/unresolved.md) · [Provenance](results/provenance.json)', '',
             '| Stage | Status | Exit | Log |', '|---|---|---|---|']
    for name, stage in manifest.get('stages', {}).items():
        log = stage.get('log', '')
        try:
            log = str(Path(log).relative_to(root))
        except ValueError:
            pass
        lines.append(f"| {name} | {stage.get('status')} | {stage.get('exit_code', '')} | [log]({log}) |")
    progress = manifest.get('progress', {})
    lines += ['', f"Unprocessed: {progress.get('unprocessed_count', 'pending')} · Failed attempts (including recovered): {progress.get('attempt_errors', 0)}", '',
              'Historical failures are not necessarily unresolved. See results/unresolved.md for final issues.', '']
    if manifest.get('kind') == 'batch':
        lines = ['# Batch overview', '', f"Status: **{execution.get('status', 'created')}**", '',
                 f"Simultaneous files: {manifest['request']['batch_size']} · Model requests per file: {manifest['request']['max_in_flight']}", '',
                 '| File | Status | Result |', '|---|---|---|']
        for item in manifest.get('items', []):
            label = Path(item['input']).name.replace('|', '\\|')
            lines.append(f"| {label} | {item['status']} | [overview]({item['directory']}/overview.md) |")
        lines += ['', '[Manifest](manifest.json) · [Events](events.jsonl)', '']
    result_manifest = root / 'generation/manifest.json'
    if result_manifest.exists():
        generated = json.loads(result_manifest.read_text(encoding='utf-8'))
        lines += ['## Final generation status', '', f"{generated.get('status')} · issues: {generated.get('issue_count', 'pending')}", '']
        for item in generated.get('unprocessed_ranges', []):
            lines.append(f"- {item.get('task_type', item.get('scope', 'unknown'))}: {item.get('error', item.get('diagnostic', ''))}")
    temporary = root / ('.overview-' + uuid4().hex)
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    os.replace(temporary, root / 'overview.md')


DEFAULTS = {
    "base_url": "http://127.0.0.1:8000/v1", "model": None, "tokenizer": None,
    "context_tokens": None, "context_budget": 20000, "max_tokens": 8192,
    "heading_max_tokens": 2048, "max_in_flight": 4, "timeout": 600.0,
    "thinking": "off", "chunk_chars": 5000, "reading_input_tokens": 12000,
    "table_chunk_tokens": 4000, "poll_interval": 5.0, "reader": "auto",
    "reuse_calls_from": [],
}


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S_%f")


def safe_name(value: str) -> str:
    value = re.sub(r'[^\w\u4e00-\u9fff.()（）-]+', '_', value).strip('._')
    return value[:60] or "test"


def create_run_directory(parent: Path, name: str) -> Path:
    """Timestamp names; an actual same-microsecond collision uses a simple counter."""
    parent.mkdir(parents=True, exist_ok=True)
    stem = safe_name(name) + "_" + timestamp()
    for index in range(10000):
        path = parent / (stem if index == 0 else f"{stem}_{index + 1}")
        try:
            path.mkdir()
            return path.resolve()
        except FileExistsError:
            continue
    raise FileExistsError("无法分配新的运行目录")


def record_event(output: Path, name: str, data: dict) -> None:
    event_type = name.removesuffix('.jsonl')
    if layout_v2(output):
        name = 'events.jsonl'
    with (output / name).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps({"recorded_at": now(), "event": event_type, **data}, ensure_ascii=False) + "\n")
        handle.flush()


def notify(output: Path, level: str, message: str, **details) -> None:
    record_event(output, "alerts.jsonl", {"level": level, "message": message, **details})
    print(f"[{level}] {message}", flush=True)


def save(path: Path, value: object) -> None:
    if path.name in MANIFEST_FIELDS and layout_v2(path.parent):
        root = path.parent
        with (root / 'logs/manifest.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest_path = root / 'manifest.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest[MANIFEST_FIELDS[path.name]] = value
            save(manifest_path, manifest)
            overview(root, manifest)
        if path.name in ('execution.json', 'stages.json'):
            record_event(root, MANIFEST_FIELDS[path.name] + '.jsonl', value)
        return
    if layout_v2(path.parent) and (path.name.startswith('launch.') or path.name.startswith('execution.')):
        record_event(path.parent, path.name.split('.')[0] + '.jsonl', value)
        return
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path, default=None):
    if path.name in MANIFEST_FIELDS and layout_v2(path.parent):
        return json.loads((path.parent / 'manifest.json').read_text(encoding='utf-8')).get(MANIFEST_FIELDS[path.name], default)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def run_test(args) -> int:
    for key, value in DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    project = Path(__file__).resolve().parents[1]
    source = args.input.resolve()
    reader = args.reader
    if reader == "auto":
        reader = "docx" if source.suffix.lower() == ".docx" else "reading"
    reading_source = source / "document.json" if source.is_dir() else source
    if reader == "docx" and (not source.is_file() or source.suffix.lower() != ".docx"):
        raise ValueError("--input must be an existing DOCX file")
    if reader == "reading" and not reading_source.is_file():
        raise ValueError("--input must point to reading output or document.json")
    if args.max_in_flight < 1:
        raise ValueError("--max-in-flight must be positive")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    config_path = output / ('manifest.json' if layout_v2(output) else 'config.json')
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(project / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    environment["OUTLINE_GEN_API_TIMEOUT_SECONDS"] = str(args.timeout)
    no_proxy = environment.get("NO_PROXY", environment.get("no_proxy", ""))
    environment["NO_PROXY"] = environment["no_proxy"] = ",".join(
        item for item in (no_proxy, "127.0.0.1", "localhost", "::1") if item
    )
    key = environment.get("BID_OUTLINE_API_KEY")
    if key:
        environment["OUTLINE_GEN_API_KEY"] = key
    headers = {"Authorization": "Bearer " + key} if key else {}
    base_url = args.base_url.rstrip("/")
    with httpx.Client(timeout=30, headers=headers, trust_env=False) as client:
        response = client.get(base_url + "/models")
        (logs / f"models.{args.attempt}.raw.txt").write_text(response.text, encoding="utf-8")
        response.raise_for_status()
        listing = response.json()
    save(output / "models.json", listing)
    models = listing.get("data", [])
    selected = [m for m in models if not args.model or m["id"] == args.model]
    if len(selected) != 1:
        raise ValueError("Specify --model using one of the IDs in models.json")
    model = selected[0]
    tokenizer = args.tokenizer
    if tokenizer is None:
        tokenizer = next((Path(p) for p in (model.get("root"), model["id"])
                          if p and Path(p).is_dir()), None)
    if tokenizer is None or not tokenizer.is_dir():
        raise ValueError("Pass --tokenizer with the local model/tokenizer directory used by vLLM")
    reported = model.get("max_model_len")
    reported = int(reported) if reported else None
    server_limit = args.context_tokens or reported
    if not server_limit:
        raise ValueError("/models omitted max_model_len; pass actual --context-tokens from vLLM configuration")
    if reported and server_limit > reported:
        raise ValueError("--context-tokens exceeds the server-reported max_model_len")
    # Retain the application's approximately 20K design budget on longer-context servers.
    context = min(server_limit, args.context_budget)
    generation = args.max_tokens
    reading_input = min(args.reading_input_tokens, context - generation - 1024)
    if reading_input < 4000:
        raise ValueError("Server context is too small for this initial test configuration")
    config = json.loads((project / "configs/local.example.json").read_text(encoding="utf-8"))
    config.update(base_url=base_url, model=model["id"], tokenizer=str(tokenizer.resolve()),
                  context_tokens=context, service_context_tokens=server_limit,
                  max_tokens=generation, safety_tokens=1024,
                  max_in_flight=args.max_in_flight, workers=max(4, args.max_in_flight),
                  queue_capacity=max(16, args.max_in_flight * 4), timeout_seconds=args.timeout,
                  chunk_chars=args.chunk_chars,
                  extra_body={} if args.thinking == "auto" else {
                      "chat_template_kwargs": {"enable_thinking": args.thinking == "on"}})
    previous_config = read_json(output / "config.json")
    if previous_config is not None and previous_config != config:
        raise ValueError("服务或运行配置发生变化，请新建测试目录，不能覆盖恢复配置")
    save(output / "config.json", config)
    save(output / "environment.json", {
        "started_at": datetime.now(timezone.utc).isoformat(), "python": sys.version,
        "executable": sys.executable, "input": str(source),
        "packages": {name: importlib.metadata.version(name) for name in ("httpx", "pydantic", "transformers", "docling")},
        "server_context_limit": server_limit, "generation_context_budget": context,
        "thinking": args.thinking,
    })
    stages = read_json(output / "stages.json", {})
    last_errors = compact_status(output)["attempt_errors"]

    def run_stage(name: str, command: list[str], cwd: Path) -> int:
        nonlocal last_errors
        previous = stages.get(name, {})
        generation_status = read_json(output / "generation/manifest.json", {}).get("status")
        reusable = name == "reading" or generation_status in ("completed", "completed_with_issues")
        if previous.get("exit_code") in (0, 2) and name != "doctor" and reusable:
            print(f"[{name}] reuse completed stage", flush=True)
            return previous["exit_code"]
        if name == "reading" and (output / "reading/manifest.json").exists():
            command = command + ["--resume"]
        if name == "generation" and (output / "generation/run.sqlite3").exists():
            command = [sys.executable, "-u", "-m", "bid_outline", "resume", "--run", str(output / "generation")]
        if name == "generation":
            if layout_v2(output):
                command += ['--output', str(output / 'results')]
            for prior_run in args.reuse_calls_from:
                command += ["--reuse-calls-from", str(prior_run)]
        history = previous.get("history", []) + ([{k: v for k, v in previous.items() if k != "history"}] if previous else [])
        log = logs / f"{name}.{args.attempt}.log"
        stages[name] = {"command": command, "cwd": str(cwd), "log": str(log),
                        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "history": history}
        save(output / "stages.json", stages)
        print(f"[{name}] log: {log}", flush=True)
        with log.open("x", encoding="utf-8") as handle:
            process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=handle,
                                       stderr=subprocess.STDOUT, pass_fds=(args.lock_fd,))
            stages[name]["pid"] = process.pid
            save(output / "stages.json", stages)
            while True:
                snapshot = compact_status(output)
                record_event(output, "status_history.jsonl", snapshot)
                save(output / "status.latest.json", snapshot)
                if snapshot["attempt_errors"] > last_errors:
                    notify(output, "WARN", "发现新的失败调用尝试（可能仍在重试）：" +
                           json.dumps(snapshot.get("last_request_error", {}), ensure_ascii=False))
                    last_errors = snapshot["attempt_errors"]
                try:
                    process.wait(timeout=args.poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    continue
        stages[name].update(exit_code=process.returncode, status="finished",
                            finished_at=datetime.now(timezone.utc).isoformat())
        save(output / "stages.json", stages)
        print(f"[{name}] exit code: {process.returncode}", flush=True)
        if process.returncode != 0:
            notify(output, "WARN" if process.returncode == 2 else "ERROR",
                   f"{name} {'处理不完整或存在诊断' if process.returncode == 2 else '执行失败'}，退出码={process.returncode}；日志={log}",
                   stage=name, exit_code=process.returncode, log=str(log))
            explain_issues(output, name)
        return process.returncode

    code = run_stage("doctor", [sys.executable, "-u", "-m", "bid_outline", "doctor",
                               "--config", str(config_path), "--check-service"], project)
    if code != 0:
        return code
    if reader == "docx":
        code = run_stage("reading", [sys.executable, "-u", "-m", "outline_gen.cli.read_document",
        "--input", str(source), "--output-dir", str(output / "reading"),
        "--provider", "vllm", "--runtime", "api", "--base-url", base_url,
        "--model", model["id"], "--tokenizer", str(tokenizer.resolve()),
        "--thinking", args.thinking, "--heading-max-tokens", str(args.heading_max_tokens),
        "--workers", str(args.max_in_flight), "--heading-workers", str(args.max_in_flight),
        "--max-tokens", str(generation), "--request-max-tokens", str(reading_input),
        "--table-chunk-tokens", str(min(args.table_chunk_tokens, reading_input // 2)),
        "--toc-max-tokens", str(min(11000, reading_input - 1024))], project / "document-reading")
        if code not in (0, 2):
            return code
        reading_source = output / "reading"
    return run_stage("generation", [sys.executable, "-u", "-m", "bid_outline", "run",
        "--reader", "reading", "--input", str(reading_source),
        "--config", str(config_path), "--run", str(output / "generation")], project)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_status(output: Path) -> dict:
    """Small, durable progress samples without copying prompts or credentials."""
    execution = read_json(output / "execution.json", {"status": "starting"})
    stages = read_json(output / "stages.json", {})
    reading = read_json(output / "reading/manifest.json", {})
    reading_validation = read_json(output / "reading/validation.json", {})
    generation = read_json(output / "generation/manifest.json", {})
    runtime = read_json(output / "generation/runtime.json", {})
    sample = {
        "execution_status": execution.get("status"), "pid": execution.get("pid"),
        "exit_code": execution.get("exit_code"), "error": execution.get("error"),
        "stage": next((name for name, data in stages.items() if data.get("status") == "running"),
                      generation.get("stage") or reading.get("stage") or "preflight"),
        "reading_status": reading_validation.get("status", reading.get("status")),
        "reading_issue_count": len(reading_validation.get("issues", [])),
        "generation_status": generation.get("status"),
        "unprocessed_count": (len(generation["unprocessed_ranges"])
                              if "unprocessed_ranges" in generation else None),
        "in_flight": runtime.get("in_flight", 0), "calls": runtime.get("calls", 0),
        "task_counts": {}, "attempt_errors": 0,
    }
    reading_calls = output / "reading/model_calls"
    inputs = list(reading_calls.glob("*/*/*.input.txt"))
    sample["reading_requests"] = len(inputs)
    reading_errors = []
    for path in reading_calls.glob("*/*/*.status.json"):
        try:
            event = read_json(path, {})
        except json.JSONDecodeError:
            continue
        if event.get("error"):
            reading_errors.append((path.stat().st_mtime_ns, path, event))
    if reading_errors:
        _, path, event = max(reading_errors, key=lambda value: value[0])
        sample["attempt_errors"] = len(reading_errors)
        sample["last_request_error"] = {"stage": "reading", "error": event["error"], "record": str(path)}
    database = output / "generation/run.sqlite3"
    if database.exists():
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                for (raw,) in connection.execute("SELECT data FROM tasks"):
                    task = json.loads(raw)
                    key = f"{task['task_type']}:{task['status']}"
                    sample["task_counts"][key] = sample["task_counts"].get(key, 0) + 1
                for (raw,) in connection.execute("SELECT data FROM attempts ORDER BY rowid"):
                    attempt = json.loads(raw)
                    if attempt.get("error"):
                        sample["attempt_errors"] += 1
                        sample["last_request_error"] = {
                            "task_type": attempt.get("task_type"), "call_id": attempt.get("call_id"),
                            "error": attempt["error"], "error_kind": attempt.get("error_kind")}
            finally:
                connection.close()
        except sqlite3.OperationalError as exc:
            sample["database_note"] = str(exc)
    return sample


def explain_issues(output: Path, stage: str) -> None:
    path = output / ("reading/validation.json" if stage == "reading" else "generation/manifest.json")
    data = read_json(path, {})
    issues = data.get("issues", []) if stage == "reading" else data.get("unprocessed_ranges", [])
    counts = {}
    for issue in issues:
        label = issue.get("code") or issue.get("error") or issue.get("task_type") or "待核查"
        counts[label] = counts.get(label, 0) + 1
    if counts:
        notify(output, "WARN", f"{stage}诊断摘要：" + json.dumps(counts, ensure_ascii=False),
               diagnostics=str(path))


def print_progress(snapshot: dict) -> None:
    time_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    unprocessed = snapshot['unprocessed_count'] if snapshot['unprocessed_count'] is not None else '待汇总'
    print(f"[{time_text}] {snapshot['execution_status']} | 阶段={snapshot['stage']} | "
          f"阅读请求={snapshot.get('reading_requests', 0)} 生成调用={snapshot['calls']} 在途={snapshot['in_flight']} "
          f"失败尝试={snapshot['attempt_errors']} 未处理={unprocessed} "
          f"阅读诊断={snapshot['reading_issue_count']}", flush=True)


def watch(output: Path, interval: float) -> int:
    """Ctrl+C detaches the viewer; the worker owns and records execution."""
    if read_json(output / 'manifest.json', {}).get('kind') == 'batch':
        return watch_batch(output, interval)
    alerted = set()
    try:
        while True:
            snapshot = compact_status(output)
            observed = status(output)
            snapshot["execution_status"] = observed["execution"]["status"]
            print_progress(snapshot)
            request_error = snapshot.get("last_request_error")
            if request_error:
                marker = json.dumps(request_error, ensure_ascii=False, sort_keys=True)
                if marker not in alerted:
                    qualifier = '可能仍在重试' if snapshot['execution_status'] in ('running', 'queued') else '历史记录'
                    print(f"[WARN] 最近失败调用（{qualifier}）：{marker}", file=sys.stderr, flush=True)
                    alerted.add(marker)
            alert_path = output / ('events.jsonl' if layout_v2(output) else 'alerts.jsonl')
            for line in alert_path.read_text(encoding="utf-8").splitlines() if alert_path.exists() else []:
                if line not in alerted:
                    try:
                        alert = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # An append may be in progress; read it next poll.
                    if 'level' not in alert:
                        continue
                    print(f"[{alert['level']}] {alert['message']}", file=sys.stderr, flush=True)
                    if sys.stderr.isatty() and alert["level"] == "ERROR":
                        print("\a", end="", file=sys.stderr, flush=True)
                    alerted.add(line)
            state = snapshot["execution_status"]
            if state not in ("starting", "queued", "running"):
                if state in ("failed", "incomplete", "interrupted", "cancelled"):
                    evidence = alert_path if alert_path.exists() else output / "logs"
                    print(f"[ERROR] 测试未完整成功：{state}。详情：{evidence}", file=sys.stderr)
                elif state == "completed_with_issues":
                    print(f"[WARN] 测试完成但有待核事项：{output / ('results/unresolved.md' if layout_v2(output) else 'generation/exports/unresolved.md')}")
                else:
                    print(f"[DONE] 结果目录：{output / ('results' if layout_v2(output) else 'generation/exports')}")
                return 2 if state in ("incomplete", "completed_with_issues") else (0 if state == "completed" else 1)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止查看；后台任务继续运行。")
        return 0


def worker(output: Path, attempt: str, inherited_lock: int | None = None) -> int:
    """Only the lock owner writes overall state; subprocess stdout stays in stage logs."""
    if read_json(output / 'manifest.json', {}).get('kind') == 'batch':
        return batch_worker(output, attempt, inherited_lock)
    with (os.fdopen(inherited_lock, "a") if inherited_lock is not None else
          (output / "execution.lock").open("a")) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("该测试目录已有执行进程，拒绝重复启动", flush=True)
            return 1
        state = {"status": "running", "pid": os.getpid(), "host": socket.gethostname(),
                 "attempt": attempt, "started_at": now(), "exit_code": None}
        save(output / "execution.json", state)
        try:
            args = argparse.Namespace(**read_json(output / "request.json"))
            args.input = Path(args.input)
            args.output = output
            args.tokenizer = Path(args.tokenizer) if getattr(args, "tokenizer", None) else None
            args.attempt = attempt
            args.lock_fd = lock.fileno()
            code = run_test(args)
            state.update(status={0: "completed", 2: "completed_with_issues"}.get(code, "failed"), exit_code=code)
            if code == 2 and read_json(output / "generation/manifest.json", {}).get("status") == "incomplete":
                state["status"] = "incomplete"
        except BaseException as exc:
            traceback.print_exc()
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            state.update(status="cancelled" if code == 130 else "failed", exit_code=code,
                         error=f"{type(exc).__name__}: {exc}")
            notify(output, "ERROR", state["error"], attempt=attempt)
        finally:
            state["finished_at"] = now()
            save(output / "execution.json", state)
            save(output / f"execution.{attempt}.json", state)
            snapshot = compact_status(output)
            record_event(output, "status_history.jsonl", snapshot)
            save(output / "status.latest.json", snapshot)
        return code


def launch(output: Path) -> None:
    attempt = timestamp()
    log = output / "logs" / f"launcher.{attempt}.log"
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "_worker", "--run", str(output), "--attempt", attempt]
    with (output / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("测试仍在执行，拒绝重复启动") from exc
        save(output / "execution.json", {"status": "queued", "attempt": attempt,
                                          "queued_at": now(), "host": socket.gethostname()})
        with log.open("x", encoding="utf-8") as handle:
            command += ["--lock-fd", str(lock.fileno())]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle,
                                       stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
    save(output / f"launch.{attempt}.json", {"pid": process.pid, "host": socket.gethostname(),
                                           "command": command, "log": str(log), "launched_at": now()})
    print(f"PID={process.pid}\n测试目录={output}\n启动日志={log}", flush=True)


def status(output: Path) -> dict:
    state = read_json(output / "execution.json", {"status": "starting"})
    # A stored PID is not proof of liveness (or even the same host). The exclusive
    # lock covers the controller and is also inherited by its active stage child.
    with (output / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
    if state["status"] == "running" and not active:
        state = {**state, "status": "interrupted", "note": "执行锁已释放，但没有正常结束记录"}
    generation = read_json(output / "generation/manifest.json", {})
    if not active and state["status"] == "completed_with_issues" and generation.get("status") == "incomplete":
        state = {**state, "status": "incomplete"}
    manifest = read_json(output / 'manifest.json', {})
    if manifest.get('kind') == 'batch':
        items = []
        for item in manifest.get('items', []):
            child = output / item['directory']
            observed = status(child) if child.exists() else None
            items.append({**item, 'execution': observed['execution'] if observed else None,
                          'active': observed['active'] if observed else False})
        return {'run': str(output), 'kind': 'batch', 'execution': state, 'active': active,
                'batch_size': manifest['request']['batch_size'],
                'concurrency_per_file': manifest['request']['max_in_flight'], 'items': items}
    return {"run": str(output), "execution": state, "active": active,
            "stages": read_json(output / "stages.json", {}),
            "model_requests": read_json(output / "generation/runtime.json", {}),
            "reading": read_json(output / "reading/validation.json", {}),
            "generation": generation}


TERMINAL = {'completed', 'completed_with_issues', 'incomplete', 'failed', 'cancelled', 'interrupted'}


def batch_outcome(items: list[dict]) -> tuple[str, int]:
    states = {item['status'] for item in items}
    if states & {'failed', 'cancelled', 'interrupted'}:
        return 'failed', 1
    if 'incomplete' in states:
        return 'incomplete', 2
    if 'completed_with_issues' in states:
        return 'completed_with_issues', 2
    return 'completed', 0


def batch_worker(output: Path, attempt: str, inherited_lock: int | None = None) -> int:
    """Bound file lifetimes, including both reading and generation, independently."""
    with (os.fdopen(inherited_lock, 'a') if inherited_lock is not None else
          (output / 'execution.lock').open('a')) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        manifest = read_json(output / 'manifest.json')
        request = manifest['request']
        items = manifest['items']
        execution = {'status': 'running', 'attempt': attempt, 'pid': os.getpid(),
                     'host': socket.gethostname(), 'started_at': now(), 'exit_code': None}

        def persist():
            manifest['execution'] = execution
            manifest['items'] = items
            save(output / 'manifest.json', manifest)
            overview(output, manifest)

        persist()
        record_event(output, 'batch_started.jsonl', {'attempt': attempt})
        try:
            while True:
                # Reattach live children after controller interruption; never
                # launch a duplicate just because the parent was restarted.
                active = 0
                for item in items:
                    child = output / item['directory']
                    if not child.exists():
                        continue
                    observed = status(child)
                    state = observed['execution']['status']
                    if state == 'starting' and not observed['active'] and item['status'] == 'pending':
                        continue  # Child directory created, but launch not yet attempted.
                    if observed['active'] or state in ('queued', 'starting'):
                        item['status'] = 'running'
                        active += 1
                        continue
                    final_state = state if state in TERMINAL else 'interrupted'
                    if item['status'] == final_state:
                        continue
                    item['status'] = final_state
                    item['exit_code'] = observed['execution'].get('exit_code')
                    item['finished_at'] = now()
                    record_event(output, 'file_finished.jsonl', dict(item))
                    if item['status'] != 'completed':
                        notify(output, 'ERROR' if item['status'] in ('failed', 'interrupted', 'cancelled') else 'WARN',
                               f"{Path(item['input']).name}: {item['status']}；详情 {child / 'overview.md'}")
                for item in items:
                    if active >= request['batch_size']:
                        break
                    if item['status'] != 'pending':
                        continue
                    child = output / item['directory']
                    try:
                        if not child.exists():
                            child.mkdir(parents=True)
                            (child / 'logs').mkdir()
                            child_request = {k: v for k, v in request.items()
                                             if k not in ('input_dir', 'batch_size', 'input')}
                            child_request.update(input=item['input'], reader='docx')
                            save(child / 'manifest.json', {'layout_version': 2, 'created_at': now(),
                                                          'request': child_request})
                        launch(child)
                        item.update(status='running', started_at=now())
                        active += 1
                        record_event(output, 'file_started.jsonl', dict(item))
                    except Exception as exc:
                        item.update(status='failed', error=f'{type(exc).__name__}: {exc}', exit_code=1)
                        notify(output, 'ERROR', f"无法启动 {item['input']}: {exc}")
                persist()
                if all(item['status'] in TERMINAL for item in items):
                    execution['status'], code = batch_outcome(items)
                    execution.update(exit_code=code, finished_at=now())
                    persist()
                    record_event(output, 'batch_finished.jsonl', execution)
                    return code
                time.sleep(request['poll_interval'])
        except BaseException as exc:
            # Children retain their own locks/status and keep running. Resume
            # reattaches them rather than exceeding the configured file limit.
            execution.update(status='interrupted', error=f'{type(exc).__name__}: {exc}', finished_at=now(), exit_code=1)
            persist()
            record_event(output, 'batch_interrupted.jsonl', execution)
            raise


def watch_batch(output: Path, interval: float) -> int:
    seen = {}
    try:
        while True:
            observed = status(output)
            counts = {}
            for item in observed['items']:
                state = (item.get('execution') or {}).get('status', item['status'])
                counts[state] = counts.get(state, 0) + 1
                if seen.get(item['input']) != state:
                    print(f"[{state}] {Path(item['input']).name} → {output / item['directory']}", flush=True)
                    seen[item['input']] = state
            print(f"[{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')}] batch={observed['execution']['status']} {counts}", flush=True)
            if observed['execution']['status'] in TERMINAL:
                print(f"批次结果：{output / 'overview.md'}")
                return observed['execution'].get('exit_code') or (1 if observed['execution']['status'] == 'interrupted' else 0)
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\n已停止查看；批量调度和子任务继续运行。')
        return 0


def main(argv=None) -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="GPU真实测试：启动、状态、日志和断点续跑")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="后台启动；自动创建输出目录及日志")
    source = start.add_mutually_exclusive_group()
    source.add_argument("--input", "--file", type=Path, default=project / "test_inputs/additional/01_general/安防服务项目采购文件(最终版).docx")
    source.add_argument('--input-dir', type=Path, help='批量处理该目录直接包含的DOCX，不递归')
    start.add_argument('--batch-size', type=int, default=2, help='批量模式同时运行的文件数，默认2')
    start.add_argument("--reader", choices=["auto", "docx", "reading"], default="auto")
    start.add_argument("--name", help="运行名，默认使用输入文件名")
    start.add_argument("--runs-dir", type=Path, default=project / "runs")
    start.add_argument("--output", type=Path)
    start.add_argument("--base-url", default=DEFAULTS["base_url"])
    start.add_argument("--model")
    start.add_argument("--tokenizer", type=Path, default=os.environ.get("BID_OUTLINE_TOKENIZER"))
    start.add_argument("--context-tokens", type=int)
    start.add_argument("--context-budget", type=int, default=DEFAULTS["context_budget"])
    start.add_argument("--max-tokens", type=int, default=DEFAULTS["max_tokens"])
    start.add_argument("--heading-max-tokens", type=int, default=DEFAULTS["heading_max_tokens"])
    start.add_argument("--thinking", choices=["auto", "on", "off"], default=DEFAULTS["thinking"])
    start.add_argument("--max-in-flight", "--concurrency", type=int, default=DEFAULTS["max_in_flight"])
    start.add_argument("--timeout", type=float, default=DEFAULTS["timeout"])
    start.add_argument("--chunk-chars", type=int, default=DEFAULTS["chunk_chars"])
    start.add_argument("--reading-input-tokens", type=int, default=DEFAULTS["reading_input_tokens"])
    start.add_argument("--table-chunk-tokens", type=int, default=DEFAULTS["table_chunk_tokens"])
    start.add_argument("--poll-interval", type=float, default=DEFAULTS["poll_interval"])
    start.add_argument("--detach", action="store_true", help="后台启动后立即返回；默认自动轮询至完成")
    start.add_argument("--reuse-calls-from", action="append", default=[],
                       help="旧generation目录；仅复用完全相同请求的成功响应，可重复指定")
    for name in ("status", "logs", "resume", "_worker"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=name == "_worker", help="不指定时使用默认runs目录中最近启动的测试")
        if name != "_worker":
            command.add_argument("--runs-dir", type=Path, default=project / "runs")
        if name in ("status", "resume"):
            command.add_argument("--poll-interval", type=float, default=DEFAULTS["poll_interval"])
        if name == "status":
            command.add_argument("--once", action="store_true", help="只输出一次完整JSON；默认持续轮询")
        if name == "resume":
            command.add_argument("--detach", action="store_true")
        if name == "logs":
            command.add_argument("--stage", choices=("launcher", "doctor", "reading", "generation"), default="launcher")
            command.add_argument("--lines", type=int, default=80)
            command.add_argument("--follow", action="store_true")
        if name == "_worker":
            command.add_argument("--attempt", required=True)
            command.add_argument("--lock-fd", type=int)
    args = parser.parse_args((sys.argv[1:] or ["start"]) if argv is None else argv)
    for field in ("poll_interval", "timeout", "max_tokens", "heading_max_tokens", "context_budget",
                  "context_tokens", "chunk_chars", "reading_input_tokens", "table_chunk_tokens", "max_in_flight", "batch_size"):
        value = getattr(args, field, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{field}必须为有限正数")
    if args.command == "start":
        batch_files = None
        if args.input_dir:
            if not args.input_dir.is_dir():
                parser.error('--input-dir必须是存在的目录')
            if args.reader == 'reading':
                parser.error('批量目录当前只支持DOCX输入，不支持reading产物目录集合')
            batch_files = sorted((path.resolve() for path in args.input_dir.iterdir()
                                  if path.is_file() and path.suffix.lower() == '.docx'
                                  and not path.name.startswith('~$')), key=lambda p: p.name)
            if not batch_files:
                parser.error('指定目录直接包含的DOCX文件为空；不会递归查找子目录')
            args.input = args.input_dir
        if not args.input.exists():
            parser.error("输入文件或阅读产物目录不存在")
        if batch_files is None and args.reader == "docx" and (not args.input.is_file() or args.input.suffix.lower() != ".docx"):
            parser.error("docx模式需要DOCX文件")
        if args.max_in_flight < 1:
            parser.error("并发数必须为正数")
        if args.output:
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=False)
        else:
            output = create_run_directory(args.runs_dir, args.name or args.input.stem)
        (output / "logs").mkdir()
        save(output / 'manifest.json', {'layout_version': 2, 'created_at': now()})
        request = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()
                   if k not in {"command", "output", "name", "runs_dir", "detach"}}
        save(output / "request.json", request)
        if batch_files is not None:
            manifest = read_json(output / 'manifest.json')
            manifest.update(kind='batch', items=[
                {'input': str(path), 'directory': f'files/{index:03d}_{safe_name(path.stem)}', 'status': 'pending'}
                for index, path in enumerate(batch_files, 1)])
            save(output / 'manifest.json', manifest)
            overview(output, manifest)
            print(f'文件数={len(batch_files)}；同时文件数={args.batch_size}；每文件请求并发={args.max_in_flight}；'
                  f'本批次客户端在途请求上限约={args.batch_size * args.max_in_flight}', flush=True)
        launch(output)
        args.runs_dir.mkdir(parents=True, exist_ok=True)
        save(args.runs_dir / "latest.json", {"run": str(output), "started_at": now()})
        return 0 if args.detach else watch(output, args.poll_interval)
    latest = read_json(getattr(args, "runs_dir", project / "runs") / "latest.json", {})
    if args.run is None and not latest.get("run"):
        parser.error("尚无最近运行记录，请指定--run")
    output = (args.run or Path(latest["run"])).resolve()
    if not output.is_dir():
        parser.error("测试目录不存在")
    if args.command == "_worker":
        return worker(output, args.attempt, args.lock_fd)
    if args.command == "status":
        if args.once:
            print(json.dumps(status(output), ensure_ascii=False, indent=2))
        else:
            return watch(output, args.poll_interval)
    elif args.command == "resume":
        if not read_json(output / "request.json"):
            parser.error("旧版测试目录没有request.json，请使用start创建新测试")
        if status(output)["active"]:
            parser.error("测试仍在运行，不能重复启动")
        launch(output)
        return 0 if args.detach else watch(output, args.poll_interval)
    elif args.command == "logs":
        logs = sorted((output / "logs").glob(f"{args.stage}*.log"), key=lambda p: p.stat().st_mtime_ns)
        if not logs:
            parser.error("该阶段尚未产生日志，请先查看status或launcher日志")
        command = ["tail", "-n", str(args.lines)] + (["-f"] if args.follow else []) + [str(logs[-1])]
        return subprocess.call(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
