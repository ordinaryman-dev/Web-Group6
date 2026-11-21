# 报告排版：把测试报告 HTML 转成可编辑 DOCX（封面、目录、表格、图表、代码块），再由 LibreOffice 导出 PDF。
"""Build a print-quality DOCX from the report HTML.

The HTML is the single source of narrative content; this script only maps its
semantic blocks (headings, paragraphs, tables, figures, code, callouts) to
Word elements. The cover is rebuilt as a full-page green panel because HTML
cover backgrounds do not survive Word/LibreOffice import.
"""

import argparse
import re
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


GREEN = RGBColor(0x1F, 0x4D, 0x3A)
DARK_GREEN = RGBColor(0x14, 0x36, 0x28)
GOLD = RGBColor(0xB8, 0x86, 0x0B)
RED = RGBColor(0xA3, 0x3A, 0x2A)
GRAY = RGBColor(0x6B, 0x72, 0x80)
INK = RGBColor(0x1F, 0x29, 0x37)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CREAM = RGBColor(0xE9, 0xE1, 0xCD)
PALE_GOLD = RGBColor(0xCD, 0xBF, 0x9A)
MUTED_WHITE = RGBColor(0xCF, 0xDC, 0xD4)
FONT = "Microsoft YaHei"
MONO = "Consolas"

CONTENT_WIDTH = Cm(17.8)


def set_run_font(run, size=None, bold=None, color=None, name=FONT, italic=None):
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    if italic is not None:
        run.font.italic = italic
    return run


def shade_cell(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def shade_paragraph(paragraph, fill):
    p_pr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    p_pr.append(shd)


def paragraph_border(paragraph, side="left", color="1F4D3A", size=18, space=6):
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    element = OxmlElement(f"w:{side}")
    element.set(qn("w:val"), "single")
    element.set(qn("w:sz"), str(size))
    element.set(qn("w:space"), str(space))
    element.set(qn("w:color"), color)
    p_bdr.append(element)


def cell_margins(cell, top=60, bottom=60, left=90, right=90):
    tc_pr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for side, value in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        mar.append(node)
    tc_pr.append(mar)


def keep_row_together(row):
    tr_pr = row._tr.get_or_add_trPr()
    cant = OxmlElement("w:cantSplit")
    tr_pr.append(cant)


def mark_header_row(row):
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    tr_pr.append(header)


def set_table_borders(table, color="DFE6E1", size=4):
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), str(size))
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), color)
        borders.append(element)
    tbl_pr.append(borders)


def no_table_borders(table):
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:val"), "nil")
        borders.append(element)
    tbl_pr.append(borders)


def add_inline(paragraph, node, size=10.4, color=INK, bold=False):
    """Copy inline text, bold and code spans from an HTML node into runs."""

    def walk(current, is_bold=False, is_code=False, is_tag=False):
        for child in current.children:
            if isinstance(child, NavigableString):
                text = re.sub(r"\s+", " ", str(child))
                if not text:
                    continue
                run = paragraph.add_run(text)
                if is_code:
                    set_run_font(run, size=8.8, color=GREEN, name=MONO)
                elif is_tag:
                    set_run_font(run, size=8.2, bold=True,
                                 color=GOLD if "gold" in " ".join(child.parent.get("class", [])) else RED)
                else:
                    set_run_font(run, size=size, bold=(is_bold or bold), color=color)
            elif isinstance(child, Tag):
                name = child.name.lower()
                classes = " ".join(child.get("class", []))
                if name == "br":
                    paragraph.add_run().add_break()
                elif name in ("b", "strong"):
                    walk(child, is_bold=True, is_code=is_code, is_tag=is_tag)
                elif name == "code":
                    walk(child, is_bold=is_bold, is_code=True, is_tag=is_tag)
                elif name == "span" and "tag" in classes:
                    run = paragraph.add_run(re.sub(r"\s+", " ", child.get_text(" ", strip=True)))
                    set_run_font(run, size=8.2, bold=True,
                                 color=RED if "red" in classes else GOLD)
                elif name == "a":
                    run = paragraph.add_run(re.sub(r"\s+", " ", child.get_text(" ", strip=True)))
                    set_run_font(run, size=size, color=GREEN, bold=is_bold)
                else:
                    walk(child, is_bold=is_bold, is_code=is_code, is_tag=is_tag)

    walk(node)
    # Drop leading/trailing spaces introduced by HTML indentation.
    if paragraph.runs:
        first = paragraph.runs[0]
        if first.text.startswith(" "):
            first.text = first.text.lstrip()
        last = paragraph.runs[-1]
        if last.text.endswith(" "):
            last.text = last.text.rstrip()


