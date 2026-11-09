"""Read Word positions, inherited styles and numbering without flattening tables."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from .models import Cell, CellPart, Table, WordElement

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def val(node, path, default=None):
    child = node.find(path, NS) if node is not None else None
    return child.get(f"{{{W}}}val", default) if child is not None else default


def text_of(node):
    parts = []
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1]
        if local == "t":
            parts.append(child.text or "")
        elif local in {"br", "cr"}:
            parts.append("\n")
        elif local == "tab":
            parts.append("\t")
        elif local == "sym":
            parts.append("[未识别符号]")
    return "".join(parts).strip()


class Numbering:
    def __init__(self, root):
        self.abstract = {
            val_: el
            for el in ([] if root is None else root.findall("w:abstractNum", NS))
            if (val_ := el.get(f"{{{W}}}abstractNumId")) is not None
        }
        self.instances = {
            el.get(f"{{{W}}}numId"): el
            for el in ([] if root is None else root.findall("w:num", NS))
        }
        self.counts = {}
        self.warnings = []

    def label(self, num_id, level):
        instance = self.instances.get(num_id)
        abstract = self.abstract.get(val(instance, "w:abstractNumId"))
        if abstract is None:
            return None
        levels = {
            int(el.get(f"{{{W}}}ilvl", "0")): el for el in abstract.findall("w:lvl", NS)
        }
        override = (
            None
            if instance is None
            else instance.find(f"w:lvlOverride[@w:ilvl='{level}']", NS)
        )
        if override is not None and override.find("w:lvl", NS) is not None:
            levels[level] = override.find("w:lvl", NS)
        rule = levels.get(level)
        if rule is None:
            return None
        start = int(val(override, "w:startOverride", val(rule, "w:start", "1")))
        counts = self.counts.setdefault(num_id, {})
        counts[level] = counts.get(level, start - 1) + 1
        for deeper in list(counts):
            if deeper > level:
                restart = val(levels.get(deeper), "w:lvlRestart")
                if restart != "0" and (restart is None or level <= int(restart) - 1):
                    counts.pop(deeper)
        result = val(rule, "w:lvlText", "")
        import re

        def replace(match):
            index = int(match.group(1)) - 1
            fmt = val(levels.get(index), "w:numFmt", "decimal")
            value = counts.get(index, int(val(levels.get(index), "w:start", "1")))
            return format_number(value, fmt, self.warnings)

        return re.sub(r"%([1-9])", replace, result)


def format_number(value, fmt, warnings):
    if fmt in {"decimal", "decimalZero"}:
        return str(value).zfill(2) if fmt == "decimalZero" else str(value)
    if fmt in {"upperLetter", "lowerLetter"}:
        chars, number = "", value
        while number:
            number, rem = divmod(number - 1, 26)
            chars = chr(65 + rem) + chars
        return chars.lower() if fmt == "lowerLetter" else chars
    if fmt in {"upperRoman", "lowerRoman"}:
        result, number = "", value
        for count, token in [
            (1000, "M"),
            (900, "CM"),
            (500, "D"),
            (400, "CD"),
            (100, "C"),
            (90, "XC"),
            (50, "L"),
            (40, "XL"),
            (10, "X"),
            (9, "IX"),
            (5, "V"),
            (4, "IV"),
            (1, "I"),
        ]:
            while number >= count:
                result, number = result + token, number - count
        return result.lower() if fmt == "lowerRoman" else result
    if (
        fmt in {"chineseCounting", "chineseCountingThousand", "ideographTraditional"}
        and 0 < value < 100
    ):
        digits = "零一二三四五六七八九"
        return (
            digits[value]
            if value < 10
            else (digits[value // 10] if value >= 20 else "")
            + "十"
            + (digits[value % 10] if value % 10 else "")
        )
    warnings.append(
        {"code": "unsupported_number_format", "format": fmt, "value": value}
    )
    return f"[编号{value}]"


def read_word_structure(path: Path):
    with ZipFile(path) as archive:

        def read(name):
            return (
                ET.fromstring(archive.read(name))
                if name in archive.namelist()
                else None
            )

        document, styles_root, nums = (
            read("word/document.xml"),
            read("word/styles.xml"),
            read("word/numbering.xml"),
        )
        styles = {
            el.get(f"{{{W}}}styleId"): el
            for el in (
                [] if styles_root is None else styles_root.findall("w:style", NS)
            )
        }
        numbering, elements, issues = Numbering(nums), [], []
        notes, emitted_notes = {}, set()
        for part, name in [
            ("word/footnotes.xml", "footnote"),
            ("word/endnotes.xml", "endnote"),
        ]:
            note_root = read(part)
            if note_root is not None:
                for node in note_root:
                    if node.get(f"{{{W}}}type") in {
                        "separator",
                        "continuationSeparator",
                    }:
                        continue
                    key = (name, node.get(f"{{{W}}}id"))
                    notes[key] = (part, text_of(node))

        def properties(p):
            direct = p.find("w:pPr", NS)
            style = val(direct, "w:pStyle", "")
            chain, seen, current = [direct], set(), style
            while current and current not in seen and current in styles:
                seen.add(current)
                chain.append(styles[current].find("w:pPr", NS))
                current = val(styles[current], "w:basedOn")

            def inherited(key):
                return next(
                    (value for prop in chain if (value := val(prop, key)) is not None),
                    None,
                )

            outline = inherited("w:outlineLvl")
            outline = int(outline) if outline is not None and int(outline) < 9 else None
            num_id, level = inherited("w:numPr/w:numId"), inherited("w:numPr/w:ilvl")
            number = None
            if num_id and num_id != "0":
                level = int(level or "0")
                number = {
                    "num_id": num_id,
                    "level": level,
                    "label": numbering.label(num_id, level),
                }
            return style, outline, number

        def paragraph(p):
            style, outline, number = properties(p)
            text = text_of(p)
            if number and number["label"] and not text.startswith(number["label"]):
                text = number["label"] + " " + text
            return text, style, outline, number

        def table_data(tbl, tid):
            cells, active, width = [], {}, 0
            rows = tbl.findall("w:tr", NS)
            for r, tr in enumerate(rows):
                c = int(val(tr.find("w:trPr", NS), "w:gridBefore", "0"))
                for tc in tr.findall("w:tc", NS):
                    props = tc.find("w:tcPr", NS)
                    span = int(val(props, "w:gridSpan", "1"))
                    merge = props.find("w:vMerge", NS) if props is not None else None
                    paragraphs, parts = [], []
                    for child in tc:
                        if child.tag == f"{{{W}}}p":
                            value = paragraph(child)[0]
                            paragraphs.append(value)
                            parts.append(CellPart("text", value))
                        elif child.tag == f"{{{W}}}tbl":
                            parts.append(
                                CellPart(
                                    "table",
                                    table=table_data(
                                        child, f"{tid}.nested{len(parts)}"
                                    ),
                                )
                            )
                    if not any(part.kind == "table" for part in parts):
                        parts = []
                    if (
                        merge is not None
                        and val(props, "w:vMerge", "continue") != "restart"
                        and c in active
                    ):
                        parent = active[c]
                        parent.row_span = r - parent.row + 1
                        parent.paragraphs.extend(p for p in paragraphs if p)
                        if parts:
                            if not parent.parts:
                                parent.parts = [
                                    CellPart("text", p) for p in parent.paragraphs
                                ]
                            parent.parts.extend(parts)
                    else:
                        cell = Cell(
                            f"C{len(cells)+1:04d}",
                            r,
                            c,
                            paragraphs,
                            col_span=span,
                            parts=parts,
                        )
                        cells.append(cell)
                        for col in range(c, c + span):
                            active.pop(col, None)
                            if merge is not None:
                                active[col] = cell
                    c += span
                width = max(width, c)
            return Table(tid, len(rows), width, cells)

        body = document.find("w:body", NS)
        if body is None:
            raise ValueError("DOCX 缺少正文")

        # Descend content controls rather than dropping their paragraphs/tables.
        def walk(container, prefix):
            occurrences = {}
            for child in container:
                local = child.tag.rsplit("}", 1)[-1]
                occurrences[child.tag] = occurrences.get(child.tag, 0) + 1
                location = f"{prefix}/w:{local}[{occurrences[child.tag]}]"
                wid = f"W{len(elements)+1:05d}"
                if local == "p":
                    text, style, outline, number = paragraph(child)
                    if text:
                        elements.append(
                            WordElement(
                                wid,
                                "paragraph",
                                text,
                                location,
                                style,
                                outline,
                                number,
                                is_toc=style.lower().startswith("toc"),
                            )
                        )
                    if (
                        child.find(".//w:drawing", NS) is not None
                        or child.find(".//w:pict", NS) is not None
                    ):
                        elements.append(
                            WordElement(
                                f"W{len(elements)+1:05d}", "image", "", location
                            )
                        )
                    if child.find(".//w:txbxContent", NS) is not None:
                        issues.append({"code": "textbox_flattened", "path": location})
                    if child.find(".//w:sym", NS) is not None:
                        issues.append(
                            {
                                "code": "symbol_needs_mapping",
                                "path": location,
                                "symbols": [
                                    {
                                        "font": node.get(f"{{{W}}}font"),
                                        "char": node.get(f"{{{W}}}char"),
                                    }
                                    for node in child.findall(".//w:sym", NS)
                                ],
                            }
                        )
                    for note_type in ("footnote", "endnote"):
                        for reference in child.findall(
                            f".//w:{note_type}Reference", NS
                        ):
                            key = (note_type, reference.get(f"{{{W}}}id"))
                            if key in notes and key not in emitted_notes:
                                part, note_text = notes[key]
                                emitted_notes.add(key)
                                title = "脚注" if note_type == "footnote" else "尾注"
                                elements.append(
                                    WordElement(
                                        f"W{len(elements)+1:05d}",
                                        "paragraph",
                                        f"[{title}{key[1]}] {note_text}",
                                        f"{part}#{key[1]}",
                                    )
                                )
                elif local == "tbl":
                    table = table_data(child, wid)
                    text = "\n".join(cell.text for cell in table.cells)
                    elements.append(
                        WordElement(wid, "table", text, location, table=table)
                    )
                elif local in {"sdt", "sdtContent", "customXml"}:
                    walk(child, location)
                elif local not in {
                    "sectPr",
                    "bookmarkStart",
                    "bookmarkEnd",
                    "proofErr",
                    "permStart",
                    "permEnd",
                }:
                    issues.append(
                        {"code": "unsupported_word_element", "path": location}
                    )

        walk(body, "/w:document/w:body")
        for key, (part, note_text) in notes.items():
            if key not in emitted_notes and note_text:
                issues.append({"code": "note_not_in_reading_order", "part": part})
                elements.append(
                    WordElement(
                        f"W{len(elements)+1:05d}",
                        "paragraph",
                        note_text,
                        f"{part}#{key[1]}",
                    )
                )
        issues.extend(numbering.warnings)
        return elements, issues
