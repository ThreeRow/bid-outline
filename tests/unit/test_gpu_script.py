"""Controller persistence and stage recovery without contacting any model service."""

import argparse
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def script():
    path = Path(__file__).resolve().parents[2] / "scripts/run_gpu_test.py"
    spec = importlib.util.spec_from_file_location("gpu_test_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def directory():
    root = Path(tempfile.mkdtemp(prefix="bid-gpu-controller-"))
    (root / "logs").mkdir()
    return root


def test_worker_records_preflight_failure_and_history(monkeypatch):
    module = script()
    root = directory()
    module.save(root / "request.json", {"input": "/unused.docx", "tokenizer": None})

    def fail(args):
        raise ValueError("service configuration missing")

    monkeypatch.setattr(module, "run_test", fail)
    assert module.worker(root, "first") == 1
    state = module.read_json(root / "execution.json")
    assert state["status"] == "failed"
    assert state["exit_code"] == 1
    assert "configuration missing" in state["error"]
    monkeypatch.setattr(module, "run_test", lambda args: 0)
    assert module.worker(root, "second") == 0
    assert module.read_json(root / "execution.first.json")["status"] == "failed"
    assert module.status(root)["execution"]["status"] == "completed"


def test_duplicate_worker_and_interrupted_status():
    module = script()
    root = directory()
    module.save(root / "execution.json", {"status": "running", "pid": 123})
    with (root / "execution.lock").open("a") as lock:
        module.fcntl.flock(lock, module.fcntl.LOCK_EX)
        assert module.status(root)["active"] is True
        assert module.worker(root, "duplicate") == 1
        assert module.read_json(root / "execution.json")["pid"] == 123
    assert module.status(root)["execution"]["status"] == "interrupted"


@pytest.mark.parametrize('new_layout', [False, True])
def test_reading_failure_resume_preserves_log_and_runs_generation(monkeypatch, new_layout):
    module = script()
    root = directory()
    if new_layout:
        module.save(root / 'manifest.json', {'layout_version': 2})
    source = root / "source.docx"
    source.write_bytes(b"test fixture: subprocess is mocked")
    tokenizer = root / "tokenizer"
    tokenizer.mkdir()
    args = argparse.Namespace(input=source, output=root, base_url="http://127.0.0.1:8000/v1",
                              model=None, tokenizer=tokenizer, context_tokens=None,
                              max_in_flight=4, attempt="first", lock_fd=0)

    class Response:
        text = json.dumps({"data": [{"id": "test-model", "max_model_len": 32768}]})

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self.text)

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            return Response()

    calls = []

    class Process:
        pid = 456

        def __init__(self, command, **kwargs):
            calls.append(command)
            self.returncode = 0
            kwargs["stdout"].write("mock stage log\n")
            if "outline_gen.cli.read_document" in command:
                assert command[command.index("--provider") + 1] == "vllm"
                (root / "reading").mkdir(exist_ok=True)
                module.save(root / "reading/manifest.json", {"status": "failed"})
                self.returncode = 1 if "--resume" not in command else 2

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "test-version")
    assert module.run_test(args) == 1
    assert not any("run" in c for c in calls)
    old_log = (root / "logs/reading.first.log").read_text()
    args.attempt = "second"
    assert module.run_test(args) == 0
    assert "--resume" in [c for c in calls if "outline_gen.cli.read_document" in c][-1]
    assert (root / "logs/reading.first.log").read_text() == old_log
    stages = module.read_json(root / "stages.json")
    assert stages["reading"]["history"][0]["exit_code"] == 1
    assert stages["reading"]["exit_code"] == 2
    assert stages["generation"]["exit_code"] == 0
    if new_layout:
        manifest = module.read_json(root / 'manifest.json')
        assert manifest['config']['model'] == 'test-model'
        assert 'environment' in manifest and 'stages' in manifest
        assert not (root / 'config.json').exists()
        assert not (root / 'stages.json').exists()
        generation = next(command for command in calls if 'bid_outline' in command and 'run' in command)
        assert generation[generation.index('--output') + 1] == str(root / 'results')
        assert generation[generation.index('--config') + 1] == str(root / 'manifest.json')


