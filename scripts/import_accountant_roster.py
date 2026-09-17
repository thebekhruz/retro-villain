"""Import the private payroll roster into the local demo database."""

import argparse

from retro.config import ROOT
from retro.modules.accountant.roster import RosterStore


def main():
    parser = argparse.ArgumentParser(description='Import only the ЗП sheet into local accountant demo data.')
    parser.add_argument('xlsx', help='Path to the private зп.xlsx file')
    parser.add_argument('--db', default=str(ROOT / 'build' / 'accountant-demo.sqlite3'))
    args = parser.parse_args()
    store = RosterStore(args.db)
    result = store.import_xlsx(args.xlsx)
    people = store.list()
    print(f'Imported: {result["imported"]}; existing: {result["existing"]}; '
          f'people: {len(people)}; without rate: {sum(p.rate is None for p in people)}')


if __name__ == '__main__':
    main()
