from decimal import Decimal

from openpyxl import Workbook

from retro.modules.accountant.roster import RosterStore


def make_roster(path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'ЗП'
    sheet.append(['Номер', 'ФИО', 'Должность', 'Ставка'])
    for row, name, role, rate in [
        (5, 'Тест Повар', 'повар миллий', 250000),
        (6, 'Тест Уборка', 'техперсонал', None),
        (7, None, 'повар', 120000),
        (8, 'Тест Официант', 'официант', 170000),
    ]:
        sheet.cell(row, 1, row - 4)
        sheet.cell(row, 2, name)
        sheet.cell(row, 3, role)
        sheet.cell(row, 4, rate)
    sheet['D9'] = '=SUM(D5:D8)'
    book.save(path)


def test_import_only_named_people_preserves_blank_rates_and_source_rows(tmp_path):
    source = tmp_path / 'roster.xlsx'
    make_roster(source)
    store = RosterStore(tmp_path / 'demo.sqlite3')

    assert store.import_xlsx(source) == {'imported': 3, 'existing': 0}
    people = store.list()
    assert [(p.source_row, p.group_name, p.rate) for p in people] == [
        (5, 'Кухня', Decimal('250000')),
        (6, 'Уборка', None),
        (8, 'Обслуживание зала', Decimal('170000')),
    ]
    assert all(p.hikvision_id is None for p in people)


def test_reimport_does_not_overwrite_corrected_rate(tmp_path):
    source = tmp_path / 'roster.xlsx'
    make_roster(source)
    store = RosterStore(tmp_path / 'demo.sqlite3')
    store.import_xlsx(source)
    person = store.list()[0]
    store.update(person.id, rate='260000', group_name='Кухня', reason='Исправлена ставка')

    assert store.import_xlsx(source) == {'imported': 0, 'existing': 3}
    assert RosterStore(tmp_path / 'demo.sqlite3').list()[0].rate == Decimal('260000')
