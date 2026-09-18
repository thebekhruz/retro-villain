from datetime import datetime

from retro.modules.cashier.service import TZ
from .pdf import render_report_pdf


class DirectorService:
    def __init__(self, iiko, claude, store):
        self.iiko, self.claude, self.store = iiko, claude, store

    async def generate(self, today):
        snapshot = await self.iiko.load_director_report(today)
        analysis = await self.claude.analyze(snapshot)
        snapshot_json = snapshot.json()
        pdf = render_report_pdf(snapshot_json, analysis)
        report_id = self.store.create(snapshot_json, analysis, pdf, datetime.now(TZ).isoformat())
        return self.store.get(report_id)
