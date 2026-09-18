import json

import httpx

from retro.modules.cashier.service import DataError


class ClaudeClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    async def analyze(self, snapshot):
        if not self.settings.claude_configured:
            raise DataError('Настройте CLAUDE_API_KEY и CLAUDE_MODEL для AI-анализа.')
        aggregate = snapshot.json()
        prompt = ('Верни только JSON: {"summary": string, "problems": [{"subject": string, '
                  '"direction": "all|retro|oxbridge|yandex", "reason": string, '
                  '"priority": "high|medium|low", "action": "remove|replace|promote|review"}]}. '
                  'До 10 проблем. Рекомендации требуют решения директора. Данные:\n' +
                  json.dumps(aggregate, ensure_ascii=False))
        headers = {'x-api-key': self.settings.claude_api_key, 'anthropic-version': '2023-06-01'}
        body = {'model': self.settings.claude_model, 'max_tokens': 2000,
                'messages': [{'role': 'user', 'content': prompt}]}
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post('https://api.anthropic.com/v1/messages', headers=headers, json=body)
            if not response.is_success:
                raise DataError('Claude не смог сформировать анализ. Повторите позже.')
            text = response.json()['content'][0]['text']
            result = json.loads(text)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise DataError('Claude вернул некорректный ответ. Повторите позже.') from None
        problems = result.get('problems')
        if not isinstance(result.get('summary'), str) or not isinstance(problems, list) or len(problems) > 10:
            raise DataError('Claude вернул некорректный ответ. Повторите позже.')
        allowed = {'direction': {'all', 'retro', 'oxbridge', 'yandex'},
                   'priority': {'high', 'medium', 'low'},
                   'action': {'remove', 'replace', 'promote', 'review'}}
        for problem in problems:
            if not isinstance(problem, dict) or not all(isinstance(problem.get(key), str) for key in ('subject', 'reason')):
                raise DataError('Claude вернул некорректный ответ. Повторите позже.')
            if any(problem.get(key) not in values for key, values in allowed.items()):
                raise DataError('Claude вернул некорректный ответ. Повторите позже.')
        return result
