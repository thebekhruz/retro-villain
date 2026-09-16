"""Import a user-provided layout without retaining their example transactions."""
import argparse
from pathlib import Path

import openpyxl
from openpyxl.cell.cell import MergedCell


def prepare(source, destination):
    if source.resolve() == destination.resolve():
        raise ValueError('Original workbook must never be overwritten.')
    workbook = openpyxl.load_workbook(source)
    sheet = workbook['отчет']
    for region in ['A4:B19', 'D2:D11', 'C13:D19', 'C22:D37', 'C40:D55',
                   'B20:B23', 'D20:D20', 'D38:D39']:
        for row in sheet[region]:
            for cell in row:
                if not isinstance(cell, MergedCell):
                    cell.value = None
    sheet['A1'] = None
    sheet['B2'] = None
    for row in workbook['Касса']:
        for cell in row:
            if not isinstance(cell, MergedCell):
                cell.value = None
    workbook.properties.creator = 'Retro Milliy'
    workbook.properties.lastModifiedBy = 'Retro Milliy'
    workbook.save(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    prepare(args.source, args.destination)