def test_named_run_and_latest_status_without_network(monkeypatch, capsys):
    module = script()
    root = directory()
    source = root / "测试 文件.docx"
    source.write_bytes(b"fixture")
    # Redirect the project-level latest pointer into this isolated test project.
    fake_script = root / "scripts/run_gpu_test.py"
    fake_script.parent.mkdir()
    monkeypatch.setattr(module, "__file__", str(fake_script))
    launched = []
    monkeypatch.setattr(module, "launch", lambda path: launched.append(path))
    assert module.main(["start", "--file", str(source), "--name", "安防测试", "--detach"]) == 0
    output = launched[0]
    assert re.fullmatch(r"安防测试_\d{8}_\d{6}_\d{6}", output.name)
    request = module.read_json(output / "request.json")
    assert request["thinking"] == "off"
    assert request["max_tokens"] == 8192
    assert module.read_json(root / "runs/latest.json")["run"] == str(output)
    assert module.main(["status", "--once"]) == 0
    assert str(output) in capsys.readouterr().out


def test_watch_reminds_incomplete_and_returns_code_two(capsys):
    module = script()
    root = directory()
    module.save(root / "execution.json", {"status": "incomplete", "exit_code": 2})
    module.notify(root, "WARN", "输出token耗尽，正文为空")
    assert module.watch(root, 0.01) == 2
    output = capsys.readouterr()
    assert "输出token耗尽" in output.err
    assert "未完整成功" in output.err


def test_timestamp_collision_uses_counter_not_random_suffix(monkeypatch):
    module = script()
    root = directory()
    monkeypatch.setattr(module, "timestamp", lambda: "20261103_120000_123456")
    first = module.create_run_directory(root, "测试/方案")
    second = module.create_run_directory(root, "测试/方案")
    assert first.name == "测试_方案_20261103_120000_123456"
    assert second.name == first.name + "_2"


def test_unprocessed_count_is_unknown_before_final_aggregation():
    module = script()
    root = directory()
    (root / 'generation').mkdir()
    module.save(root / 'generation/manifest.json', {'status': 'running'})
    assert module.compact_status(root)['unprocessed_count'] is None
    module.save(root / 'generation/manifest.json', {'status': 'incomplete', 'unprocessed_ranges': [{}] * 9})
    assert module.compact_status(root)['unprocessed_count'] == 9


def test_real_background_worker_failure_is_recorded_without_gpu():
    module = script()
    root = directory()
    source = root / "input.docx"
    source.write_bytes(b"not parsed: service preflight intentionally fails")
    target = root / "background"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([sys.executable, "-B", module.__file__, "start", "--file", str(source),
                                 "--output", str(target), "--runs-dir", str(root / "runs"),
                                 "--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                                 "--detach"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = module.read_json(target / "execution.json", {})
            if state.get("status") == "failed" and (target / "events.jsonl").is_file():
                break
            time.sleep(0.05)
        assert state["status"] == "failed", state
        assert "Specify --model" in state["error"]
        assert "ERROR" in (target / "events.jsonl").read_text()
        assert (target / "overview.md").is_file()
        assert not (target / "request.json").exists()
        assert len(list((target / "logs").glob("launcher.*.log"))) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_thinking_and_budgets_are_forwarded_to_both_stages(monkeypatch):
    module = script()
    root = directory()
    source = root / "source.docx"
    source.write_bytes(b"fixture")
    tokenizer = root / "tokenizer"
    tokenizer.mkdir()
    args = argparse.Namespace(input=source, output=root, tokenizer=tokenizer, attempt="fixed", lock_fd=0,
                              thinking="off", max_tokens=8192, heading_max_tokens=2048)

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            class Response:
                text = '{}'
                def raise_for_status(self): pass
                def json(self): return {"data": [{"id": "test", "max_model_len": 32768}]}
            return Response()

    commands = []
    class Process:
        pid = 1
        returncode = 0
        def __init__(self, command, **kwargs): commands.append(command)
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "test")
    assert module.run_test(args) == 0
    config = module.read_json(root / "config.json")
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    read_cmd = next(c for c in commands if "outline_gen.cli.read_document" in c)
    assert read_cmd[read_cmd.index("--thinking") + 1] == "off"
    assert read_cmd[read_cmd.index("--max-tokens") + 1] == "8192"
    assert int(read_cmd[read_cmd.index("--request-max-tokens") + 1]) + 8192 + 1024 <= 20000
