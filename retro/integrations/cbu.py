"""Persisted historical USD rates from the Central Bank of Uzbekistan."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

import httpx

from retro.modules.cashier.service import DataError
from retro.logging_config import log_upstream_failure
from retro.db import as_database
from retro.runtime import secure_directory, secure_file

CBU_ORIGIN = 'https://cbu.uz'
# Независимый рыночный курс для сверки курса, присланного из браузера (T-526):
# cbu.uz не отвечает зарубежным дата-центрам (Railway), а браузер кассира —
# в Узбекистане. Кассир не должен подменить курс: расхождение с рынком
# обычно ~0,3%, порог 3% отсекает любую выгодную подмену.
REFERENCE_ORIGIN = 'https://open.er-api.com'
REFERENCE_PATH = '/v6/latest/USD'
REFERENCE_TOLERANCE = Decimal('0.03')
DISCOUNT = Decimal('0.015')


def round_restaurant_rate(value: Decimal) -> Decimal:
    return (value.to_integral_value(rounding=ROUND_DOWN) // Decimal('100')) * Decimal('100')


@dataclass(frozen=True)
class UsdRate:
    day: date
    source_date: date
    official_rate: Decimal
    restaurant_rate: Decimal

    def json(self):
        return dict(date=self.day.isoformat(), source_date=self.source_date.isoformat(),
                    official_rate=str(self.official_rate),
                    restaurant_rate=str(self.restaurant_rate), discount_percent='1.5')


class UsdRates:
    def __init__(self, path: Path, *, transport=None):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        self.transport = transport
        self.lock = asyncio.Lock()
        self._initialize()

    def _open(self):
        return self.db.connect()

    def _initialize(self):
        with closing(self._open()) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_usd_rates (
                day TEXT PRIMARY KEY,
                source_date TEXT NOT NULL,
                official_rate TEXT NOT NULL,
                restaurant_rate TEXT NOT NULL
            )''')
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_usd_balances (
                day TEXT PRIMARY KEY, amount TEXT NOT NULL
            )''')

    def balance(self, day: date):
        with closing(self._open()) as connection:
            row = connection.execute('SELECT amount FROM cashier_usd_balances WHERE day = ?',
                                     (day.isoformat(),)).fetchone()
        return {'date': day.isoformat(), 'amount': row[0] if row else None}

    def save_balance(self, day: date, amount):
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise DataError('Укажите корректное количество USD.') from None
        if not value.is_finite() or value < 0 or value > Decimal('1000000000') or value.as_tuple().exponent < -2:
            raise DataError('Количество USD должно быть от 0 до 1 млрд, не более двух знаков.')
        with closing(self._open()) as connection, connection:
            connection.execute('INSERT INTO cashier_usd_balances(day, amount) VALUES (?, ?) '
                               'ON CONFLICT(day) DO UPDATE SET amount=excluded.amount',
                               (day.isoformat(), str(value)))
        return {'date': day.isoformat(), 'amount': str(value)}

    def stored(self, day: date):
        """Persisted rate for this day, without an external request."""
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT source_date, official_rate, restaurant_rate FROM cashier_usd_rates WHERE day = ?',
                (day.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return UsdRate(day, date.fromisoformat(row[0]), Decimal(row[1]), round_restaurant_rate(Decimal(row[2])))

    def _save(self, rate: UsdRate):
        with closing(self._open()) as connection:
            with connection:
                connection.execute('''INSERT OR IGNORE INTO cashier_usd_rates
                    (day, source_date, official_rate, restaurant_rate) VALUES (?, ?, ?, ?)''',
                    (rate.day.isoformat(), rate.source_date.isoformat(),
                     str(rate.official_rate), str(rate.restaurant_rate)))
        return self.stored(rate.day)

    async def get(self, day: date):
        saved = await asyncio.to_thread(self.stored, day)
        if saved is not None:
            return saved
        async with self.lock:
            saved = await asyncio.to_thread(self.stored, day)
            if saved is not None:
                return saved
            rate = await self._fetch(day)
            return await asyncio.to_thread(self._save, rate)

    async def submit(self, day: date, payload):
        """Курс, который браузер кассира получил у ЦБ сам (T-526).

        Сервер не доверяет ему вслепую: разбирает теми же проверками, что и
        свой запрос, и сверяет с независимым рыночным курсом. Сохранённый
        ранее курс за день не перезаписывается.
        """
        saved = await asyncio.to_thread(self.stored, day)
        if saved is not None:
            return saved
        rate = self._parse(day, payload)
        reference = await self._reference()
        deviation = abs(rate.official_rate - reference) / reference
        if deviation > REFERENCE_TOLERANCE:
            log_upstream_failure('cbu', ValueError(
                f'submitted {rate.official_rate} vs reference {reference}'), operation='submit_usd_rate')
            raise DataError('Присланный курс USD расходится с рыночным. Взнос не сохранён.')
        async with self.lock:
            saved = await asyncio.to_thread(self.stored, day)
            if saved is not None:
                return saved
            return await asyncio.to_thread(self._save, rate)

    async def _reference(self) -> Decimal:
        try:
            async with httpx.AsyncClient(base_url=REFERENCE_ORIGIN, timeout=10,
                                         follow_redirects=False,
                                         transport=self.transport) as client:
                response = await client.get(REFERENCE_PATH)
                response.raise_for_status()
                payload = response.json()
            value = Decimal(str(payload['rates']['UZS']))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as error:
            log_upstream_failure('usd-reference', error, operation='load_reference_rate')
            raise DataError('Не удалось сверить курс USD с рыночным. Повторите позже.') from None
        if not value.is_finite() or value <= 0:
            raise DataError('Не удалось сверить курс USD с рыночным. Повторите позже.')
        return value

    async def _fetch(self, day: date):
        try:
            async with httpx.AsyncClient(base_url=CBU_ORIGIN, timeout=10,
                                         follow_redirects=False,
                                         transport=self.transport) as client:
                response = await client.get(f'/ru/arkhiv-kursov-valyut/json/USD/{day.isoformat()}/')
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log_upstream_failure('cbu', error, operation='load_usd_rate')
            raise DataError('Не удалось получить курс USD от Центрального банка.') from None
        return self._parse(day, payload)

    @staticmethod
    def _parse(day: date, payload) -> UsdRate:
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise DataError('Центральный банк вернул некорректный курс USD.')
        item = payload[0]
        try:
            official = Decimal(str(item['Rate']))
            nominal = Decimal(str(item['Nominal']))
            source_date = datetime.strptime(item['Date'], '%d.%m.%Y').date()
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise DataError('Центральный банк вернул некорректный курс USD.') from None
        if (item.get('Ccy') != 'USD' or nominal != 1 or not official.is_finite()
                or official <= 0 or source_date > day):
            raise DataError('Центральный банк вернул некорректный курс USD.')
        discounted = (official * (1 - DISCOUNT)).to_integral_value(rounding=ROUND_DOWN)
        restaurant = round_restaurant_rate(discounted)
        return UsdRate(day, source_date, official, restaurant)
