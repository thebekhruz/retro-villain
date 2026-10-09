"""Одно чтение на запрос: повтор того же запроса к базе внутри одного ответа.

Неделя учредителя собирает до семи дней бухгалтера сразу, и каждый день
заново читал реестр окладников, записи резерва дивидендов и движения за шесть
недель — одними и теми же запросами с одними и теми же границами. Экран дня
бухгалтера делает то же в меньшем масштабе: `list_monthly()` читается четырежды
за один ответ.

Кэш живёт ровно один запрос и только для безопасных методов (GET/HEAD): запись
никогда не читает из него, поэтому устареть ему негде. Значение считается один
раз даже тогда, когда его одновременно просят из разных потоков — семь дней
недели собираются в `asyncio.to_thread` и без этого промахнулись бы все разом.

Оборачивать стоит только «листовые» чтения: внутри `compute` держится замок
того же ключа, поэтому чтение, которое вызывает само себя с теми же
аргументами, в кэш заворачивать нельзя.
"""

from contextvars import ContextVar
from functools import wraps
from threading import Lock


_reads = ContextVar('request_reads', default=None)


class _Slot:
    __slots__ = ('lock', 'ready', 'value')

    def __init__(self):
        self.lock = Lock()
        self.ready = False
        self.value = None


class ReadCache:
    def __init__(self):
        self._guard = Lock()
        self._slots = {}

    def get(self, key, compute):
        with self._guard:
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot()
        with slot.lock:
            # Сбой чтения не кэшируется: следующий вызов попробует снова.
            if not slot.ready:
                slot.value = compute()
                slot.ready = True
            return slot.value

    def __len__(self):
        return len(self._slots)


def begin():
    """Включить кэш на текущий запрос. Возвращает токен для `end`."""
    return _reads.set(ReadCache())


def end(token):
    _reads.reset(token)


def active():
    return _reads.get()


def call(key, compute):
    """Чтение по готовому ключу — для функций без хранилища."""
    cache = _reads.get()
    return compute() if cache is None else cache.get(key, compute)


def once(method=None, *, key=None):
    """Чтение, которое за один безопасный запрос выполняется один раз.

    По умолчанию ключ — сам метод, хранилище и аргументы. `key` нужен там, где
    в аргументах лежит открытое соединение: оно у каждого вызова своё, и по
    нему два одинаковых чтения никогда не совпали бы.
    """

    def decorate(method):
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            cache = _reads.get()
            if cache is None:
                return method(self, *args, **kwargs)
            extra = key(*args, **kwargs) if key else (args, tuple(sorted(kwargs.items())))
            return cache.get((method.__qualname__, id(self), extra),
                             lambda: method(self, *args, **kwargs))

        return wrapper

    return decorate(method) if method is not None else decorate