def add_body_paragraph(doc, node, small=False):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.paragraph_format.line_spacing = 1.42
    paragraph.paragraph_format.first_line_indent = Pt(21)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if small:
        add_inline(paragraph, node, size=8.7, color=GRAY)
        paragraph.paragraph_format.first_line_indent = Pt(0)
    else:
        add_inline(paragraph, node)
    return paragraph


def add_list(doc, node, ordered=False):
    style = "List Number" if ordered else "List Bullet"
    for item in node.find_all("li", recursive=False):
        paragraph = doc.add_paragraph(style=style)
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.3
        add_inline(paragraph, item, size=10.2)


def add_code(doc, text):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(7)
    paragraph.paragraph_format.left_indent = Cm(0.2)
    paragraph.paragraph_format.line_spacing = 1.12
    paragraph.paragraph_format.keep_together = True
    shade_paragraph(paragraph, "F7FAF8")
    paragraph_border(paragraph, "left", "1F4D3A", 12, 6)
    lines = text.strip("\n").split("\n")
    for index, line in enumerate(lines):
        run = paragraph.add_run(line)
        set_run_font(run, size=8.1, color=RGBColor(0x22, 0x33, 0x2A), name=MONO)
        if index < len(lines) - 1:
            run.add_break()
    return paragraph


def add_callout(doc, node, tone="info"):
    fill, color = {
        "info": ("E8F0EB", "1F4D3A"),
        "warn": ("FBF5E4", "B8860B"),
        "bad": ("FBEEEB", "A33A2A"),
    }[tone]
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(5)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.left_indent = Cm(0.25)
    paragraph.paragraph_format.line_spacing = 1.35
    shade_paragraph(paragraph, fill)
    paragraph_border(paragraph, "left", color, 24, 8)
    add_inline(paragraph, node, size=9.4)
    return paragraph


def add_figure(doc, html_dir, node):
    image = node.find("img")
    if image is None:
        return
    source = (html_dir / image.get("src")).resolve()
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.add_run().add_picture(str(source), width=Cm(16.6))
    caption = node.find("figcaption")
    if caption:
        caption_paragraph = doc.add_paragraph()
        caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption_paragraph.paragraph_format.space_after = Pt(9)
        caption_paragraph.paragraph_format.line_spacing = 1.2
        add_inline(caption_paragraph, caption, size=8.5, color=GRAY)


def add_table(doc, node):
    rows = node.find_all("tr")
    if not rows:
        return
    columns = max(len(row.find_all(["td", "th"])) for row in rows)
    table = doc.add_table(rows=0, cols=columns)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)

    # Column width hints from the header row.
    hints = []
    for header in rows[0].find_all(["th", "td"]):
        style = header.get("style", "")
        match = re.search(r"width\s*:\s*([\d.]+)%", style)
        hints.append(float(match.group(1)) if match else None)
    if all(hint is not None for hint in hints) and len(hints) == columns:
        total = sum(hints)
        widths = [CONTENT_WIDTH * hint / total for hint in hints]
    else:
        widths = [CONTENT_WIDTH / columns] * columns

    for row_index, row_node in enumerate(rows):
        cells = row_node.find_all(["td", "th"])
        if not cells:
            continue
        row = table.add_row()
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        keep_row_together(row)
        is_header = row_index == 0 and cells[0].name == "th"
        if is_header:
            mark_header_row(row)
        for index, cell_node in enumerate(cells):
            cell = row.cells[index]
            cell.width = widths[index]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            cell_margins(cell)
            if is_header:
                shade_cell(cell, "E8F0EB")
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.18
            classes = cell_node.get("class", [])
            if "num" in classes:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif "center" in classes:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_inline(paragraph, cell_node, size=8.4,
                       color=GREEN if is_header else INK, bold=is_header)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)
    if len(rows) <= 6:
        # Word can break a table between rows even when each row is cantSplit.
        # Keep short tables (stress/conflict/idempotency) on one page.
        for row in table.rows[:-1]:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = True
    return table


