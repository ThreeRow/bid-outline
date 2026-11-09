"""Single audited execution gateway for all generation tasks."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import time
from uuid import uuid4

from .call_view import new_call_path, publish, stage
from .audit_io import AuditIO
from .budget import BudgetExceeded, TokenBudget, TaskExecutionError
from .client import ModelClientError, ModelResponse
from .parsers import ParseError, parse_output, retry_guidance, close_complete_final_record
from .task_specs import get_spec, render_task
from .references import prepare_inputs, prepare_call_inputs, restore_output, render_readable_view, render_view
from .compact_protocol import normalize_compact, compact_retry_guidance, VERSION as PROTOCOL_VERSION


class OutputTruncated(TaskExecutionError):
    pass


class CallLimitExceeded(TaskExecutionError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_once(path: Path, value, *, text: bool = False) -> None:
    payload = value if text else json.dumps(value, ensure_ascii=False, indent=2)
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class ModelExecutor:
    """Serialize task adoption and enforce shared audited model-call limits."""

    def __init__(self, config, client, repository, artifacts):
        self.config, self.client = config, client
        self.repository, self.artifacts = repository, artifacts
        self.calls_dir = Path(repository.run_dir) / 'calls'
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.budget = TokenBudget(config)
        self.semaphore = asyncio.Semaphore(config.max_in_flight)
        self.locks = {}
        self.stats = {'in_flight': 0, 'peak_in_flight': 0, 'calls': 0, 'adopted': 0, 'cache_hits': 0}
        self.io = AuditIO()
        self.requests_by_task = {}
        self.stage_numbers = {}
        self.reserved_calls = 0
        self.recorded_count = 0
        self.request_scan_count = 1
        self.request_read_count = 0
        self.navigation_generation = 0
        self.navigation_published = 0
        self.navigation_lock = asyncio.Lock()
        self.active = set()
        self.closing = False
        # One startup reconciliation includes orphan requests that reached disk
        # before SQLite attempt registration. Normal execution never scans again.
        for request_path in self.calls_dir.rglob('request.json'):
            try:
                recorded = json.loads(request_path.read_text(encoding='utf-8'))
                if not {'_task_id', '_fingerprint', 'messages'} <= recorded.keys():
                    raise ValueError('request identity is incomplete')
            except (ValueError, OSError) as exc:
                raise OSError(f'corrupt startup request audit: {request_path}') from exc
            self.request_read_count += 1
            task = recorded.get('_task_id')
            if task:
                self.requests_by_task.setdefault(task, []).append((request_path.parent, recorded.get('_fingerprint')))
            self.reserved_calls += 1
            self.recorded_count += 1
        for folder in self.calls_dir.iterdir():
            if folder.is_dir():
                numbers = [int(p.name.split('_',1)[0]) for p in folder.iterdir()
                           if p.is_dir() and p.name.split('_',1)[0].isdigit()]
                self.stage_numbers[folder.name] = max(numbers, default=0)

        self._persist_runtime('initialized')

    def _persist_runtime(self, state: str, snapshot=None) -> None:
        """Atomically publish live counters; abandoned temp files remain audit evidence."""
        destination = Path(self.repository.run_dir) / 'runtime.json'
        temporary = destination.with_name('.runtime-' + uuid4().hex + '.json')
        write_once(temporary, snapshot or {**self.stats, 'heartbeat_at': utcnow(), 'state': state,
            'pid': os.getpid(), 'recorded_attempts': self.recorded_count, 'reserved_attempts': self.reserved_calls})
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def execute(self, task_type: str, inputs: dict, *, task_id: str,
                      dependencies: list[str] | None = None) -> dict:
        """Reuse, recover, or execute one fingerprinted logical model task."""
        if self.closing:
            raise RuntimeError('executor is closing')
        current = asyncio.current_task()
        self.active.add(current)
        try:
            async with self.locks.setdefault(task_id, asyncio.Lock()):
                try:
                    return await self._execute(task_type, inputs, task_id, dependencies or [])
                except BaseException as exc:
                    task = self.repository.get_task(task_id)
                    if task and task.get('status') != 'succeeded':
                        self.repository.upsert_task(task_id, task_type, task['fingerprint'],
                            'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed', error=str(exc))
                        for attempt in self.repository.list_attempts(task_id):
                            if attempt.get('fingerprint') == task['fingerprint'] and not attempt.get('finished_at'):
                                self.repository.record_attempt(attempt['call_id'], task_id, {
                                    'error_kind': 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'execution',
                                    'error': str(exc) or type(exc).__name__, 'finished_at': utcnow()})
                                self.navigation_generation += 1
                    raise
        finally:
            self.active.discard(current)

    async def _write(self, *args, **kwargs):
        return await self.io.run(write_once, *args, **kwargs)

    async def _runtime(self, state):
        # The function executes on the I/O worker; shared counters are snapshotted
        # by its serialized entry, never by a SQLite operation in that worker.
        snapshot = {**self.stats, 'heartbeat_at': utcnow(), 'state': state, 'pid': os.getpid(),
                    'recorded_attempts': self.recorded_count, 'reserved_attempts': self.reserved_calls}
        await self.io.run(self._persist_runtime, state, snapshot)

    async def flush_navigation(self):
        async with self.navigation_lock:
            generation = self.navigation_generation
            if generation == self.navigation_published:
                return
            attempts, tasks = self.repository.list_attempts(), self.repository.list_tasks()
            class Snapshot:
                def list_attempts(self): return attempts
                def list_tasks(self): return tasks
            await self.io.run(publish, Path(self.repository.run_dir), Snapshot(), None)
            # Updates arriving during await remain dirty for the next flush.
            self.navigation_published = generation

    def _parse(self, task_type, response, inputs):
        if response.finish_reason != 'stop':
            raise OutputTruncated(f'unusable finish_reason: {response.finish_reason}')
        if self.config.protocol_for(task_type) == 'text-v1':
            if not isinstance(response.content, str) or not response.content.strip():
                raise TaskExecutionError('empty text response')
            return {'text': response.content, 'fields': {}, 'records': {}, 'supplements': []}
        aliases = inputs.get('id_aliases', {})
        validation = dict(allowed_ids=[aliases.get(x, x) for x in inputs.get('allowed_ids', [])],
                          expected_ids=([aliases.get(x, x) for x in inputs['expected_ids']]
                                        if 'expected_ids' in inputs else None),
                          id_groups={key: [aliases.get(x, x) for x in values]
                                     for key, values in inputs['id_groups'].items()} if 'id_groups' in inputs else None,
                          existing_node_basis={aliases.get(key, key): [aliases.get(rid, rid) for rid in values]
                                               for key, values in inputs.get('existing_node_basis', {}).items()})
        selected_protocol = self.config.protocol_for(task_type)
        if selected_protocol == 'markdown-v1':
            from .markdown_directory import normalize
            parsed = parse_output(task_type, normalize(task_type, response.content, inputs), **validation)
            return restore_output(task_type, parsed, {alias: ident for ident, alias in aliases.items()})
        if selected_protocol == 'compact-v1':
            parsed = parse_output(task_type, normalize_compact(task_type, response.content), **validation)
            return restore_output(task_type, parsed, {alias: ident for ident, alias in aliases.items()})
        try:
            parsed = parse_output(task_type, response.content, **validation)
        except ParseError as exc:
            if str(exc) != 'Unclosed record':
                raise
            parsed = close_complete_final_record(task_type, response.content, **validation)
        return restore_output(task_type, parsed, {alias: ident for ident, alias in aliases.items()})

    async def _adopt(self, task_id, parsed, dependencies, call_id):
        artifact_id = await self.io.run(self.artifacts.put, 'model_result', parsed, parents=dependencies, task_id=task_id)
        # Durable adoption intent precedes the database transaction. The task's
        # accepted_artifact_id is authoritative if a crash interrupts commit.
        await self._write(self.calls_dir / call_id / ('adoption-' + uuid4().hex + '.json'),
            {'artifact_id': artifact_id, 'task_id': task_id, 'created_at': utcnow(), 'status': 'commit_intent'})
        self.repository.commit_task(task_id, artifact_id)
        task = self.repository.get_task(task_id)
        self.repository.upsert_task(task_id, task['task_type'], task['fingerprint'], 'succeeded', accepted_call_id=call_id)
        self.navigation_generation += 1
        self.stats['adopted'] += 1
        return parsed

    async def _execute(self, task_type, inputs, task_id, dependencies):
        spec = get_spec(task_type)
        inputs = prepare_call_inputs(inputs)
        aliases = inputs.get('id_aliases', {})
        messages = render_task(task_type, inputs, view_version=self.config.prompt_view,
                               protocol=self.config.prompt_protocol, directory_protocol=self.config.directory_protocol)
        material_view = inputs.get('material_view')
        render_component = render_readable_view if self.config.prompt_view == 'readable-v1' else render_view
        material_counts = ({name: self.budget.text_tokens(
                                render_component({'__sections__': [[name, value]]}, aliases))
                            for name, value in material_view.get('__sections__', [])}
                           if isinstance(material_view, dict) else {})
        dependency_hashes = await self.io.run(lambda: [hashlib.sha256(json.dumps(self.artifacts.get(x), sort_keys=True, ensure_ascii=False).encode()).hexdigest() for x in dependencies])
        fingerprint_data = dict(task_type=task_type, inputs={k: v for k, v in inputs.items() if k not in ('display_name', 'trigger_stage')}, dependencies=dependencies,
            dependency_hashes=dependency_hashes,
            template_version=spec.version, parser_version=spec.parser_version, business_rules_version='01-05-v1',
            output_protocol=self.config.protocol_for(task_type), protocol_version=PROTOCOL_VERSION,
            config=self.config.model_dump(mode='json'), rendered_messages=messages, executor_version='1.8.0')
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prior = self.repository.get_task(task_id)
        if prior and prior.get('fingerprint') == fingerprint and prior.get('status') == 'succeeded':
            self.stats['cache_hits'] += 1
            return await self.io.run(self.artifacts.get, prior['accepted_artifact_id'])
        self.repository.upsert_task(task_id, task_type, fingerprint, 'running', dependencies=dependencies)
        indexed = self.requests_by_task.get(task_id, [])
        matching = [directory for directory, saved in indexed if saved == fingerprint]
        train_mode = hasattr(self.client, 'generate_audited')
        train_ticket = None
        task_count = len(indexed)
        truncated = None
        for directory in reversed(sorted(matching)):
            response_path = directory / 'response.model.json'
            if response_path.exists():
                response = ModelResponse(**json.loads(response_path.read_text(encoding='utf-8')))
                try:
                    parsed = self._parse(task_type, response, inputs)
                except ParseError:
                    continue
                except OutputTruncated as exc:
                    truncated = exc
                    continue
                for filename, value, is_text in [('output.txt', response.content, True),
                        ('parsed.json', parsed, False), ('validation.json', {'valid': True, 'recovered': True}, False)]:
                    if not (directory / filename).exists():
                        await self._write(directory / filename, value, text=is_text)
                await self._write(directory / ('recovery-' + uuid4().hex + '.json'), {'parsed': parsed, 'recovered_at': utcnow()})
                if not any(a['call_id'] == str(directory.relative_to(self.calls_dir)) for a in self.repository.list_attempts(task_id)):
                    self.repository.record_attempt(str(directory.relative_to(self.calls_dir)), task_id, dict(call_id=str(directory.relative_to(self.calls_dir)),
                        task_id=task_id, fingerprint=fingerprint, recovered_at=utcnow(), usage=response.usage,
                        task_type=task_type, attempt_no=task_count, reason='response_recovery',
                        display_name=inputs.get('display_name', task_type),
                        finish_reason=response.finish_reason, parse_status='valid', validation_status='valid',
                        adopted=False, adoption_authority='task.accepted_artifact_id'))
                return await self._adopt(task_id, parsed, dependencies, str(directory.relative_to(self.calls_dir)))
        if truncated is not None:
            raise truncated
        if train_mode:
            for directory in reversed(sorted(matching)):
                if (directory / 'response.model.json').exists():
                    continue  # Completed output: normal format retry may submit anew.
                ticket_path = directory / 'train.ticket.json'
                if ticket_path.exists():
                    train_ticket = json.loads(ticket_path.read_text(encoding='utf-8'))
                    if (not isinstance(train_ticket, dict)
                            or not isinstance(train_ticket.get('response_id'), str)
                            or not train_ticket['response_id']
                            or train_ticket.get('query_id') != task_id):
                        raise TaskExecutionError('invalid persisted train ticket identity')
                    break
                receipt_path = directory / 'train.submit.response.json'
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                    body = receipt.get('body')
                    if isinstance(body, dict) and body.get('status') == 1 and isinstance(body.get('responseId'), str) and body['responseId']:
                        train_ticket = {'response_id': body['responseId'], 'query_id': task_id, 'submission': body}
                        break
                if (directory / 'train.submit_started.json').exists():
                    raise TaskExecutionError('train submission outcome uncertain and no saved responseId; automatic resubmission refused')
        for directory in matching:
            if (directory / 'meta.json').exists():
                previous_meta = json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
                if previous_meta.get('context_exceeded'):
                    raise BudgetExceeded('service rejected this complete input budget; reorganize the task')
                if (previous_meta.get('error_kind') == 'transport' and previous_meta.get('retryable') is False
                        and not (train_mode and train_ticket and previous_meta.get('poll_resume_available'))):
                    raise TaskExecutionError('non-retryable service error for this unchanged task')
        max_tokens = spec.max_tokens or self.config.max_tokens
        try:
            budget = self.budget.check(messages, max_tokens, task_type=task_type)
        except BudgetExceeded as exc:
            await self.io.run(self.artifacts.put, 'blocked_request_budget', {
                'task_type': task_type, 'task_id': task_id, 'error': str(exc),
                'input_tokens': self.budget.count(messages, task_type=task_type), 'generation_tokens': max_tokens,
                'service_context_tokens': self.config.service_context_tokens,
                'context_tokens': self.budget.window, **self.budget.breakdown(messages, task_type=task_type),
                'material_sections_text_tokens_nonadditive': material_counts,
            }, parents=dependencies, task_id=task_id)
            raise
        saved_metadata = [json.loads((p / 'meta.json').read_text()) for p in matching if (p / 'meta.json').exists()]
        transport_errors = sum(m.get('error_kind') == 'transport' and not (
            train_mode and m.get('poll_resume_available')) for m in saved_metadata)
        format_errors = sum((json.loads((p / 'meta.json').read_text()).get('error_kind') == 'format') for p in matching if (p / 'meta.json').exists())
        if transport_errors > self.config.transport_retries or format_errors > self.config.format_retries:
            raise TaskExecutionError('retry limit already exhausted for this input')
        reason = 'initial' if not matching else 'resume'
        correction = None
        prior_format_errors = []
        for directory in matching:
            meta_path = directory / 'meta.json'
            if meta_path.exists():
                saved_meta = json.loads(meta_path.read_text(encoding='utf-8'))
                if saved_meta.get('error_kind') == 'format':
                    prior_format_errors.append(saved_meta)
        if prior_format_errors:
            latest = max(prior_format_errors, key=lambda meta: meta['attempt_no'])
            correction = (latest['call_id'], latest['error'])
        while True:
            if task_count >= self.config.max_task_calls or self.reserved_calls >= self.config.max_run_calls:
                raise CallLimitExceeded('task or run total call limit reached')
            attempt_messages = list(messages)
            if correction:
                if self.config.protocol_for(task_type) == 'markdown-v1':
                    from .markdown_directory import retry_guidance as markdown_retry
                    feedback = markdown_retry(correction[1])
                else:
                    guidance = compact_retry_guidance if self.config.protocol_for(task_type) == 'compact-v1' else retry_guidance
                    feedback = guidance(task_type, correction[1])
                attempt_messages.append({'role': 'user', 'content': feedback})
            # A retry's feedback is part of the actual input budget too.
            budget = self.budget.check(attempt_messages, max_tokens, task_type=task_type)
            budget.update(self.budget.breakdown(attempt_messages, task_type=task_type))
            budget['material_sections_text_tokens_nonadditive'] = material_counts
            # Atomic reservation: no await between checking and consuming quota.
            self.reserved_calls += 1
            parent_stage = inputs.get('trigger_stage', stage(task_type)) if task_type in ('P06','P19') else stage(task_type)
            number = self.stage_numbers.get(parent_stage, 0) + 1
            self.stage_numbers[parent_stage] = number
            call_id, display_name = await self.io.run(new_call_path, self.calls_dir, task_type, inputs, task_count + 1, number)
            directory = self.calls_dir / call_id
            request = dict(self.config.extra_body, model=self.config.model, messages=attempt_messages,
                max_tokens=max_tokens, temperature=self.config.temperature, stream=False,
                _task_type=task_type, _task_id=task_id, _fingerprint=fingerprint)
            thinking = self.config.thinking_for(task_type)
            if self.config.provider == 'openai' and thinking is not None:
                request['chat_template_kwargs'] = {
                    **request.get('chat_template_kwargs', {}), 'enable_thinking': thinking}
            meta = dict(call_id=call_id, internal_id=uuid4().hex, display_name=display_name, task_id=task_id, run_id=Path(self.repository.run_dir).name,
                task_type=task_type, attempt_no=task_count + 1, reason=reason, fingerprint=fingerprint,
                template_version=spec.version, parser_version=spec.parser_version, model=self.config.model,
                provider=self.config.provider,
                output_protocol=self.config.protocol_for(task_type), protocol_version=PROTOCOL_VERSION,
                enable_thinking=thinking,
                base_url=self.config.base_url, started_at=utcnow(), budget=budget, adopted=False,
                adoption_authority='task.accepted_artifact_id', usage={}, finish_reason=None,
                request_id=None, parse_status='not_started', validation_status='not_started')
            if correction:
                meta['format_correction_of'] = correction[0]
                meta['format_diagnostic'] = correction[1]
            request_written = False
            def persist_request():
                nonlocal request_written
                write_once(directory / 'request.json', request)
                request_written = True
            try:
                await self.io.run(persist_request)
            finally:
                # Cancellation can arrive while the thread fsyncs request.json.
                # Its durable request must still be indexed before cancellation
                # leaves this task, even though HTTP was never sent.
                if request_written:
                    self.requests_by_task.setdefault(task_id, []).append((directory, fingerprint))
                    self.recorded_count += 1
                    self.repository.record_attempt(call_id, task_id, {**meta, 'request_origin': 'durable_request', 'sent': False})
                    self.navigation_generation += 1
            if aliases:
                await self._write(directory / 'id_aliases.json', {'alias_to_id': {v: k for k, v in aliases.items()}})
            if train_mode:
                wire = {'response_id': train_ticket['response_id']} if train_ticket else self.client.wire_request(request)
                meta['request_path'] = '/api/v2/get_resp_data' if train_ticket else '/api/v2/chat_train'
                meta['request_origin'] = 'train_poll_resume' if train_ticket else 'train_submission'
                await self._write(directory / 'request.http.json', wire)
            else:
                await self._write(directory / 'request.http.json', {k: v for k, v in request.items() if not k.startswith('_')})
            await self._write(directory / 'input.md', '\n\n'.join(m['role'] + '\n' + m['content'] for m in attempt_messages), text=True)
            task_count += 1
            started = time.monotonic()
            retry_delay = None
            parsed = None
            failure = None
            try:
                async with self.semaphore:
                    meta['queue_seconds'] = time.monotonic() - started
                    self.stats['in_flight'] += 1
                    self.stats['peak_in_flight'] = max(self.stats['peak_in_flight'], self.stats['in_flight'])
                    sent = time.monotonic()
                    try:
                        await self._runtime('request_enter')
                        if train_mode and train_ticket:
                            self.stats['resumed_provider_jobs'] = self.stats.get('resumed_provider_jobs', 0) + 1
                        else:
                            self.stats['calls'] += 1
                        meta['sent'] = True
                        if train_mode:
                            async def audit(name, value):
                                await self._write(directory / name, value)
                            response = await self.client.generate_audited(request, audit, ticket=train_ticket)
                        else:
                            response = await self.client.generate(request)
                        await self._write(directory / 'response.raw.json', response.raw)
                        await self._write(directory / 'response.model.json', asdict(response))
                        await self._write(directory / 'output.txt', response.content, text=True)
                    finally:
                        meta['request_seconds'] = time.monotonic() - sent
                        self.stats['in_flight'] -= 1
                        await self._runtime('request_exit')
                meta.update(usage=response.usage, finish_reason=response.finish_reason, request_id=response.request_id)
                if response.reused_from:
                    meta['reused_from'] = response.reused_from
                    meta['request_origin'] = 'explicit_exact_request_reuse'
                    self.stats['calls'] -= 1
                    self.stats['reused_responses'] = self.stats.get('reused_responses', 0) + 1
                    await self._runtime('response_reused')
                parsed = self._parse(task_type, response, inputs)
                if parsed.get('format_repairs'):
                    await self._write(directory / 'format_repairs.json', parsed['format_repairs'])
                await self._write(directory / 'parsed.json', parsed)
                await self._write(directory / 'validation.json', {'valid': True})
                meta.update(parse_status='valid', validation_status='valid')
            except asyncio.CancelledError as exc:
                failure = exc
                meta.update(error_kind='cancelled', error='cancelled')
            except ModelClientError as exc:
                failure = BudgetExceeded(str(exc)) if exc.context_exceeded else exc
                if exc.raw is not None:
                    await self._write(directory / ('response.raw.txt' if isinstance(exc.raw, str) else 'response.raw.json'), exc.raw, text=isinstance(exc.raw, str))
                meta.update(error_kind='transport', error=str(exc), http_status=exc.status_code,
                    retryable=exc.retryable, context_exceeded=exc.context_exceeded,
                    poll_resume_available=getattr(exc, 'poll_resume_available', False))
                if exc.retryable and transport_errors < self.config.transport_retries:
                    transport_errors += 1
                    retry_delay = max(exc.retry_after, min(2 ** (transport_errors - 1), 30) + random.random() * .1)
                    reason = 'transport_retry'
            except ParseError as exc:
                failure = TaskExecutionError(str(exc))
                meta.update(error_kind='format', error=str(exc), parse_status='invalid', validation_status='invalid')
                await self._write(directory / 'validation.json', {'valid': False, 'error': str(exc)})
                if format_errors < self.config.format_retries:
                    format_errors += 1
                    retry_delay = 0
                    reason = 'format_retry'
                    correction = (call_id, str(exc))
                    train_ticket = None  # Terminal invalid answer, not a pending job.
            except BaseException as exc:
                failure = exc
                meta.update(error_kind='truncation' if isinstance(exc, OutputTruncated) else 'execution', error=str(exc))
            meta['finished_at'] = utcnow()
            await self._write(directory / 'meta.json', meta)
            self.repository.record_attempt(call_id, task_id, meta)
            self.navigation_generation += 1
            if parsed is not None and failure is None:
                return await self._adopt(task_id, parsed, dependencies, call_id)
            if retry_delay is None:
                raise failure
            await self._runtime(reason)
            await asyncio.sleep(retry_delay)

    async def aclose(self) -> None:
        self.closing = True
        current = asyncio.current_task()
        remaining = [task for task in self.active if task is not current]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        await self.io.drain()
        try:
            await self.flush_navigation()
        finally:
            await self.client.aclose()
            await self._runtime('closed')
