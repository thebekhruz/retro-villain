"""Backfill a chosen historical range, sequentially, without changing iiko.

python -m scripts.archive_cashier_days --start 2026-09-01 --end 2026-09-27
Uses the application's IIKO_* and DATABASE_URL/RETRO_DATA_DIR settings.
"""
import argparse
import asyncio
from datetime import date, timedelta

from retro.config import Settings
from retro.db import Database
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.archive import CashierArchive
from retro.modules.cashier.days import CashierDays
from retro.modules.cashier.service import today_tashkent


async def backfill(settings, start, end, *, refresh=False):
    source = IikoClient(settings)
    archive = CashierArchive(Database(settings.database_url or settings.data_dir / 'cashier.sqlite3'), settings)
    days = CashierDays(archive, source.load)
    try:
        day = start
        while day <= end:
            await days.get(day, refresh=refresh)
            print(f'{day.isoformat()}: сохранено', flush=True)
            day += timedelta(days=1)
            if day <= end:
                await asyncio.sleep(1)
    finally:
        await days.close()
        await source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    parser.add_argument('--refresh', action='store_true', help='Перечитать уже сохранённые дни из iiko')
    args = parser.parse_args()
    if args.start > args.end or args.end >= today_tashkent():
        parser.error('Нужен диапазон завершившихся дат: start <= end < сегодня.')
    settings = Settings.from_env()
    if not settings.configured:
        parser.error('Подключение iiko не настроено.')
    asyncio.run(backfill(settings, args.start, args.end, refresh=args.refresh))


if __name__ == '__main__':
    main()
