"""Minimal wikitext table reader.

Only what the AVN ceremony articles need: split a ``{| ... |}`` table into rows,
and each row into cells, keeping header cells distinguishable from data cells.
Full wikitext parsing is out of scope -- this exists so award categories can be
mapped to their entries positionally, which a flat regex cannot do.
"""

import re

_ROW = re.compile(r"^\|-")
_END = re.compile(r"^\|\}")
_START = re.compile(r"^\{\|")

# A cell attribute prefix: ``style="..." |`` before the content. The separator
# is a single pipe; ``||`` starts the next cell instead, so it must not match.
_ATTRS = re.compile(r'^\s*[^|\n]*?=\s*"[^"]*"[^|\n]*\|(?!\|)')


class Cell:
    __slots__ = ("header", "lines")

    def __init__(self, header: bool):
        self.header = header
        self.lines: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "H" if self.header else "D"
        return f"<Cell {kind} {self.text[:40]!r}>"


def _strip_attrs(fragment: str) -> str:
    """Drop a leading ``style=...|`` attribute block from cell content."""

    return _ATTRS.sub("", fragment, count=1).strip()


def _split_inline(fragment: str, sep: str) -> list[str]:
    """Split a line on ``||`` / ``!!`` inline cell separators."""

    return fragment.split(sep) if sep in fragment else [fragment]


def iter_tables(wikitext: str):
    """Yield each top-level table as a list of rows, each row a list of Cells.

    Nested tables are folded into their parent cell rather than yielded
    separately; the AVN articles do not nest, and treating a nested ``{|`` as a
    new table would silently split a category away from its entries.
    """

    lines = wikitext.splitlines()
    depth = 0
    rows: list[list[Cell]] = []
    cur: list[Cell] = []
    cell: Cell | None = None

    def close_row():
        nonlocal cur, cell
        if cell is not None:
            cur.append(cell)
            cell = None
        if cur:
            rows.append(cur)
        cur = []

    for raw in lines:
        line = raw.rstrip()

        if _START.match(line.strip()):
            depth += 1
            if depth == 1:
                rows, cur, cell = [], [], None
                continue

        if depth == 0:
            continue

        if _END.match(line.strip()):
            depth -= 1
            if depth == 0:
                close_row()
                if rows:
                    yield rows
                rows = []
            continue

        if depth > 1:
            # Inside a nested table: keep the text with the enclosing cell.
            if cell is not None:
                cell.lines.append(line)
            continue

        stripped = line.strip()

        if _ROW.match(stripped):
            close_row()
            continue

        if stripped.startswith("!"):
            for part in _split_inline(stripped[1:], "!!"):
                if cell is not None:
                    cur.append(cell)
                cell = Cell(header=True)
                cell.lines.append(_strip_attrs(part))
            continue

        if stripped.startswith("|"):
            for part in _split_inline(stripped[1:], "||"):
                if cell is not None:
                    cur.append(cell)
                cell = Cell(header=False)
                cell.lines.append(_strip_attrs(part))
            continue

        if cell is not None:
            cell.lines.append(line)

    close_row()
    if rows:
        yield rows