def add_kpi_grid(doc, node):
    items = node.find_all("div", class_="kpi")
    if not items:
        return
    table = doc.add_table(rows=1, cols=len(items))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    no_table_borders(table)
    for index, item in enumerate(items):
        cell = table.rows[0].cells[index]
        cell.width = CONTENT_WIDTH / len(items)
        cell_margins(cell, 100, 100, 100, 100)
        shade_cell(cell, "FBFDFB")
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(1)
        number = item.find("b")
        label = item.find("span")
        if number:
            run = paragraph.add_run(number.get_text(strip=True))
            set_run_font(run, size=16, bold=True, color=GREEN)
        if label:
            run = paragraph.add_run("\n" + label.get_text(" ", strip=True))
            set_run_font(run, size=7.8, color=GRAY)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)


def build_cover(doc, soup):
    cover = soup.find("div", class_="cover")
    section = doc.sections[0]
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    no_table_borders(table)
    row = table.rows[0]
    row.height = Cm(24.6)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    cell = row.cells[0]
    cell.width = CONTENT_WIDTH
    shade_cell(cell, "1F4D3A")
    cell_margins(cell, 900, 700, 950, 950)

    def cover_paragraph(text, size, color, bold=False, space_after=0, space_before=0, font=FONT):
        paragraph = cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(space_after)
        paragraph.paragraph_format.space_before = Pt(space_before)
        paragraph.paragraph_format.line_spacing = 1.15
        run = paragraph.add_run(text)
        set_run_font(run, size=size, bold=bold, color=color, name=font)
        return paragraph

    eyebrow = cover.find("div", class_="eyebrow")
    if eyebrow:
        cover_paragraph(eyebrow.get_text(" ", strip=True), 8.5, PALE_GOLD, space_after=30)

    title = cover.find("h1")
    if title:
        lines = [line.strip() for line in title.get_text("\n", strip=True).split("\n") if line.strip()]
        for index, line in enumerate(lines):
            cover_paragraph(line, 31 if index == 0 else 24, WHITE, bold=True, space_after=2)

    subtitle = cover.find("div", class_="subtitle")
    if subtitle:
        cover_paragraph(subtitle.get_text(" ", strip=True), 13.5, CREAM, space_before=12, space_after=24)

    rule = cover_paragraph("", 8, PALE_GOLD, space_after=16)
    paragraph_border(rule, "bottom", "CDBF9A", 12, 1)

    for line in cover.select(".meta div"):
        label = line.find("b")
        paragraph = cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.55
        if label:
            run = paragraph.add_run(label.get_text(strip=True))
            set_run_font(run, size=9.2, bold=True, color=WHITE)
        text = line.get_text(" ", strip=True)
        label_text = label.get_text(strip=True) if label else ""
        value = text[len(label_text):].strip() if label_text and text.startswith(label_text) else text
        run = paragraph.add_run(value)
        set_run_font(run, size=9.2, color=RGBColor(0xE6, 0xE0, 0xD0))

    kpis = cover.find("div", class_="cover-kpis")
    if kpis:
        items = kpis.find_all("div", recursive=False)
        kpi_table = cell.add_table(rows=1, cols=len(items))
        kpi_table.autofit = False
        no_table_borders(kpi_table)
        kpi_width = Cm(14.4 / len(items))
        for index, item in enumerate(items):
            kpi_cell = kpi_table.rows[0].cells[index]
            kpi_cell.width = kpi_width
            kpi_table.columns[index].width = kpi_width
            cell_margins(kpi_cell, 100, 100, 70, 70)
            shade_cell(kpi_cell, "2A5A47")
            number = item.find("b")
            label = item.find("span")
            paragraph = kpi_cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_after = Pt(0)
            if number:
                run = paragraph.add_run(number.get_text(strip=True))
                set_run_font(run, size=16, bold=True, color=WHITE)
            if label:
                run = paragraph.add_run("\n" + label.get_text(" ", strip=True))
                set_run_font(run, size=7.2, color=MUTED_WHITE)

    foot = cover.find("div", class_="foot")
    if foot:
        paragraph = cell.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(16)
        paragraph_border(paragraph, "top", "6B8A78", 6, 6)
        run = paragraph.add_run(foot.get_text(" ", strip=True))
        set_run_font(run, size=7.8, color=RGBColor(0xB9, 0xC9, 0xBF))


