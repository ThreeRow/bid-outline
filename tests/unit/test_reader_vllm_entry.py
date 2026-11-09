"""Local vLLM must not fail SDK construction merely because auth is disabled."""

from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import tempfile

from bid_outline.cli import invoke_reader
from bid_outline.config import AppConfig


PROJECT = Path(__file__).resolve().parents[2]


def test_docx_bridge_selects_vllm_and_preserves_configured_key(monkeypatch):
    directory = Path(tempfile.mkdtemp(prefix="bid-reader-entry-"))
    args = Namespace(reading_module=PROJECT / "document-reading", run=directory,
                     input=directory / "input.docx")
    monkeypatch.setenv("BID_OUTLINE_API_KEY", "test-only-secret")
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert invoke_reader(args, AppConfig(model="Qwen3.5-27B")) == directory / "reading"
    command = captured["command"]
    assert command[command.index("--provider") + 1] == "vllm"
    assert captured["env"]["OUTLINE_GEN_API_KEY"] == "test-only-secret"
    assert "test-only-secret" not in command


def test_actual_reading_sdk_construction_without_credentials():
    # An isolated subprocess imports the migrated package and installed SDK, but
    # only constructs/closes clients: no server requests or DOCX conversion.
    code = '''
import os
for name in ("OUTLINE_GEN_API_KEY", "SILICONFLOW_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
    os.environ.pop(name, None)
from outline_gen.config import resolve_provider
from outline_gen.runtime.openai_api import make_client
settings = resolve_provider("vllm", model="Qwen3.5-27B", base_url="http://127.0.0.1:8000/v1")
assert settings["api_key"] == "EMPTY"
client = make_client(api_key=settings["api_key"], base_url=settings["base_url"])
client.close()
explicit = resolve_provider("vllm", api_key="test-configured-key")
assert explicit["api_key"] == "test-configured-key"
print("LOCAL_VLLM_SDK_OK")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT / "document-reading",
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert "LOCAL_VLLM_SDK_OK" in result.stdout


def test_reading_thinking_cli_to_actual_sdk_wire_body():
    code = '''
import json
import httpx
from openai import OpenAI
from outline_gen.runtime.openai_api import chat
from outline_gen.cli import read_document as cli
seen = {}
def runtime(config):
    seen['runtime'] = config
    return object()
def read(*args, **kwargs):
    seen['reading'] = kwargs['config']
    return {'status': 'completed'}
cli.create_runtime = runtime
cli.read_document = read
assert cli.main(['--input','unused.docx','--output-dir','unused-output',
                 '--provider','vllm','--model','test','--thinking','off',
                 '--heading-max-tokens','2048','--max-tokens','8192']) == 0
expected = {'chat_template_kwargs': {'enable_thinking': False}}
assert seen['runtime'].extra_body == expected
assert seen['reading'].extra_body == expected
assert seen['reading'].heading_max_tokens == 2048
def handle(request):
    body = json.loads(request.content)
    assert body['chat_template_kwargs'] == expected['chat_template_kwargs']
    assert 'extra_body' not in body
    return httpx.Response(200, json={'id':'test','object':'chat.completion','created':0,
        'model':'test','choices':[{'index':0,'finish_reason':'stop',
        'message':{'role':'assistant','content':'P0001'}}]})
client = OpenAI(api_key='EMPTY', base_url='http://127.0.0.1:8000/v1',
                http_client=httpx.Client(transport=httpx.MockTransport(handle)))
assert chat(client, model='test', user='选择标题', extra_body=seen['runtime'].extra_body) == 'P0001'
client.close()
print('THINKING_FORWARDING_OK')
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT / "document-reading",
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert "THINKING_FORWARDING_OK" in result.stdout
