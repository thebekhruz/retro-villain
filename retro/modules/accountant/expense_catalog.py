"""Cash-outflow names transcribed from the April 2026 Retro finance workbook.

Source: РЕТРО_Лина апрель 2026.xls, sheet «Категории», columns B:H.
Income and discount columns are intentionally excluded from cash expenses.
"""

# Доп. зарплата и выплаты временному персоналу (ТЗ 09.10, Б-08): люди без
# начисления в ведомости — разовая хостес, подмена на смену. Это зарплата: в
# сводках она идёт строкой зарплат (CashBook.flow → salary), а не в «прочих».
# Окладом она не считается (reserves.is_monthly_salary) и долг смены не гасит.
EXTRA_SALARY_ITEM = 'salary_extra'
# Дивиденды собственнику прямо из кассы. Отложенные в сейф — резерв `dividends`
# (reserves.py), выданные из сейфа деньги кассы не трогают.
CASH_DIVIDENDS_ITEM = 'distribution_dividends'

GROUPS = (
    ('income', 'Приходы', (
        ('income_opening', 'Остаток на начало дня'),
        ('income_cashier', 'Касса'),
        ('income_other', 'Прочие поступления'),
    )),
    ('salary', 'Заработная плата', (
        ('salary_cashier', 'Кассир'),
        ('salary_staff', 'ЗП персонал'),
        ('salary_technical', 'Тех персонал'),
        (EXTRA_SALARY_ITEM, 'Доп. зарплата и временный персонал'),
        ('salary_carryover', 'ЗП за прошлый месяц'),
        ('salary_monthly', 'Месячная заработная плата'),
    )),
    ('administrative', 'Общие и административные', (
        ('admin_internet', 'Интернет и корпоративная связь'),
        ('admin_music', 'Музыкальное оформление (зарплата певцов)'),
        ('admin_it', 'Обслуживание техники'),
        ('admin_fine', 'Штраф ККМ'),
        ('admin_legal', 'Подписки, юрист, кадастр, инкассация'),
        ('admin_design', 'Дизайн интерьера'),
        ('admin_other', 'Прочие расходы'),
    )),
    ('operations', 'Операционные расходы', (
        ('ops_rent', 'Аренда помещения'),
        ('ops_repair', 'Ремонт и строительство'),
        ('ops_transport', 'ГСМ и дорожные расходы'),
        ('ops_equipment', 'Инвентарь и оборудование'),
        ('ops_disinfection', 'Дезинфекция'),
    )),
    ('marketing', 'Маркетинг и реклама', (
        ('marketing_video', 'Видеоролик и аренда камеры'),
        ('marketing_smm', 'SMM и зарплата'),
        ('marketing_print', 'Визитки, меню и другие материалы'),
    )),
    ('utilities', 'Коммунальные и охрана', (
        ('utilities', 'Газ, свет, вода, мусор, охрана'),
        ('national_guard', 'Национальная гвардия'),
    )),
    ('procurement', 'Закуп', (
        ('proc_shoh', 'Шох'),
        ('proc_meat', 'Казы и рулеты'),
        ('proc_coal', 'Уголь'),
        ('proc_mushrooms', 'Грибы'),
        ('proc_household', 'Хозяйственные товары'),
        ('proc_other', 'Прочий закуп'),
        ('proc_dough', 'Тесто'),
        ('proc_khasip', 'Хасип'),
        ('proc_bread', 'Хлеб'),
        ('proc_cleaners', 'Моющие средства'),
    )),
    ('distributions', 'Дивиденды и переводы', (
        (CASH_DIVIDENDS_ITEM, 'Дивиденды напрямую из кассы (не из сейфа)'),
        ('distribution_oxbridge', 'Перевод Oxbridge'),
        ('distribution_shakiraka', 'Шакирака (чеки, обеды)'),
        ('distribution_bloggers', 'Блогеры (чеки, обеды)'),
        ('distribution_oxbridge_other', 'Перевод Oxbridge 2'),
    )),
)

ITEMS = {code: (group_code, label) for group_code, _, items in GROUPS for code, label in items}

# Доп. выплата сменному из «Зарплата · день» (ТЗ 09.10, Б-05). Статья служебная:
# в списке журнала её нет — без сотрудника и даты смены такую выплату не записать.
# Код отдельный от статьи журнала «Доп. зарплата и временный персонал»
# (EXTRA_SALARY_ITEM): ту бухгалтер пишет сам, без сотрудника, и правит в журнале.
EXTRA_ITEM = 'salary_extra_payout'
ITEMS[EXTRA_ITEM] = ('salary', 'Доп. выплаты')
# Обе статьи — зарплата сменным: в сводках строкой зарплат, не в «прочих».
EXTRA_SALARY_CODES = frozenset({EXTRA_SALARY_ITEM, EXTRA_ITEM})


def catalog_json():
    return {'groups': [
        {'code': group_code, 'label': group_label,
         'items': [{'code': code, 'label': label} for code, label in items]}
        for group_code, group_label, items in GROUPS
    ]}
