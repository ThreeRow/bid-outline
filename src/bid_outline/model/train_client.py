"""Text train provider: one logical task per ticket, bounded polling, no resubmit.

The service accepts a single text query, not role-separated chat messages.
The conversion below is explicit, deterministic, and retained in wire audit.
"""
import asyncio
import os
import time

import httpx

from .client import ModelClientError, ModelResponse


def train_query(messages):
    """Preserve all system/user messages in order without depending on history."""
    if (len(messages) == 1 and messages[0].get('role') == 'user'
            and isinstance(messages[0].get('content'), str)):
        return messages[0]['content']
    parts = ['以下为一次独立任务。系统说明规定任务边界；用户消息依次提供本次任务、材料和必要校验反馈。']
    for index, message in enumerate(messages, 1):
        if message.get('role') not in {'system', 'user', 'assistant'} or not isinstance(message.get('content'), str):
            raise ValueError('train accepts text system/user/assistant messages only')
        label = {'system': '系统说明', 'user': '用户消息', 'assistant': '提供的助手消息'}[message['role']]
        parts.append(f'{index}. {label}\n{message["content"]}')
    return '\n\n'.join(parts)


class TrainClientError(ModelClientError):
    def __init__(self, message, *, raw=None, status_code=None, poll_resume_available=False):
        # Never let the executor retry a submission with an uncertain outcome.
        super().__init__(message, retryable=False, raw=raw, status_code=status_code)
        self.poll_resume_available = poll_resume_available


