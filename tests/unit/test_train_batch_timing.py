import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('train_batch_script',ROOT/'scripts/run_train_batch.py')
batch=importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


@pytest.mark.parametrize('staged', [False, True])
def test_batch_four_files_stage_timing_failure_isolation_and_thinking(monkeypatch, staged):
    async def run():
        folder=Path(tempfile.mkdtemp(prefix='bid-train-batch-test-'))
        incoming=folder/'inputs';incoming.mkdir()
        for name in ['a.docx','b.docx','c.docx','d.DOCX','~$lock.docx','ignore.txt']:
            (incoming/name).write_text('fixture')
        tokenizer=folder/'tokenizer';tokenizer.mkdir()
        args=SimpleNamespace(input_dir=incoming,output=folder/'batch',tokenizer=tokenizer,
          config=ROOT/('configs/train.staged_markdown.example.json' if staged else 'configs/train.thinking.example.json'),
          parallel_files=4,concurrency=8,
          thinking='config' if staged else 'on', reader_thinking='off' if staged else None,
          max_tokens=8192,heading_max_tokens=4096,
          reader_base_url='http://fake/v1',reader_model='fake')
        active=0;peak=0;captured=[]
        class Process:
            returncode=None
            async def wait(self):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.01)
                active-=1
                self.returncode=self.code
                return self.code
        async def launch(*command,**kwargs):
            captured.append((command,kwargs));proc=Process()
            if 'outline_gen.cli.read_document' in command:
                path=Path(command[command.index('--output-dir')+1]);path.mkdir()
                (path/'document.json').write_text('{}')
                proc.code=1 if Path(command[command.index('--input')+1]).name=='a.docx' else 2
                assert command[command.index('--thinking')+1]==('off' if staged else 'on')
                assert command[command.index('--workers')+1]=='8'
                assert command[command.index('--request-max-tokens')+1]=='11264'
            else:
                proc.code=0
            return proc
        monkeypatch.setattr(batch.asyncio,'create_subprocess_exec',launch)
        result=await batch.run(args)
        state=json.loads((args.output/'timing.json').read_text())
        assert result==1 and len(state['items'])==4 and peak==4
        assert sum('generation' in i['stages'] for i in state['items'])==3
        assert state['elapsed_seconds']>0 and state['finished_at']
        for item in state['items']:
            assert item['elapsed_seconds']>0
            assert item['stages']['reading']['elapsed_seconds']>0
        config=json.loads((args.output/'generation.config.json').read_text())
        assert config['provider']=='train' and config['train_enable_thinking'] is (not staged)
        if staged:
            assert config['thinking_default'] is False
            assert config['thinking_by_task']['P08'] is True
            assert config['directory_protocol']=='markdown-v1'
            assert state['thinking_by_task']['P03'] is False
            assert state['thinking_by_task']['P04-A'] is False
            assert state['thinking_by_task']['P11'] is True
        assert config['max_in_flight']==8 and config['workers']>=8
        assert config['tokenizer']==str(tokenizer.resolve())
        assert config['service_context_tokens']==config['context_tokens']==20480
        assert (args.output/'timing.md').exists()
        assert all('PYTHONPATH' in kwargs['env'] for _,kwargs in captured)
    asyncio.run(run())
