"""Existing-reader batch scheduling and frozen generation-only invocation."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('reading_batch_script',ROOT/'scripts/run_reading_batch.py')
batch=importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def fixture(tmp_path, samples=2):
    incoming=tmp_path/'inputs'
    for n in range(1,samples+1):
        folder=incoming/f'S{n:02d}'/'reading'
        folder.mkdir(parents=True)
        text='技术方案'
        raw={'version':2,'blocks':[{'id':'B1','kind':'paragraph','text':text}],
            'corrected':{'sections':[{'id':'S1','title':'投标文件格式','parent_id':None}],
                'fragments':[{'id':'F1','block_id':'B1','kind':'paragraph','text':text,
                              'start_offset':0,'end_offset':len(text),'section_id':'S1','chapter_id':'S1'}]},
            'tables':{},'source_tables':{}}
        (folder/'document.json').write_text(json.dumps(raw))
        for name in ['manifest.json','validation.json']:
            (folder/name).write_text(json.dumps({'status':'completed','issues':[]}))
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'provider':'train','base_url':'http://never-called','model':'fake'}))
    responses=tmp_path/'responses.json'
    responses.write_text(json.dumps({'E01':['无'],'N01-A':['总体技术方案'],'N05':['总体技术方案']}))
    return SimpleNamespace(input_dir=incoming,output=tmp_path/'batch',config=config,
        samples=[f'S{n:02d}' for n in range(1,samples+1)],parallel_files=5,concurrency=4,
        max_total_in_flight=20,train_batch_size=2,http_timeout=600,task_timeout=3600,poll_timeout=7200,
        fake_responses=responses)


def test_batch_configuration_enforces_twenty_logical_inputs(tmp_path):
    args=fixture(tmp_path)
    config=batch.configured(args)
    assert config.max_in_flight==4 and args.parallel_files*config.max_in_flight==20
    assert config.train_batch_size==2 and config.train_max_batches==2
    assert (config.timeout_seconds,config.train_timeout_seconds,config.train_poll_timeout_seconds)==(600,3600,7200)
    assert config.thinking_default is False and config.temperature==0
    args.parallel_files=6
    with pytest.raises(ValueError,match='exceeds total limit'):
        batch.configured(args)


def test_batch_real_cli_simulation_from_existing_reading_uses_frozen_source(tmp_path):
    args=fixture(tmp_path)
    assert asyncio.run(batch.run(args))==0
    state=json.loads((args.output/'batch.json').read_text())
    assert state['simulation'] and state['final_directories']==2
    assert all(x['has_final_directory'] and x['reading_hashes_unchanged'] for x in state['items'])
    for x in state['items']:
        command=x['command']
        assert command[command.index('--reader')+1]=='reading'
        assert '--stop-after' in command and command[command.index('--stop-after')+1]=='directory'
        assert (args.output/x['sample']/'generation/exports/backbone.md').is_file()
        assert (args.output/'directories'/f"{x['sample']}.md").read_text()=='总体技术方案'
    assert (args.output/'source_snapshot/src/bid_outline/workflow/collaborative_runner.py').is_file()
    assert (args.output/'source_snapshot/sha256.json').is_file()


def test_batch_file_concurrency_and_failure_isolation(tmp_path,monkeypatch):
    args=fixture(tmp_path,4)
    args.parallel_files=3
    active=peak=0
    async def launch(*command,**kwargs):
        assert 'outline_gen.cli.read_document' not in command
        run_dir=Path(command[command.index('--run')+1])
        sid=run_dir.parent.name
        assert 'source_snapshot/src' in kwargs['env']['PYTHONPATH']
        class Process:
            pid=100+int(sid[1:])
            returncode=None
            async def wait(self):
                nonlocal active,peak
                active+=1
                peak=max(peak,active)
                try:
                    await asyncio.sleep(.02)
                    if sid=='S01':
                        self.returncode=1
                    else:
                        (run_dir/'exports').mkdir(parents=True)
                        (run_dir/'exports/provenance.json').write_text(json.dumps({
                            'status':'incomplete','directory_text':'最终目录','draft_text':'草稿',
                            'directory_state':'final_text','unprocessed_ranges':[{'scope':'upstream_reader'}]}))
                        self.returncode=2
                    return self.returncode
                finally:
                    active-=1
        return Process()
    monkeypatch.setattr(batch.asyncio,'create_subprocess_exec',launch)
    assert asyncio.run(batch.run(args))==2
    state=json.loads((args.output/'batch.json').read_text())
    assert peak==3 and state['items'][0]['status']=='failed'
    assert state['final_directories']==3
    assert all(x['status']=='incomplete' for x in state['items'][1:])
    assert state['items'][1]['unprocessed_count']==1
