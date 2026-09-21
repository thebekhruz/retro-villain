from datetime import datetime

from retro.modules.cashier.service import TZ
from .pdf import render_report_pdf


class DirectorService:
    def __init__(self, iiko, claude, store, retention=24):
        self.iiko, self.claude, self.store, self.retention = iiko, claude, store, retention

    async def generate(self, today):
        snapshot = await self.iiko.load_director_report(today)
        snapshot_json = snapshot.json()
        existing = self.store.get_for_period(
            snapshot_json['period_start'], snapshot_json['period_end'])
        if existing is not None and existing['snapshot'] == snapshot_json:
            analysis = existing['analysis']
        else:
            analysis = await self.claude.analyze(snapshot)
        pdf = render_report_pdf(snapshot_json, analysis)
        report_id = self.store.create_or_replace(
            snapshot_json, analysis, pdf, datetime.now(TZ).isoformat(), self.retention)
        return self.store.get(report_id)
