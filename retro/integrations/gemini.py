"""Gemini JSON analysis adapter for the director report."""

import json

import httpx

from retro.modules.cashier.service import DataError


class GeminiClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    async def analyze(self, snapshot):
        if not self.settings.gemini_configured:
            raise DataError('Настройте GEMINI_API_KEY и GEMINI_MODEL для AI-анализа.')
        prompt = ('Верни только JSON без markdown: {"summary": string, "problems": [{"subject": string, '
                  '"direction": "all|retro|oxbridge|yandex", "reason": string, '
                  '"priority": "high|medium|low", "action": "remove|replace|promote|review"}]}. '
                  'До 10 проблем. Рекомендации требуют решения директора. Данные отчёта:\n' +
                  json.dumps(snapshot.json(), ensure_ascii=False))
        body = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2},
        }
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:generateContent'
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post(url, params={'key': self.settings.gemini_api_key}, json=body)
            if not response.is_success:
                raise DataError('Gemini не смог сформировать анализ. Повторите позже.')
            text = response.json()['candidates'][0]['content']['parts'][0]['text']
            result = json.loads(text)
        except DataError:
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            raise DataError('Gemini вернул некорректный ответ. Повторите позже.') from None
        problems = result.get('problems') if isinstance(result, dict) else None
        if not isinstance(result.get('summary') if isinstance(result, dict) else None, str) or not isinstance(problems, list) or len(problems) > 10:
            raise DataError('Gemini вернул некорректный ответ. Повторите позже.')
        allowed = {'direction': {'all', 'retro', 'oxbridge', 'yandex'},
                   'priority': {'high', 'medium', 'low'},
                   'action': {'remove', 'replace', 'promote', 'review'}}
        for problem in problems:
            if not isinstance(problem, dict) or not all(isinstance(problem.get(key), str) for key in ('subject', 'reason')):
                raise DataError('Gemini вернул некорректный ответ. Повторите позже.')
            if any(problem.get(key) not in values for key, values in allowed.items()):
                raise DataError('Gemini вернул некорректный ответ. Повторите позже.')
        return result
