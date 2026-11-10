"""Generate directories from existing reading artifacts with bounded file concurrency.

Creates a frozen source snapshot and independent generation runs. Never reruns
reading, probes a service, or automatically resubmits an existing run.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bid_outline import __version__
from bid_outline.config import AppConfig, load_config
from bid_outline.export.markdown import write_atomic


def stamp():
    return datetime.now(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds')


def save(path, value):
    write_atomic(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def read_optional(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configured(args):
    base=load_config(args.config)
    if base.provider!='train':
        raise ValueError('this batch requires provider=train')
    if args.parallel_files*args.concurrency>args.max_total_in_flight:
        raise ValueError('file concurrency multiplied by request concurrency exceeds total limit')
    data=base.model_dump(mode='json')
    data.update(max_in_flight=args.concurrency,workers=max(base.workers,args.concurrency),
        queue_capacity=max(base.queue_capacity,args.concurrency*4),
        train_batch_size=args.train_batch_size,
        train_max_batches=math.ceil(args.concurrency/min(args.concurrency,args.train_batch_size)),
        timeout_seconds=args.http_timeout,train_timeout_seconds=args.task_timeout,
        train_poll_timeout_seconds=args.poll_timeout,train_poll_interval_seconds=5.0,
        temperature=0.0,thinking_default=False,thinking_by_task={},train_enable_thinking=False,
        stop_after='directory',directory_assistance=True,extraction_mode='direct',transport_retries=0)
    result=AppConfig.model_validate(data)
    if not args.fake_responses and (not result.tokenizer or not Path(result.tokenizer).is_dir()):
        raise ValueError('real execution requires an existing local tokenizer')
    if result.train_poll_timeout_seconds<=result.train_timeout_seconds:
        raise ValueError('polling deadline must exceed the requested server task timeout')
    return result


def inputs(args):
    source_batch=read_optional(args.input_dir/'batch.json')
    names={x['sample']:x for x in source_batch.get('items',[])}
    rows=[]
    for sid in args.samples:
        reading=(args.input_dir/sid/'reading').resolve()
        for name in ['document.json','manifest.json','validation.json']:
            if not (reading/name).is_file():
                raise ValueError(f'{sid}: missing reading/{name}')
        raw=json.loads((reading/'document.json').read_text())
        if raw.get('version')!=2 or 'corrected' not in raw:
            raise ValueError(f'{sid}: expected version=2 corrected reading data')
        manifest=read_optional(reading/'manifest.json')
        validation=read_optional(reading/'validation.json')
        if manifest.get('status') not in {'completed','incomplete'} or validation.get('status') not in {'completed','incomplete'}:
            raise ValueError(f'{sid}: reading/validation is not an adoptable terminal artifact')
        hashes={name:digest(reading/name) for name in ['document.json','manifest.json','validation.json','source_map.json']
                if (reading/name).is_file()}
        rows.append({'sample':sid,'reading':str(reading),'source_docx':names.get(sid,{}).get('input'),
            'reading_status':manifest['status'],'reading_validation_status':validation['status'],
            'reading_hashes':hashes,'status':'pending','has_final_directory':False,'has_draft':False})
    return rows


async def run(args):
    config=configured(args)
    print('Checking existing reading inputs before any model requests...',flush=True)
    items=inputs(args)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    # Freeze all executable code and prompt assets before any request is issued.
    snapshot=output/'source_snapshot'
    shutil.copytree(ROOT/'src',snapshot/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copyfile(Path(__file__),snapshot/'run_reading_batch.py')
    hashes={str(p.relative_to(snapshot)):digest(p) for p in sorted(snapshot.rglob('*')) if p.is_file()}
    save(snapshot/'sha256.json',hashes)
    config_path=output/'generation.config.json'
    save(config_path,config.model_dump(mode='json'))
    start=time.monotonic()
    manifest={'status':'running','pid':os.getpid(),'started_at':stamp(),'code_version':__version__,
        'simulation':bool(args.fake_responses),'scope':'existing document-reading → complete ABC extraction → final directory',
        'parallel_files':args.parallel_files,'max_in_flight_per_file':config.max_in_flight,
        'max_total_in_flight':args.parallel_files*config.max_in_flight,
        'train_batch_size':config.train_batch_size,'train_max_batches_per_file':config.train_max_batches,
        'config':str(config_path),'source_snapshot':str(snapshot),'semantic_quality':'not_evaluated','items':items}
    active={}

    def publish():
        for item in items:
            folder=output/item['sample']/'generation'
            state=read_optional(folder/'manifest.json')
            runtime=read_optional(folder/'runtime.json')
            if state:
                item['generation_status']=state.get('status')
                item['generation_stage']=state.get('stage')
                item['issue_count']=state.get('issue_count')
            if runtime:
                item['runtime']=runtime
        manifest['updated_at']=stamp()
        manifest['elapsed_seconds']=round(time.monotonic()-start,3)
        manifest['counts']=dict(Counter(x['status'] for x in items))
        manifest['final_directories']=sum(x['has_final_directory'] for x in items)
        manifest['current_in_flight_snapshot']=sum(x.get('runtime',{}).get('in_flight',0)
            for x in items if x['status']=='running')
        save(output/'batch.json',manifest)
        lines=['# 24份目录生成测试进度','',f"状态：{manifest['status']}；更新：{manifest['updated_at']}",
            f"文件并发{args.parallel_files}，每文件请求上限{config.max_in_flight}，全批逻辑在途上限{manifest['max_total_in_flight']}。",
            '','| 样本 | 原阅读状态 | 测试状态 | 生成阶段 | 最终目录 | 耗时秒 |',
            '|---|---|---|---|---|---:|']
        for x in items:
            label='[查看]('+x['sample']+'/generation/exports/backbone.md)' if x.get('has_final_directory') else (
                '草稿' if x.get('has_draft') else '')
            lines.append(f"| {x['sample']} | {x['reading_status']} | {x['status']} | {x.get('generation_stage','')} | {label} | {x.get('elapsed_seconds','')} |")
        lines+=['','完成仅代表调用链状态，原阅读问题、生成失败及是否有最终目录分别记录；未作语义质量验收。']
        write_atomic(output/'progress.md','\n'.join(lines)+'\n')

    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',PYTHONPATH=str(snapshot/'src'))
    slots=asyncio.Semaphore(args.parallel_files)
    async def one(item):
        async with slots:
            folder=output/item['sample']
            folder.mkdir()
            before=time.monotonic()
            item.update(status='running',started_at=stamp())
            command=[sys.executable,'-B','-m','bid_outline','run','--reader','reading','--input',item['reading'],
                '--config',str(config_path),'--run',str(folder/'generation'),'--stop-after','directory']
            if args.fake_responses:
                command+=['--fake-responses',str(args.fake_responses.resolve())]
            item['command']=command
            publish()
            process=None
            try:
                with (folder/'generation.log').open('x',encoding='utf-8') as log:
                    process=await asyncio.create_subprocess_exec(*command,cwd=output,env=env,
                        stdin=asyncio.subprocess.DEVNULL,stdout=log,stderr=asyncio.subprocess.STDOUT)
                    active[item['sample']]=process
                    item['pid']=process.pid
                    publish()
                    item['exit_code']=await process.wait()
                result=read_optional(folder/'generation/exports/provenance.json')
                item.update(has_final_directory=bool(result.get('directory_text')),
                    has_draft=bool(result.get('draft_text')),directory_state=result.get('directory_state'),
                    unprocessed_count=len(result.get('unprocessed_ranges',[])))
                item['status']='failed' if item['exit_code'] not in (0,2) else result.get('status','missing_result')
                if result.get('directory_text'):
                    write_atomic(output/'directories'/f"{item['sample']}.md",result['directory_text'])
                if result.get('draft_text') and not result.get('directory_text'):
                    write_atomic(output/'drafts'/f"{item['sample']}.md",result['draft_text'])
                item['reading_hashes_unchanged']=all(digest(Path(item['reading'])/name)==sha
                    for name,sha in item['reading_hashes'].items())
            except asyncio.CancelledError:
                item['status']='cancelled'
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(),15)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                raise
            except Exception as exc:
                item.update(status='failed',error=f'{type(exc).__name__}: {exc}')
            finally:
                active.pop(item['sample'],None)
                item.update(finished_at=stamp(),elapsed_seconds=round(time.monotonic()-before,3))
                publish()
                print(item['sample'],item['status'],'final_directory=',item['has_final_directory'],flush=True)

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            publish()
    publish()
    print(f'Starting {len(items)} generation runs; output={output}; max_total_in_flight={manifest["max_total_in_flight"]}',flush=True)
    timer=asyncio.create_task(heartbeat())
    tasks=[asyncio.create_task(one(x)) for x in items]
    try:
        await asyncio.gather(*tasks)
        manifest['status']='finished_with_issues' if any(x['status']!='completed' for x in items) else 'completed'
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for item in items:
            if item['status']=='pending':
                item['status']='cancelled'
        manifest['status']='cancelled'
        raise
    finally:
        timer.cancel()
        await asyncio.gather(timer,return_exceptions=True)
        manifest['finished_at']=stamp()
        publish()
    return 0 if manifest['status']=='completed' else 2


def positive(value):
    number=int(value)
    if number<1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


async def entry(args):
    task=asyncio.current_task()
    loop=asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM,task.cancel)
    return await run(args)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir',type=Path,default=ROOT/'reviews/document_reading_location_24_20261109')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'configs/train.text.example.json')
    parser.add_argument('--samples',nargs='+',default=[f'S{n:02d}' for n in range(1,25)])
    parser.add_argument('--parallel-files',type=positive,default=5)
    parser.add_argument('--concurrency',type=positive,default=4)
    parser.add_argument('--max-total-in-flight',type=positive,default=20)
    parser.add_argument('--train-batch-size',type=positive,default=2)
    parser.add_argument('--http-timeout',type=positive,default=600)
    parser.add_argument('--task-timeout',type=positive,default=3600)
    parser.add_argument('--poll-timeout',type=positive,default=7200)
    parser.add_argument('--fake-responses',type=Path,help='Explicit offline integration check; no service calls')
    args=parser.parse_args()
    if len(set(args.samples))!=len(args.samples) or any(not __import__('re').fullmatch(r'S\d{2}',x) for x in args.samples):
        parser.error('samples must be unique S01-style identifiers')
    try:
        return asyncio.run(entry(args))
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    except (KeyboardInterrupt,asyncio.CancelledError):
        return 130


if __name__=='__main__':raise SystemExit(main())
