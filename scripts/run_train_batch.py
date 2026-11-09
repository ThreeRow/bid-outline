"""Timed DOCX batch: existing OpenAI reader, new train generation provider.

No service probing, resource installation, deletion or automatic resubmission.
Each stage runs in a separate process. Outputs persist throughout the run.
"""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bid_outline.config import load_config, AppConfig
from bid_outline.model.task_specs import TASK_TYPES
from bid_outline.export.markdown import write_atomic


def stamp():
    return datetime.now(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds')


def save(path, data):
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def stage_commands(source, folder, config_path, args):
    config = load_config(config_path)
    reading_input = (min(config.context_tokens, config.service_context_tokens) -
                     max(args.max_tokens, args.heading_max_tokens) - config.safety_tokens)
    if reading_input <= 1024:
        raise ValueError('reading requires input capacity beyond its fixed prompt overhead')
    reader_thinking = getattr(args, 'reader_thinking', None) or (
        args.thinking if args.thinking != 'config' else
        ('on' if config.thinking_for() else 'off'))
    reading = [sys.executable, '-B', '-m', 'outline_gen.cli.read_document',
               '--input', str(source), '--output-dir', str(folder / 'reading'),
               '--provider', 'vllm', '--runtime', 'api', '--base-url', args.reader_base_url.rstrip('/'),
               '--model', args.reader_model, '--tokenizer', str(args.tokenizer.resolve()),
               '--thinking', reader_thinking, '--heading-max-tokens', str(args.heading_max_tokens),
               '--max-tokens', str(args.max_tokens), '--workers', str(args.concurrency),
               '--heading-workers', str(args.concurrency),
               '--request-max-tokens', str(reading_input),
               '--table-chunk-tokens', str(min(6000, reading_input // 2)),
               '--toc-max-tokens', str(min(11000, reading_input - 1024))]
    generation = [sys.executable, '-B', '-m', 'bid_outline', 'run', '--reader', 'reading',
                  '--input', str(folder / 'reading'), '--config', str(config_path),
                  '--run', str(folder / 'generation')]
    return reading, generation


async def run(args):
    sources = sorted(p.resolve() for p in args.input_dir.iterdir()
                     if p.is_file() and p.suffix.lower() == '.docx' and not p.name.startswith('~$'))
    if not sources:
        raise ValueError('input directory has no DOCX files')
    if not args.tokenizer.is_dir():
        raise ValueError('reading tokenizer must be an existing local directory')
    config = load_config(args.config)
    if config.provider != 'train':
        raise ValueError('generation configuration must select provider=train')
    data = config.model_dump(mode='json')
    data.update(max_in_flight=args.concurrency, workers=max(args.concurrency, config.workers),
                queue_capacity=max(args.concurrency * 4, config.queue_capacity),
                max_tokens=args.max_tokens,
                tokenizer=str((getattr(args, 'generation_tokenizer', None) or args.tokenizer).resolve()),
                context_tokens=getattr(args, 'context_tokens', None) or config.context_tokens,
                service_context_tokens=getattr(args, 'service_context_tokens', None) or config.service_context_tokens)
    if args.thinking != 'config':
        # Explicit global switch overrides a stage profile, rather than silently
        # retaining contradictory per-task switches.
        data.update(train_enable_thinking=args.thinking == 'on',
                    thinking_default=args.thinking == 'on', thinking_by_task={})
    if getattr(args, 'outline_thinking', None):
        data['thinking_by_task'].update({f'P{i:02d}': args.outline_thinking == 'on'
                                         for i in range(7, 19)})
        from bid_outline.model.text_tasks import TASKS
        data['thinking_by_task'].update({task: args.outline_thinking == 'on'
                                        for task in TASKS if not task.startswith('N01')})
    if getattr(args, 'directory_protocol', None):
        data['directory_protocol'] = args.directory_protocol
    config = AppConfig.model_validate(data)
    if config.context_tokens > config.service_context_tokens:
        raise ValueError('logical context_tokens exceeds the configured service window')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config_path = output / 'generation.config.json'
    save(config_path, config.model_dump(mode='json'))
    start = time.monotonic()
    items = [{'input': str(p), 'directory': f'{i:02d}_{p.stem}', 'status': 'pending', 'stages': {}}
             for i, p in enumerate(sources, 1)]
    manifest = {'status': 'running', 'started_at': stamp(), 'parallel_files': args.parallel_files,
                'concurrency_per_file': args.concurrency, 'thinking': args.thinking,
                'reading_provider': 'openai/vllm', 'generation_provider': 'train',
                'reader_base_url': args.reader_base_url, 'reader_model': args.reader_model,
                'tokenizer': str(args.tokenizer.resolve()),
                'generation_tokenizer': config.tokenizer,
                'service_context_tokens': config.service_context_tokens,
                'context_tokens': config.context_tokens,
                'generation_includes_thinking': True,
                'thinking_by_task': {task: config.thinking_for(task) for task in TASK_TYPES},
                'reader_thinking': getattr(args, 'reader_thinking', None) or
                    (args.thinking if args.thinking != 'config' else ('on' if config.thinking_for() else 'off')),
                'directory_protocol': config.directory_protocol,
                'note': 'Local tokenizers; server model/template compatibility unverified.', 'items': items}

    def publish():
        manifest['elapsed_seconds'] = round(time.monotonic() - start, 3)
        save(output / 'timing.json', manifest)
        rows = ['# 批量执行及计时', '', f"状态：{manifest['status']}；整批墙钟时间：{manifest['elapsed_seconds']} 秒", '',
                '| 文件 | 状态 | 阅读秒 | 生成秒 | 单文件秒 |', '|---|---|---:|---:|---:|']
        for item in items:
            st = item['stages']
            rows.append(f"| {Path(item['input']).name.replace('|', '/')} | {item['status']} | "
                        f"{st.get('reading', {}).get('elapsed_seconds', '')} | "
                        f"{st.get('generation', {}).get('elapsed_seconds', '')} | {item.get('elapsed_seconds', '')} |")
        write_atomic(output / 'timing.md', '\n'.join(rows) + '\n')

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    generation_env = dict(env, PYTHONPATH=str(ROOT / 'src'))
    reader_env = dict(env, PYTHONPATH=str(ROOT / 'document-reading'))
    if env.get(config.api_key_env):
        reader_env['OUTLINE_GEN_API_KEY'] = env[config.api_key_env]
    semaphore = asyncio.Semaphore(args.parallel_files)

    async def stage(item, name, command, cwd, environment):
        folder = output / item['directory']
        state = {'started_at': stamp(), 'status': 'running', 'command': command}
        item['stages'][name] = state
        before = time.monotonic()
        process = None
        publish()
        try:
            with (folder / f'{name}.log').open('x', encoding='utf-8') as log:
                process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=environment,
                                                               stdout=log, stderr=asyncio.subprocess.STDOUT)
                code = await process.wait()
            state.update(exit_code=code, status='completed' if code == 0 else 'completed_with_issues' if code == 2 else 'failed')
            return code
        except asyncio.CancelledError:
            state['status'] = 'cancelled'
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        finally:
            state.update(finished_at=stamp(), elapsed_seconds=round(time.monotonic() - before, 3))
            publish()

    async def one(source, item):
        async with semaphore:
            before = time.monotonic()
            item.update(status='running', started_at=stamp())
            folder = output / item['directory']
            folder.mkdir()
            try:
                reading, generation = stage_commands(source, folder, config_path, args)
                code = await stage(item, 'reading', reading, ROOT / 'document-reading', reader_env)
                if code not in (0, 2) or not (folder / 'reading/document.json').is_file():
                    item['status'] = 'failed_reading'
                    return
                reader_code = code
                code = await stage(item, 'generation', generation, ROOT, generation_env)
                item['status'] = 'failed_generation' if code not in (0, 2) else (
                    'completed_with_issues' if code == 2 or reader_code == 2 else 'completed')
            except asyncio.CancelledError:
                item['status'] = 'cancelled'
                raise
            except Exception as exc:
                item.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            finally:
                item.update(finished_at=stamp(), elapsed_seconds=round(time.monotonic() - before, 3))
                publish()
                print(f"{Path(item['input']).name}: {item['status']} ({item['elapsed_seconds']}s)", flush=True)

    publish()
    print(f'Output: {output}', flush=True)
    tasks = [asyncio.create_task(one(source, item)) for source, item in zip(sources, items)]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in items:
            if item['status'] == 'pending':
                item['status'] = 'cancelled'
        manifest['status'] = 'cancelled'
        raise
    else:
        manifest['status'] = ('failed' if any(i['status'].startswith('failed') for i in items) else
                              'completed_with_issues' if any(i['status'] != 'completed' for i in items) else 'completed')
    finally:
        manifest['finished_at'] = stamp()
        publish()
    return 1 if manifest['status'] == 'failed' else 2 if manifest['status'] == 'completed_with_issues' else 0


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=ROOT / 'test_inputs/input1')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parallel-files', type=positive, default=4)
    parser.add_argument('--concurrency', type=positive, default=4)
    parser.add_argument('--thinking', choices=['config', 'on', 'off'], default='config',
                        help='config preserves per-task switches; on/off overrides all generation tasks')
    parser.add_argument('--reader-thinking', choices=['on', 'off'], help='Independent document-reading switch')
    parser.add_argument('--outline-thinking', choices=['on', 'off'], help='Override directory generation thinking after global switch')
    parser.add_argument('--directory-protocol', choices=['strict', 'markdown-v1'],
                        help='Output format of P08/P11/P12/P16; default from config')
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/train.text.example.json')
    parser.add_argument('--reader-base-url', default='http://10.21.150.137:50007/v1')
    parser.add_argument('--reader-model', default='qwen35')
    parser.add_argument('--tokenizer', type=Path, default=Path('/groups/g900403/home/share/zxc/models/Qwen3.5-27B'))
    parser.add_argument('--generation-tokenizer', type=Path, help='Generation fast tokenizer; defaults to --tokenizer')
    parser.add_argument('--context-tokens', type=positive, help='Logical window, including thinking/output; default from config')
    parser.add_argument('--service-context-tokens', type=positive, help='Verified service window; default from config (20480)')
    parser.add_argument('--max-tokens', type=positive, default=8192)
    parser.add_argument('--heading-max-tokens', type=positive, default=4096)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
