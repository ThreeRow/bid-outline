from outline_gen.runtime.tokenization import count_text_tokens, token_spans
"""Readable table variants; native exports stay unchanged for inspection."""

from html import escape
from html.parser import HTMLParser
from dataclasses import replace
import re
from .models import Table, CellPart


class CleanHTML(HTMLParser):
    allowed = {
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "p",
        "br",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "caption",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in self.allowed:
            attr = "".join(
                f' {k}="{escape(v,quote=True)}"'
                for k, v in attrs
                if k in {"rowspan", "colspan"} and v and v.isdigit()
            )
            self.parts.append(f"<{tag}{attr}>")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.allowed and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(escape(data))


def clean_html(value):
    parser = CleanHTML()
    parser.feed(value)
    return "".join(parser.parts)


def compact_md(value):
    result = []
    for line in value.splitlines():
        if line.lstrip().startswith("|"):
            cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
            cells = [
                ("---" if re.fullmatch(r":?-{2,}:?", c.strip()) else c.strip())
                for c in cells
            ]
            line = "| " + " | ".join(cells) + " |"
        result.append(line)
    return "\n".join(result)


def cell_content(cell, fmt):
    if not cell.parts:
        if fmt == "html":
            return "<br>".join(escape(p) for p in cell.paragraphs)
        return "\n".join(cell.paragraphs)
    pieces = []
    for part in cell.parts:
        if part.kind == "text":
            pieces.append(escape(part.text) if fmt == "html" else part.text)
        elif part.table is not None:
            nested = render_table(part.table, fmt, native=False)
            pieces.append(
                nested
                if fmt == "html"
                else "\n[单元格内表格]\n" + nested + "\n[单元格内表格结束]\n"
            )
    return ("<br>" if fmt == "html" else "\n").join(pieces)


def render_cells(cells):
    result = []
    for c in cells:
        label = f"第{c.row+1}行，第{c.col+1}列"
        if c.row_span > 1:
            label += f"，跨至第{c.row+c.row_span}行"
        if c.col_span > 1:
            label += f"，跨至第{c.col+c.col_span}列"
        if c.column_header:
            label += "，列标题"
        if c.row_header:
            label += "，行标题"
        result.append(label + "：\n" + cell_content(c, "text"))
    return "\n\n".join(result)


def generated_html(table):
    rows = []
    for r in sorted({c.row for c in table.cells}):
        cells = []
        for c in table.cells:
            if c.row != r:
                continue
            tag = "th" if c.column_header or c.row_header else "td"
            attrs = (f' rowspan="{c.row_span}"' if c.row_span > 1 else "") + (
                f' colspan="{c.col_span}"' if c.col_span > 1 else ""
            )
            cells.append(f"<{tag}{attrs}>" + cell_content(c, "html") + f"</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def render_table(table, fmt, *, native=True):
    nested = any(any(p.kind == "table" for p in c.parts) for c in table.cells)
    if fmt == "text":
        return render_cells(table.cells)
    if fmt == "html":
        return (
            clean_html(table.html)
            if native and table.html and not table.modified and not nested
            else generated_html(table)
        )
    if fmt != "md":
        raise ValueError("未知表格输入形式")
    if native and table.markdown and not table.modified and not nested:
        result = compact_md(table.markdown)
    elif nested:
        # Markdown has no reliable nested-table syntax; cell boundaries remain
        # explicit while inner tables retain their own row/column structure.
        result = render_cells(table.cells)
    else:
        grid = {r: [""] * table.cols for r in sorted({c.row for c in table.cells})}
        for c in table.cells:
            grid[c.row][c.col] = (
                cell_content(c, "md").replace("|", "&#124;").replace("\n", "<br>")
            )
        result = "\n".join("| " + " | ".join(row) + " |" for row in grid.values())
    spans = []
    for c in table.cells:
        if c.row_span > 1 or c.col_span > 1:
            spans.append(
                f"第{c.row+1}行第{c.col+1}列覆盖至第{c.row+c.row_span}行第{c.col+c.col_span}列"
            )
    if spans:
        result += "\n合并范围：" + "；".join(spans)
    return result


def chunks(table, limit, fmt="text"):
    measure = count_text_tokens
    """Budget actual rendered text, including repeated headers/parent cells."""
    if measure(render_table(table, fmt)) <= limit:
        yield table.cells, []
        return
    units = []
    for c in table.cells:
        if measure(render_cells([c])) <= limit // 2:
            units.append(c)
            continue
        parts = c.parts or [CellPart("text", c.text)]
        for part in parts:
            if part.kind == "table":
                for scope, context in chunks(part.table, max(20, limit // 2), fmt):
                    inner = replace(
                        part.table,
                        cells=context + scope,
                        markdown="",
                        html="",
                        modified=True,
                    )
                    units.append(
                        replace(
                            c, paragraphs=[], parts=[CellPart("table", table=inner)]
                        )
                    )
            else:
                width = max(1, limit // 2 - 100)
                spans = token_spans(part.text, width)
                for start, end in spans:
                    units.append(
                        replace(
                            c, paragraphs=[part.text[start:end]], parts=[]
                        )
                    )

    def repeated(scope):
        row = min(c.row for c in scope)
        ids = {c.id for c in scope}
        return [
            c
            for c in table.cells
            if c.id not in ids
            and (
                c.column_header
                or c.row == 0
                or c.row < row < c.row + c.row_span
                or (c.row == row and c.col < min(x.col for x in scope))
            )
        ]

    def cost(scope):
        fragment = replace(table, cells=scope, markdown="", html="", modified=True)
        return measure(render_table(fragment, fmt, native=False) + "\n" + render_cells(repeated(scope)))

    scope = []
    for c in units:
        if scope and cost(scope + [c]) > limit:
            yield scope, repeated(scope)
            scope = []
        scope.append(c)
    if scope:
        yield scope, repeated(scope)