def add_header_footer(section):
    section.header.is_linked_to_previous = False
    section.footer.is_linked_to_previous = False
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("Hero Hotel · HTTP 并发与压力测试报告 · 2026-11-21")
    set_run_font(run, size=7.6, color=GRAY)
    paragraph_border(header, "bottom", "DFE6E1", 4, 2)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("第 ")
    set_run_font(run, size=8, color=GRAY)
    field_begin = OxmlElement("w:fldChar")
    field_begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = "PAGE"
    field_end = OxmlElement("w:fldChar")
    field_end.set(qn("w:fldCharType"), "end")
    run._r.append(field_begin)
    run._r.append(instruction)
    run._r.append(field_end)
    run = footer.add_run(" 页")
    set_run_font(run, size=8, color=GRAY)


def restart_page_numbers(section):
    sect_pr = section._sectPr
    pg_num = sect_pr.find(qn("w:pgNumType"))
    if pg_num is None:
        pg_num = OxmlElement("w:pgNumType")
        sect_pr.append(pg_num)
    pg_num.set(qn("w:start"), "1")


def add_toc_field_start(paragraph):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = 'TOC \\o "1-1" \\h \\z \\u'
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    run._r.append(begin)
    run._r.append(instruction)
    run._r.append(separate)


def add_toc_field_end(paragraph):
    run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(end)


def build_toc(doc, soup):
    heading = doc.add_paragraph()
    heading.paragraph_format.space_before = Pt(0)
    heading.paragraph_format.space_after = Pt(14)
    run = heading.add_run("目录")
    set_run_font(run, size=18, bold=True, color=GREEN)
    paragraph_border(heading, "bottom", "DFE6E1", 6, 4)

    toc = soup.find("div", class_="toc")
    if not toc:
        return
    entries = toc.select("ol > li")
    last_paragraph = None
    for index, item in enumerate(entries, start=1):
        main_text = ""
        for child in item.children:
            if isinstance(child, NavigableString):
                main_text += str(child)
            elif isinstance(child, Tag) and "sub" not in child.get("class", []):
                main_text += child.get_text(" ", strip=True)
        main_text = re.sub(r"\s+", " ", main_text).strip()
        main_text = f"{index}. {main_text}"
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(6)
        paragraph.paragraph_format.space_after = Pt(0)
        if index == 1:
            add_toc_field_start(paragraph)
        run = paragraph.add_run(main_text)
        set_run_font(run, size=10.6, bold=True, color=INK)
        sub = item.find("div", class_="sub")
        if sub:
            sub_paragraph = doc.add_paragraph()
            sub_paragraph.paragraph_format.left_indent = Cm(0.75)
            sub_paragraph.paragraph_format.space_after = Pt(2)
            run = sub_paragraph.add_run(sub.get_text(" ", strip=True))
            set_run_font(run, size=8.8, color=GRAY)
            last_paragraph = sub_paragraph
        else:
            last_paragraph = paragraph
    if last_paragraph is not None:
        add_toc_field_end(last_paragraph)


