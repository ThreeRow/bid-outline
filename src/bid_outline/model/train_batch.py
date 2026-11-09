"""Durable train micro-batches behind independently audited executor tasks."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .train_client import TrainClientError


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class TrainBatcher:
    def __init__(self, client, run_dir):
        self.client = client
        self.root = Path(run_dir) / 'train_batches'
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending, self.tasks = [], set()
        self.resuming = {}
        self.timer = None
        self.slots = asyncio.Semaphore(client.config.train_max_batches)
        # Logical task semaphores are held by the executor throughout waiting/polling.
        self.size = min(client.config.train_batch_size, client.config.max_in_flight)

    def member_path(self, task, fingerprint):
        key = hashlib.sha256(json.dumps([task, fingerprint]).encode()).hexdigest()
        return self.root / 'members' / (key + '.json')

    def saved_ticket(self, task, fingerprint):
        path = self.member_path(task, fingerprint)
        if not path.exists():
            return None
        member = json.loads(path.read_text())
        folder = self.root / member['batch_id']
        if (folder / 'ticket.json').exists():
            ticket = json.loads((folder / 'ticket.json').read_text())
        elif (folder / 'train.submit.response.json').exists():
            saved = json.loads((folder / 'train.submit.response.json').read_text())
            body = saved.get('body', {})
            if saved.get('status_code', 500) >= 400 or not body.get('responseId'):
                raise TrainClientError('batch submission uncertain; automatic resubmission refused')
            ticket = {'response_id': body['responseId']}
        else:
            # Even pre-submit crashes are conservative: no blind replay of the batch.
            raise TrainClientError('batch membership persisted without receipt; automatic resubmission refused')
        if not isinstance(ticket.get('response_id'), str) or not ticket['response_id']:
            raise TrainClientError('invalid shared train ticket')
        return {**ticket, 'batch_id': member['batch_id'], 'query_id': task}

    async def generate(self, request, audit):
        saved = self.saved_ticket(request['_task_id'], request['_fingerprint'])
        if saved:
            return await self.client.generate_audited(request, audit, saved)
        future = asyncio.get_running_loop().create_future()
        self.pending.append((request, audit, future))
        if len(self.pending) >= self.size:
            self.flush()
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(
                self.client.config.train_batch_wait_ms / 1000, self.flush)
        return await future

    def flush(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        while self.pending:
            members, self.pending = self.pending[:self.size], self.pending[self.size:]
            task = asyncio.create_task(self.run(members))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def run(self, members):
        members = [m for m in members if not m[2].cancelled()]
        if not members:
            return
        try:
            async with self.slots:
                batch_id = 'batch-' + uuid4().hex
                folder = self.root / batch_id
                queries = [self.client.wire_request(request)['query_list'][0] for request, _, _ in members]
                payload = {'query_list': queries}
                await asyncio.to_thread(write, folder / 'members.json', [
                    {'task_id': r['_task_id'], 'fingerprint': r['_fingerprint']} for r, _, _ in members])
                for request, _, _ in members:
                    await asyncio.to_thread(write, self.member_path(request['_task_id'], request['_fingerprint']),
                        {'batch_id': batch_id, 'query_id': request['_task_id']})
                async def audit(label, value):
                    await asyncio.to_thread(write, folder / label, value)
                await audit('train.submit_started.json', {'query_ids': [q['id'] for q in queries]})
                # Per-task audit is durable before the shared HTTP submission.
                for _, member_audit, _ in members:
                    await member_audit('train.batch.json', {'batch_id': batch_id, 'query_ids': [q['id'] for q in queries]})
                receipt = await self.client._post('/api/v2/chat_train', payload, audit, 'train.submit')
                response_id = receipt.get('responseId')
                if not isinstance(response_id, str) or not response_id.strip():
                    raise TrainClientError('batch responseId missing; automatic resubmission refused')
                ticket = {'response_id': response_id, 'batch_id': batch_id,
                          'query_ids': [q['id'] for q in queries], 'submission': receipt}
                await audit('ticket.json', ticket)
                for request, member_audit, _ in members:
                    await member_audit('train.ticket.json', {**ticket, 'query_id': request['_task_id']})
                body = await self.client.poll_body(ticket, audit, shared=True)
                # A malformed batch-wide identity must not be joined by position.
                rows = body.get('data', [])
                ids = [str(x.get('id')) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []
                unknown = set(ids) - {q['id'] for q in queries}
                if unknown:
                    raise TrainClientError('terminal batch contains unexpected query IDs', raw=body)
                for request, member_audit, future in members:
                    if future.cancelled():
                        continue
                    try:
                        await member_audit('train.terminal.json', body)
                        response = self.client._response(body, {**ticket, 'query_id': request['_task_id']})
                        future.set_result(response)
                    except Exception as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            for _, _, future in members:
                if not future.done():
                    if isinstance(exc, asyncio.CancelledError):
                        future.cancel()
                    else:
                        future.set_exception(exc)

    async def resume_body(self, ticket):
        key = ticket['response_id']
        task = self.resuming.get(key)
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            async def poll():
                async with self.slots:
                    folder = self.root / ticket['batch_id'] / ('resume-' + uuid4().hex)
                    async def audit(label, value):
                        await asyncio.to_thread(write, folder / label, value)
                    body = await self.client.poll_body(ticket, audit, shared=True)
                    destination = self.root / ticket['batch_id'] / 'train.terminal.json'
                    if not destination.exists():
                        await asyncio.to_thread(write, destination, body)
                    return body
            task = asyncio.create_task(poll())
            self.resuming[key] = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        # Cancelling one logical query must not cancel the other members' poll.
        return await asyncio.shield(task)

    async def aclose(self):
        if self.timer:
            self.timer.cancel()
        for _, _, future in self.pending:
            future.cancel()
        self.pending.clear()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
