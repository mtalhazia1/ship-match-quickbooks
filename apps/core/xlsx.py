"""Writing .xlsx files with XlsxWriter, which writes cells about 2.4 times faster than openpyxl.

openpyxl is still what reads uploaded spreadsheets. Values are written the way openpyxl wrote them
before, so the files look the same: text is always text (never a formula; `csvsafe` decides what
needs neutralising), Decimals are numbers, dates get the formats openpyxl gave them by default.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

DEFAULT_NUMBER_FORMATS = ((datetime, "yyyy-mm-dd h:mm:ss"), (date, "yyyy-mm-dd"), (time, "h:mm:ss"))


class Book:
    """One workbook. `target` is a file name or a binary file object; call close() to finish writing it.

    constant_memory keeps memory flat for big sheets, but then each sheet's rows must be written in order.
    """

    def __init__(self, target, *, constant_memory: bool = False):
        import xlsxwriter

        self.workbook = xlsxwriter.Workbook(target, {
            "constant_memory": constant_memory,
            "strings_to_formulas": False, "strings_to_numbers": False, "strings_to_urls": False,
        })
        self._formats: dict[tuple, object] = {}

    def format(self, **props):
        """A cell format (bold=True, bg_color="#EEF2F7", num_format="#,##0.00", ...), shared when equal."""
        if not props:
            return None
        key = tuple(sorted(props.items()))
        if key not in self._formats:
            self._formats[key] = self.workbook.add_format(props)
        return self._formats[key]

    def sheet(self, name: str):
        return self.workbook.add_worksheet(name[:31])

    def write(self, ws, row: int, col: int, value, **props) -> None:
        """One cell, 0-based. props add formatting; dates and times get a default number format.

        None and "" leave the cell empty (with its formatting, if any), as openpyxl did."""
        if value is None or value == "":
            if props:
                ws.write_blank(row, col, None, self.format(**props))
            return
        if isinstance(value, bool):
            ws.write_boolean(row, col, value, self.format(**props))
        elif isinstance(value, (int, float, Decimal)):
            ws.write_number(row, col, float(value), self.format(**props))
        elif isinstance(value, (datetime, date, time)):
            if "num_format" not in props:
                props["num_format"] = next(f for kind, f in DEFAULT_NUMBER_FORMATS if isinstance(value, kind))
            ws.write_datetime(row, col, value, self.format(**props))
        else:
            ws.write_string(row, col, str(value), self.format(**props))

    def write_row(self, ws, row: int, values, *, formats: dict[int, dict] | None = None, **props) -> None:
        """A row, 0-based. formats maps a column to extra props for that column; props apply to every cell."""
        for col, value in enumerate(values):
            extra = (formats or {}).get(col)
            self.write(ws, row, col, value, **({**props, **extra} if extra else props))

    def close(self) -> None:
        self.workbook.close()