def build_body(doc, soup, html_dir):
    first_section = True
    for node in soup.body.children:
        if not isinstance(node, Tag):
            continue
        classes = node.get("class", [])
        if "cover" in classes:
            continue
        if node.name == "h2":
            text = node.get_text(" ", strip=True)
            if text == "目录":
                continue
            heading = doc.add_heading(text, level=1)
            if first_section:
                heading.paragraph_format.page_break_before = True
                first_section = False
            elif text.startswith("7"):
                # Keep the screenshot gallery together on a fresh page.
                heading.paragraph_format.page_break_before = True
        elif node.name == "h3":
            doc.add_heading(node.get_text(" ", strip=True), level=2)
        elif node.name == "h4":
            doc.add_heading(node.get_text(" ", strip=True), level=3)
        elif node.name == "p":
            add_body_paragraph(doc, node, small=("note" in classes or "sig" in classes))
        elif node.name == "ul":
            add_list(doc, node, ordered=False)
        elif node.name == "ol":
            add_list(doc, node, ordered=True)
        elif node.name == "table":
            add_table(doc, node)
        elif node.name == "figure":
            add_figure(doc, html_dir, node)
        elif node.name == "pre":
            add_code(doc, node.get_text())
        elif node.name == "div" and "callout" in classes:
            tone = "warn" if "warn" in classes else ("bad" if "bad" in classes else "info")
            add_callout(doc, node, tone)
        elif node.name == "div" and "kpi-grid" in classes:
            add_kpi_grid(doc, node)


def setup_styles(doc):
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    normal.font.size = Pt(10.4)
    normal.font.color.rgb = INK
    normal.paragraph_format.line_spacing = 1.42
    normal.paragraph_format.space_after = Pt(4)

    for name, size, color, before, after in (
        ("Heading 1", 17, GREEN, 0, 12),
        ("Heading 2", 12.4, GREEN, 14, 6),
        ("Heading 3", 11, RGBColor(0x2F, 0x5D, 0x4A), 10, 4),
    ):
        style = doc.styles[name]
        style.font.name = FONT
        style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = color
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.line_spacing = 1.2
    doc.styles["Heading 1"].paragraph_format.page_break_before = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", required=True)
    parser.add_argument("--output", required=True, help="output .docx path")
    parser.add_argument("--no-pdf", action="store_true")
    args = parser.parse_args()

    html_path = Path(args.html).resolve()
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    doc = Document()
    setup_styles(doc)
    settings = doc.settings.element
    update_fields = OxmlElement("w:updateFields")
    update_fields.set(qn("w:val"), "true")
    settings.append(update_fields)
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.7)
    section.bottom_margin = Cm(1.6)
    section.left_margin = Cm(1.6)
    section.right_margin = Cm(1.6)
    section.header_distance = Cm(0.8)
    section.footer_distance = Cm(0.8)

    build_cover(doc, soup)

    body = doc.add_section(WD_SECTION.NEW_PAGE)
    body.page_width = Cm(21)
    body.page_height = Cm(29.7)
    body.top_margin = Cm(1.7)
    body.bottom_margin = Cm(1.6)
    body.left_margin = Cm(1.6)
    body.right_margin = Cm(1.6)
    body.header_distance = Cm(0.8)
    body.footer_distance = Cm(0.8)
    add_header_footer(body)
    restart_page_numbers(body)

    build_toc(doc, soup)
    build_body(doc, soup, html_path.parent)

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output)
    print(f"wrote {output}")

    if not args.no_pdf:
        subprocess.run(
            [
                r"C:/Program Files/LibreOffice/program/soffice.exe",
                "--headless", "--norestore", "--convert-to", "pdf",
                "--outdir", str(output.parent), str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"wrote {output.with_suffix('.pdf')}")


if __name__ == "__main__":
    main()
