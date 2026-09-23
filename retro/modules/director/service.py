import asyncio
from datetime import datetime

from retro.modules.cashier.service import TZ
from .pdf import render_report_pdf


class DirectorService:
    def __init__(self, iiko, claude, store, retention=24, *, loader=None):
        self.iiko, self.claude, self.store, self.retention = iiko, claude, store, retention
        self.loader = loader or iiko.load_director_report

    async def generate(self, today):
        snapshot = await self.loader(today)
        snapshot_json = snapshot.json()
        existing = await asyncio.to_thread(self.store.get_for_period,
            snapshot_json['period_start'], snapshot_json['period_end'])
        if existing is not None and existing['snapshot'] == snapshot_json:
            analysis = existing['analysis']
        else:
            analysis = await self.claude.analyze(snapshot)
        pdf = await asyncio.to_thread(render_report_pdf, snapshot_json, analysis)
        report_id = await asyncio.to_thread(self.store.create_or_replace,
            snapshot_json, analysis, pdf, datetime.now(TZ).isoformat(), self.retention)
        return await asyncio.to_thread(self.store.get, report_id)