class TrainModelClient:
    """All submissions and polls remain within the executor's global semaphore."""

    def __init__(self, config):
        self.config = config
        key = os.environ.get(config.api_key_env)
        self.http = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            limits=httpx.Limits(max_connections=config.max_in_flight, max_keepalive_connections=config.max_in_flight),
            transport=httpx.AsyncHTTPTransport(retries=0),
            headers={'Authorization': 'Bearer ' + key} if key else {},
        )

    def wire_request(self, request):
        return {'query_list': [{
            'id': request.get('_task_id', 'query-1'),
            'query': train_query(request['messages']),
            'timeout': self.config.train_timeout_seconds,
            'temperature': request['temperature'],
            # This train API names its total model window `max_tokens`.
            # request['max_tokens'] is only the executor's generation reserve
            # (and OpenAI output cap), not this service parameter.
            'max_tokens': self.config.service_context_tokens,
            'res_type': 'str',
            'level': self.config.train_level,
            'enable_thinking': int(self.config.thinking_for(request.get('_task_type'))),
        }]}

    async def _post(self, path, payload, audit, label, timeout=None):
        # Full HTTP payload precedes every operation, including read-only polling.
        await audit(label + '.request.json', {'path': path, 'body': payload})
        try:
            response = await self.http.post(self.config.base_url + path, json=payload, timeout=timeout or self.config.timeout_seconds)
        except httpx.TransportError as exc:
            await audit(label + '.error.json', {'error': type(exc).__name__})
            raise TrainClientError(type(exc).__name__) from exc
        try:
            body = response.json()
        except ValueError:
            body = response.text
        await audit(label + '.response.json', {'status_code': response.status_code, 'body': body})
        if response.is_error:
            raise TrainClientError(f'train HTTP {response.status_code}', raw=body, status_code=response.status_code)
        if not isinstance(body, dict) or body.get('status') != 1:
            raise TrainClientError('train response has no successful status', raw=body)
        return body

    async def generate(self, request):
        raise ValueError('TrainModelClient requires generate_audited via ModelExecutor')

    async def generate_audited(self, request, audit, ticket=None):
        if (getattr(self, 'batcher', None) is not None and ticket is None
                and self.config.protocol_for(request['_task_type']) == 'text-v1'):
            return await self.batcher.generate(request, audit)
        if ticket is None:
            payload = self.wire_request(request)
            await audit('train.submit_started.json', {'query_id': payload['query_list'][0]['id']})
            submission = await self._post('/api/v2/chat_train', payload, audit, 'train.submit')
            response_id = submission.get('responseId')
            if not isinstance(response_id, str) or not response_id.strip():
                raise TrainClientError('train submission missing responseId; outcome uncertain', raw=submission)
            ticket = {'response_id': response_id, 'query_id': payload['query_list'][0]['id'], 'submission': submission}
            # Durable ticket is essential: timeout/cancellation resumes this job.
            await audit('train.ticket.json', ticket)
        else:
            await audit('train.ticket.json', ticket)
        body = await self.poll_body(ticket, audit)
        return self._response(body, ticket)

    async def poll_body(self, ticket, audit, *, shared=False):
        if getattr(self, 'batcher', None) is not None and ticket.get('batch_id'):
            terminal = self.batcher.root / ticket['batch_id'] / 'train.terminal.json'
            if terminal.exists():
                import json
                body = json.loads(terminal.read_text())
                await audit('train.terminal.json', body)
                return body
            if not shared:
                body = await self.batcher.resume_body(ticket)
                await audit('train.terminal.json', body)
                return body
        deadline = time.monotonic() + self.config.train_poll_timeout_seconds
        failures, number = 0, 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrainClientError('train polling deadline reached; resume the saved response_id',
                                       raw=ticket, poll_resume_available=True)
            number += 1
            try:
                body = await self._post('/api/v2/get_resp_data', {'response_id': ticket['response_id']},
                                        audit, f'train.poll-{number:06d}',
                                        timeout=min(remaining, self.config.timeout_seconds))
            except TrainClientError as exc:
                transient = exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
                if transient and failures < self.config.train_poll_retries:
                    failures += 1
                    await asyncio.sleep(min(self.config.train_poll_interval_seconds, max(0, deadline-time.monotonic())))
                    continue
                exc.poll_resume_available = True
                raise
            msg = body.get('msg')
            if not isinstance(msg, str) or not msg.strip():
                raise TrainClientError('train poll missing terminal/progress message', raw=body, poll_resume_available=True)
            if msg.strip() != '任务进行中':
                await audit('train.terminal.json', body)
                return body
            # Partial data is not adopted until the batch is terminal.
            await asyncio.sleep(min(self.config.train_poll_interval_seconds, max(0, deadline-time.monotonic())))

    @staticmethod
    def _response(body, ticket):
        rows = body.get('data')
        if not isinstance(rows, list):
            raise TrainClientError('terminal train response missing data list', raw=body)
        if ticket.get('query_ids'):
            returned = {str(row.get('id')) for row in rows if isinstance(row, dict)}
            if returned - set(ticket['query_ids']):
                raise TrainClientError('terminal batch contains unexpected query IDs', raw=body)
        selected = [row for row in rows if isinstance(row, dict) and str(row.get('id')) == str(ticket['query_id'])]
        if len(selected) != 1:
            raise TrainClientError('train result must contain exactly one matching query id', raw=body)
        item = selected[0]
        if item.get('is_success') is not True or not isinstance(item.get('answer'), str):
            raise TrainClientError('train item failed or answer is not text', raw=body)
        answer = item['answer']
        reasoning = item.get('reasoning_content', item.get('reasoning'))
        finish = item.get('finish_reason') or 'stop'
        # Only a leading, explicitly delimited thinking block is separated.
        if answer.lstrip().startswith('<think>'):
            text = answer.lstrip()
            end = text.find('</think>', len('<think>'))
            if end < 0:
                finish, reasoning, answer = 'length', text[len('<think>'):], ''
            else:
                reasoning, answer = text[len('<think>'):end], text[end+len('</think>'):].lstrip('\r\n ')
        raw = {'provider': 'train', **ticket, 'result': body,
               'finish_reason_origin': 'service' if item.get('finish_reason') else 'terminal_item_success'}
        return ModelResponse(answer, finish, raw, item.get('usage') or {},
                             ticket['response_id'], reasoning)

    async def aclose(self):
        if getattr(self, 'batcher', None) is not None:
            await self.batcher.aclose()
        await self.http.aclose()

    def enable_batching(self, run_dir):
        if self.config.train_batch_size > 1:
            from .train_batch import TrainBatcher
            self.batcher = TrainBatcher(self, run_dir)

    def saved_ticket(self, task_id, fingerprint):
        if getattr(self, 'batcher', None) is not None:
            return self.batcher.saved_ticket(task_id, fingerprint)
        return None
