"""Claude JSON analysis adapter for the director report."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

import httpx

from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError


MESSAGES_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
MAX_PROBLEMS = 6
MAX_CHAT_TOOL_CALLS = 8
MAX_CHAT_TOOL_RESULT_CHARS = 100_000
ANALYSIS_SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string'},
        'problems': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'subject': {'type': 'string'},
                    'direction': {'type': 'string', 'enum': ['all', 'retro', 'oxbridge', 'banquet', 'yandex']},
                    'reason': {'type': 'string'},
                    'priority': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                    'action': {'type': 'string', 'enum': ['remove', 'replace', 'promote', 'review']},
                },
                'required': ['subject', 'direction', 'reason', 'priority', 'action'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['summary', 'problems'],
    'additionalProperties': False,
}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _metric(name, direction, value):
    return {
        'subject': name,
        'direction': direction,
        'quantity': str(value.get('quantity', '0')),
        'revenue': str(value.get('revenue', '0')),
        'cost': str(value.get('cost', '0')),
        'gross_profit': str(value.get('gross_profit', '0')),
        'margin_percent': value.get('margin_percent'),
    }


def _direction_totals(item_metrics):
    result = {}
    for direction in ('all', 'retro', 'oxbridge', 'banquet', 'yandex'):
        totals = {'quantity': Decimal(0), 'revenue': Decimal(0),
                  'cost': Decimal(0), 'gross_profit': Decimal(0)}
        for value in item_metrics.get(direction, {}).values():
            for key in totals:
                totals[key] += _decimal(value.get(key))
        margin = (totals['gross_profit'] / totals['revenue'] * 100
                  if totals['revenue'] else None)
        result[direction] = {
            **{key: str(value) for key, value in totals.items()},
            'margin_percent': str(margin.quantize(Decimal('.01'))) if margin is not None else None,
        }
    return result


def compact_analysis_input(snapshot):
    """Build a bounded fact set so Claude never receives the full menu dump."""
    item_metrics = snapshot.get('item_metrics', {})
    all_items = item_metrics.get('all', {})

    def item_direction(name):
        if name in item_metrics.get('banquet', {}):
            return 'banquet'
        if name in item_metrics.get('oxbridge', {}):
            return 'oxbridge'
        if name in item_metrics.get('yandex', {}) and 'яндекс' in name.casefold():
            return 'yandex'
        return 'retro'

    candidates = [_metric(name, item_direction(name), value)
                  for name, value in all_items.items()]
    losses = sorted(
        (item for item in candidates if _decimal(item['gross_profit']) < 0),
        key=lambda item: (_decimal(item['gross_profit']), -_decimal(item['revenue'])),
    )[:16]
    low_margin = sorted(
        (item for item in candidates
         if _decimal(item['gross_profit']) >= 0
         and item['margin_percent'] is not None
         and _decimal(item['margin_percent']) < 15),
        key=lambda item: -_decimal(item['revenue']),
    )[:6]
    leaders = sorted(
        (item for item in candidates if _decimal(item['gross_profit']) > 0),
        key=lambda item: -_decimal(item['gross_profit']),
    )[:5]
    selected, seen = [], set()
    for item in losses + low_margin + leaders:
        key = item['subject'].casefold()
        if key not in seen:
            selected.append(item)
            seen.add(key)

    negative_waiters = []
    for name, value in snapshot.get('waiter_metrics', {}).items():
        if _decimal(value.get('gross_profit')) < 0:
            negative_waiters.append({
                'name': name,
                'revenue': str(value.get('revenue', '0')),
                'gross_profit': str(value.get('gross_profit', '0')),
                'margin_percent': value.get('margin_percent'),
            })
    negative_waiters.sort(key=lambda item: _decimal(item['gross_profit']))

    return {
        'period_start': snapshot.get('period_start'),
        'period_end': snapshot.get('period_end'),
        'cash_total': snapshot.get('cash_total'),
        'yandex_revenue': snapshot.get('yandex_revenue'),
        'direction_totals': _direction_totals(item_metrics),
        'review_candidates': selected,
        'negative_waiters': negative_waiters[:6],
    }


class ClaudeClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    async def analyze(self, snapshot):
        if not self.settings.claude_configured:
            raise DataError('Настройте CLAUDE_API_KEY и CLAUDE_MODEL для AI-анализа.')
        report_data = compact_analysis_input(snapshot.json())
        prompt = (
            'Верни краткий JSON-анализ отчёта Retro Milliy: резюме и не более '
            f'{MAX_PROBLEMS} неповторяющихся проблем. Пиши сжато, только по фактам из данных.\n'
            '<report_data>\n' + json.dumps(report_data, ensure_ascii=False, separators=(',', ':'))
            + '\n</report_data>'
        )
        headers = {
            'content-type': 'application/json',
            'x-api-key': self.settings.claude_api_key,
            'anthropic-version': ANTHROPIC_VERSION,
        }
        body = {
            'model': self.settings.claude_model,
            'max_tokens': 1200,
            'system': ('Ты аналитик ресторанного бизнеса. Возвращай результат строго по заданной '
                       'JSON-схеме. Не придумывай отсутствующие показатели.'),
            'messages': [{'role': 'user', 'content': prompt}],
            'output_config': {
                'format': {'type': 'json_schema', 'schema': ANALYSIS_SCHEMA},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post(MESSAGES_URL, headers=headers, json=body)
            if not response.is_success:
                log_upstream_failure(
                    'claude', RuntimeError(f'http_status_{response.status_code}'),
                    operation='analyze')
                raise DataError('Claude не смог сформировать анализ. Повторите позже.')
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('invalid Claude response')
            if payload.get('stop_reason') != 'end_turn':
                raise ValueError('incomplete Claude response')
            text_blocks = [block.get('text') for block in payload.get('content', [])
                           if isinstance(block, dict) and block.get('type') == 'text']
            if len(text_blocks) != 1 or not isinstance(text_blocks[0], str):
                raise ValueError('missing Claude text block')
            result = json.loads(text_blocks[0])
            self._validate(result)
            return result
        except DataError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as error:
            log_upstream_failure('claude', error, operation='analyze')
            raise DataError('Claude вернул некорректный ответ. Повторите позже.') from None

    async def chat(self, messages, *, tools=(), tool_handler=None, current_date=None):
        """Answer a founder conversation, optionally using allowlisted read-only tools."""
        if not self.settings.claude_configured:
            raise DataError('Настройте CLAUDE_API_KEY и CLAUDE_MODEL для чата с ИИ.')
        bounded = self._bounded_chat(messages)
        if bool(tools) != bool(tool_handler):
            raise ValueError('chat tools and handler must be configured together')
        headers = {
            'content-type': 'application/json',
            'x-api-key': self.settings.claude_api_key,
            'anthropic-version': ANTHROPIC_VERSION,
        }
        system = (
            'Ты конфиденциальный деловой ассистент учредителей ресторана Retro Milliy. '
            'Отвечай на русском языке ясно, кратко и практически. Отделяй факты от '
            'предположений, не выдумывай цифры. Для любых утверждений о данных ресторана '
            'обязательно используй подходящий серверный инструмент, даже если похожая цифра '
            'встречалась раньше в диалоге. Тебе доступны все read-only данные приложения: '
            'iiko, касса, бухгалтерия, сотрудники, зарплаты, бронирования, посещаемость и '
            'сохранённые отчёты директора. Для нестандартных разрезов продаж обращайся '
            'напрямую к детальному OLAP-инструменту iiko. Указывай период и предупреждения '
            'источника. unavailable и unlinked не означают, что сотрудник отсутствовал; '
            'missing достоверен только при complete=true. Не выполняй действия и не меняй '
            'данные — инструменты работают только на чтение.'
        )
        if current_date is not None:
            system += f' Текущая дата ресторана в Asia/Tashkent: {current_date}.'
        body = {
            'model': self.settings.claude_model,
            'max_tokens': 5600,
            'system': system,
            'messages': bounded,
        }
        if tools:
            body['tools'] = list(tools)
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                tool_calls = 0
                while True:
                    response = await client.post(MESSAGES_URL, headers=headers, json=body)
                    if not response.is_success:
                        log_upstream_failure(
                            'claude', RuntimeError(f'http_status_{response.status_code}'),
                            operation='founder_chat')
                        raise DataError('Claude сейчас не отвечает. Повторите позже.')
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get('content'), list):
                        raise ValueError('invalid Claude chat response')
                    content = payload['content']
                    if payload.get('stop_reason') == 'end_turn':
                        parts = [block.get('text') for block in content
                                 if isinstance(block, dict) and block.get('type') == 'text']
                        if not parts or any(not isinstance(part, str) for part in parts):
                            raise ValueError('missing Claude chat text')
                        answer = '\n'.join(part.strip() for part in parts if part.strip()).strip()
                        if not answer or len(answer) > 12_000:
                            raise ValueError('invalid Claude chat text')
                        return answer
                    if payload.get('stop_reason') != 'tool_use' or not tools:
                        raise ValueError('incomplete Claude chat response')
                    calls = [block for block in content
                             if isinstance(block, dict) and block.get('type') == 'tool_use']
                    if not calls or tool_calls + len(calls) > MAX_CHAT_TOOL_CALLS:
                        raise ValueError('invalid Claude tool calls')
                    results = []
                    for call in calls:
                        call_id, name, arguments = call.get('id'), call.get('name'), call.get('input')
                        if (not isinstance(call_id, str) or not call_id
                                or not isinstance(name, str) or not isinstance(arguments, dict)):
                            raise ValueError('invalid Claude tool call')
                        try:
                            result = await tool_handler(name, arguments)
                            result_text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
                            if len(result_text) > MAX_CHAT_TOOL_RESULT_CHARS:
                                raise DataError('Результат слишком большой. Сузьте период или фильтр.')
                            results.append({'type': 'tool_result', 'tool_use_id': call_id,
                                            'content': result_text})
                        except DataError as error:
                            results.append({'type': 'tool_result', 'tool_use_id': call_id,
                                            'content': str(error), 'is_error': True})
                        tool_calls += 1
                    body['messages'] = [*body['messages'],
                                        {'role': 'assistant', 'content': content},
                                        {'role': 'user', 'content': results}]
        except DataError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as error:
            log_upstream_failure('claude', error, operation='founder_chat')
            raise DataError('Claude вернул некорректный ответ. Повторите позже.') from None

    @staticmethod
    def _bounded_chat(messages):
        if not isinstance(messages, list) or not messages:
            raise ValueError('chat messages must be a non-empty list')
        selected, total = [], 0
        for message in reversed(messages[-24:]):
            if (not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}
                    or not isinstance(message.get('content'), str)):
                raise ValueError('invalid chat message')
            content = message['content'].strip()
            if not content or len(content) > 12_000:
                raise ValueError('invalid chat content')
            if selected and total + len(content) > 20_000:
                break
            selected.append({'role': message['role'], 'content': content})
            total += len(content)
        selected.reverse()
        while selected and selected[0]['role'] == 'assistant':
            selected.pop(0)
        if not selected:
            raise ValueError('chat must include a user message')
        if selected[-1]['role'] != 'user':
            raise ValueError('chat must end with user message')
        return selected

    @staticmethod
    def _validate(result):
        if not isinstance(result, dict) or set(result) != {'summary', 'problems'}:
            raise ValueError('analysis must be an object')
        summary, problems = result.get('summary'), result.get('problems')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            raise ValueError('invalid summary')
        if not isinstance(problems, list) or len(problems) > MAX_PROBLEMS:
            raise ValueError('invalid problems')
        allowed = {
            'direction': {'all', 'retro', 'oxbridge', 'banquet', 'yandex'},
            'priority': {'high', 'medium', 'low'},
            'action': {'remove', 'replace', 'promote', 'review'},
        }
        subjects = set()
        for problem in problems:
            if not isinstance(problem, dict):
                raise ValueError('invalid problem')
            if set(problem) != {'subject', 'direction', 'reason', 'priority', 'action'}:
                raise ValueError('invalid problem fields')
            if any(not isinstance(problem.get(key), str) or not problem[key].strip()
                   for key in ('subject', 'reason')):
                raise ValueError('invalid problem text')
            if len(problem['subject']) > 160 or len(problem['reason']) > 280:
                raise ValueError('problem text is too long')
            if any(problem.get(key) not in values for key, values in allowed.items()):
                raise ValueError('invalid problem enum')
            subject = ' '.join(problem['subject'].casefold().split())
            if subject in subjects:
                raise ValueError('duplicate problem')
            subjects.add(subject)
