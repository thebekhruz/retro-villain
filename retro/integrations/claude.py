"""Claude JSON analysis adapter for the director report."""

from __future__ import annotations

import json

import httpx

from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError


MESSAGES_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
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
                    'direction': {'type': 'string', 'enum': ['all', 'retro', 'oxbridge', 'yandex']},
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


class ClaudeClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    async def analyze(self, snapshot):
        if not self.settings.claude_configured:
            raise DataError('Настройте CLAUDE_API_KEY и CLAUDE_MODEL для AI-анализа.')
        prompt = (
            'Проанализируй агрегированные показатели отчёта Retro Milliy. '
            'Дай короткое резюме и до 10 наиболее важных неповторяющихся проблем. '
            'Опирайся только на переданные данные; рекомендации требуют решения директора.\n'
            '<report_data>\n' + json.dumps(snapshot.json(), ensure_ascii=False) + '\n</report_data>'
        )
        headers = {
            'content-type': 'application/json',
            'x-api-key': self.settings.claude_api_key,
            'anthropic-version': ANTHROPIC_VERSION,
        }
        body = {
            'model': self.settings.claude_model,
            'max_tokens': 2400,
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

    @staticmethod
    def _validate(result):
        if not isinstance(result, dict) or set(result) != {'summary', 'problems'}:
            raise ValueError('analysis must be an object')
        summary, problems = result.get('summary'), result.get('problems')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 900:
            raise ValueError('invalid summary')
        if not isinstance(problems, list) or len(problems) > 10:
            raise ValueError('invalid problems')
        allowed = {
            'direction': {'all', 'retro', 'oxbridge', 'yandex'},
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
            if any(problem.get(key) not in values for key, values in allowed.items()):
                raise ValueError('invalid problem enum')
            subject = ' '.join(problem['subject'].casefold().split())
            if subject in subjects:
                raise ValueError('duplicate problem')
            subjects.add(subject)
