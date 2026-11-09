"""File-level scheduling tested without GPU or model calls."""
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def script():
    path = Path(__file__).resolve().parents[2] / 'scripts/run_gpu_test.py'
    spec = importlib.util.spec_from_file_location('batch_launcher', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_directory_input_snapshots_direct_docx_only(monkeypatch):
    module = script()
    root = Path(tempfile.mkdtemp(prefix='bid-batch-files-'))
    source = root / 'inputs'
    source.mkdir()
    for name in ('甲.docx', '乙.DOCX', 'notes.txt', '~$锁文件.docx'):
        (source / name).write_text('fixture')
    (source / 'nested').mkdir()
    (source / 'nested/不应读取.docx').write_text('fixture')
    launched = []
    monkeypatch.setattr(module, 'launch', lambda path: launched.append(path))
    assert module.main(['start', '--input-dir', str(source), '--batch-size', '2', '--concurrency', '3',
                        '--runs-dir', str(root / 'runs'), '--detach']) == 0
    manifest = module.read_json(launched[0] / 'manifest.json')
    assert manifest['kind'] == 'batch'
    assert {Path(item['input']).name for item in manifest['items']} == {'甲.docx', '乙.DOCX'}
    assert manifest['request']['batch_size'] == 2
    assert manifest['request']['max_in_flight'] == 3
    assert (launched[0] / 'overview.md').exists()


def test_batch_bounds_lifetimes_continues_failure_and_forwards_concurrency(monkeypatch):
    module = script()
    root = Path(tempfile.mkdtemp(prefix='bid-batch-limits-'))
    (root / 'logs').mkdir()
    items = [{'input': str(root / f'{i}.docx'), 'directory': f'files/{i}', 'status': 'pending'} for i in range(5)]
    module.save(root / 'manifest.json', {'layout_version': 2, 'kind': 'batch',
        'request': {'batch_size': 2, 'max_in_flight': 3, 'poll_interval': 0.001, 'model': 'test'}, 'items': items})
    running = {}
    finished = {}
    launched = []
    peak = 0

    def launch(child):
        nonlocal peak
        request = module.read_json(child / 'request.json')
        assert request['max_in_flight'] == 3 and request['reader'] == 'docx'
        running[str(child)] = 0
        launched.append(child)
        peak = max(peak, len(running))

    def status(child):
        key = str(child)
        if key in finished:
            return finished[key]
        running[key] += 1
        if running[key] == 1:
            return {'active': True, 'execution': {'status': 'running'}}
        running.pop(key)
        failed = child.name == '1'
        finished[key] = {'active': False, 'execution': {'status': 'failed' if failed else 'completed', 'exit_code': 1 if failed else 0}}
        return finished[key]

    monkeypatch.setattr(module, 'launch', launch)
    monkeypatch.setattr(module, 'status', status)
    assert module.batch_worker(root, 'test') == 1
    assert peak == 2 and len(launched) == 5
    final = module.read_json(root / 'manifest.json')
    assert [item['status'] for item in final['items']].count('completed') == 4
    assert final['execution']['status'] == 'failed'
    assert 'file_finished' in (root / 'events.jsonl').read_text()


def test_resume_reattaches_child_even_if_parent_crashed_before_recording_start(monkeypatch):
    module = script()
    root = Path(tempfile.mkdtemp(prefix='bid-batch-reattach-'))
    (root / 'logs').mkdir()
    child = root / 'files/one'
    child.mkdir(parents=True)
    module.save(root / 'manifest.json', {'layout_version': 2, 'kind': 'batch',
        'request': {'batch_size': 1, 'max_in_flight': 2, 'poll_interval': 0.001},
        'items': [{'input': 'one.docx', 'directory': 'files/one', 'status': 'pending'}]})
    responses = iter([{'active': True, 'execution': {'status': 'running'}},
                      {'active': False, 'execution': {'status': 'completed', 'exit_code': 0}}])
    monkeypatch.setattr(module, 'status', lambda path: next(responses))
    monkeypatch.setattr(module, 'launch', lambda path: pytest.fail('must not duplicate existing child'))
    assert module.batch_worker(root, 'resume') == 0


def test_empty_directory_and_invalid_parallelism_do_not_create_runs():
    module = script()
    root = Path(tempfile.mkdtemp(prefix='bid-batch-invalid-'))
    for extra in ([], ['--batch-size', '0'], ['--concurrency', '0']):
        with pytest.raises(SystemExit):
            module.main(['start', '--input-dir', str(root), '--runs-dir', str(root / 'runs'), '--detach'] + extra)
    assert not (root / 'runs').exists()


def test_real_batch_controller_runs_all_children_and_records_errors():
    module = script()
    root = Path(tempfile.mkdtemp(prefix='bid-batch-processes-'))
    inputs = root / 'inputs'
    inputs.mkdir()
    for name in ('first.docx', 'second.docx', 'third.docx'):
        (inputs / name).write_text('preflight fails before parsing')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = root / 'batch'
    try:
        result = subprocess.run([sys.executable, '-B', module.__file__, 'start', '--input-dir', str(inputs),
            '--output', str(output), '--runs-dir', str(root / 'runs'), '--batch-size', '2', '--concurrency', '3',
            '--poll-interval', '0.1', '--base-url', f'http://127.0.0.1:{server.server_port}/v1', '--detach'],
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            manifest = module.read_json(output / 'manifest.json')
            if manifest.get('execution', {}).get('status') == 'failed' and all(
                    item['status'] == 'failed' for item in manifest['items']):
                break
            time.sleep(0.1)
        assert len(manifest['items']) == 3
        assert all(item['status'] == 'failed' for item in manifest['items'])
        for item in manifest['items']:
            child = output / item['directory']
            assert module.read_json(child / 'request.json')['max_in_flight'] == 3
            assert 'Specify --model' in module.read_json(child / 'execution.json')['error']
            assert list((child / 'logs').glob('launcher.*.log'))
        assert 'third.docx' in (output / 'overview.md').read_text()
        assert module.status(output)['kind'] == 'batch'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
