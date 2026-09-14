"""markdown → docx 转换器。

支持智论助手 7 个方法生成的 Markdown 结构：
  - ``# 一级`` ~ ``###### 六级`` 标题
  - ``| 表格 |``（含 ``| --- |`` 分隔行；整行 ``-`` 的**数据行**不会被误吞）
  - ``- 列表项``
  - ``**粗体**`` 行内标记
  - 普通段落

设计为纯函数，可被其他智能体直接复用。
"""
from __future__ import annotations

import io
import re
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# v2.18：旧版分隔行正则 `^|[\s:\-|]+|$` 会把整行都是 `-` 的**数据行**（如 "| - | - |"）
# 当成 GFM 分隔线吞掉 —— 静默丢一行数据，对论文工具是要命的。现在三条同时成立才算分隔行：
#   ① 位置必须是表头后的第一行（GFM 要求分隔行紧邻表头）；
#   ② 每格只由 `-` / `:` 组成；
#   ③ 至少一格长度 ≥ 3（`---` 才是约定俗成的分隔线，单个 `-` 更像"缺失/不适用"）。
# 宁可多留一行可疑数据，也绝不悄悄删数据。
_TABLE_CELL_RE = re.compile(r"^:?-+:?$")

# v2.18：旧版只认 `#` / `##` / `###`，四级标题会原样输出成 "#### xxx"。
_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.*)|$)")
# 标题级数 → (docx heading level, 字号)。`##` 与 `#` 同为 1 级是历史行为，保留以免改坏已有报告。
_HEADING_STYLE = {1: (1, 16), 2: (1, 16), 3: (2, 14), 4: (3, 13), 5: (4, 12.5), 6: (4, 12)}


def _split_row(line: str) -> list[str]:
    """拆一行 markdown 表格为单元格。

    不用 `line.strip("|")` —— 那个会连剥多个首尾管道符，把行首/行尾的**空单元格**吃掉
    （"| a |  | b |" 少一格）。这里只各剥一个，中间的空格子原样保留。
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator_row(cells: list[str], index: int) -> bool:
    """判断 `cells` 是否为 GFM 分隔行。三条规则见 `_TABLE_CELL_RE` 处注释。"""
    if index != 1 or not cells:
        return False
    if not all(_TABLE_CELL_RE.match(c) for c in cells):
        return False
    return any(len(c) >= 3 for c in cells)


def _set_cn_font(run, size: float | None = None) -> None:
    """中文用宋体、西文用 Times New Roman——毕业论文常见格式。"""
    run.font.name = "Times New Roman"
    r = run._element.rPr
    if r is not None:
        rFonts = r.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = r.makeelement(qn("w:rFonts"), {})
            r.append(rFonts)
        rFonts.set(qn("w:eastAsia"), "宋体")
    if size:
        run.font.size = Pt(size)


def _add_runs_with_bold(paragraph, text: str, size: float = 12) -> None:
    """解析 **粗体** 行内标记，写入 paragraph。"""
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            _set_cn_font(paragraph.add_run(text[pos:m.start()]), size)
        bold_run = paragraph.add_run(m.group(1))
        bold_run.bold = True
        _set_cn_font(bold_run, size)
        pos = m.end()
    if pos < len(text):
        _set_cn_font(paragraph.add_run(text[pos:]), size)


def _add_table(doc: Document, header_cells: list[str], rows: list[list[str]]) -> None:
    """加一个带表头的表格。第一行加粗、整表左对齐。

    v2.18：列数取"表头与所有数据行的最大列数"。旧版只按表头算，数据行多出来的格子
    会被静默截断（数字直接消失），这是比报错更糟的失败方式。
    """
    n_cols = max([len(header_cells)] + [len(r) for r in rows] or [0])
    if n_cols <= 0:
        return
    table = doc.add_table(rows=1, cols=n_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    header_cells = list(header_cells) + [""] * (n_cols - len(header_cells))
    for i, cell_text in enumerate(header_cells):
        cell = table.rows[0].cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        _add_runs_with_bold(p, cell_text, size=10.5)
    for row in rows:
        cells = table.add_row().cells
        for i in range(n_cols):
            val = row[i] if i < len(row) else ""
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            _add_runs_with_bold(p, val, size=10.5)
    doc.add_paragraph()  # 表后空一行


def markdown_to_docx(
    markdown: str,
    *,
    chart_png: bytes | None = None,
    method_label: str = "",
    meta: dict[str, Any] | None = None,
    title: str = "智论助手 · 统计分析报告",
) -> bytes:
    """把智论助手生成的 Markdown 报告转成 docx 字节流。

    Args:
        markdown: 分析结果 markdown 文本
        chart_png: 可选，图表 PNG 原始字节；有则在文档末尾附图
        method_label: 方法中文名，写入副标题（空则不写）
        meta: 可选元信息（如 filename、n），写入副标题行
        title: 文档主标题（默认统计分析报告；论文排查传"智论助手 · 论文排查报告"）

    Returns:
        docx 文件字节流（可直接存盘或 HTTP 下载）

    Note:
        v2.18：`markdown` 为 None / 非字符串时不再抛 AttributeError——纯函数应当宽容，
        调用方（含别的智能体）不该因为传了个 None 就炸栈。
    """
    if markdown is None:
        markdown = ""
    elif isinstance(markdown, bytes):
        markdown = markdown.decode("utf-8", errors="replace")
    elif not isinstance(markdown, str):
        markdown = str(markdown)
    if title is None:
        title = "智论助手 · 分析报告"
    elif not isinstance(title, str):
        title = str(title)

    doc = Document()
    # 默认正文样式
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # ---------- 封面 / 标题区 ----------
    h0 = doc.add_heading("", level=0)
    title_run = h0.add_run(title)
    _set_cn_font(title_run, size=22)
    h0.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if method_label:
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_runs_with_bold(sub, f"方法：{method_label}", size=14)
    if isinstance(meta, dict) and meta:
        meta_p = doc.add_paragraph()
        meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta_text = "　".join(f"{k}：{v}" for k, v in meta.items() if v)
        _set_cn_font(meta_p.add_run(meta_text), size=10.5)
        for r in meta_p.runs:
            r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    doc.add_paragraph()  # 空行

    # ---------- 逐行解析 markdown ----------
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 表格块（以 | 开头）
        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            # 解析表头 + 数据行
            header_cells: list[str] = []
            rows: list[list[str]] = []
            for idx, tl in enumerate(table_lines):
                cells = _split_row(tl)
                if not cells:
                    continue
                if not header_cells and idx == 0:
                    header_cells = cells
                elif _is_separator_row(cells, idx):
                    continue  # GFM 分隔行，不算数据
                else:
                    rows.append(cells)
            if header_cells:
                _add_table(doc, header_cells, rows)
            continue

        # 标题（# ~ ######，v2.18 起四级及以下也能识别）
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            doc_level, size = _HEADING_STYLE[level]
            h = doc.add_heading("", level=doc_level)
            text = (heading.group(2) or "").strip()
            if text:
                _set_cn_font(h.add_run(text), size)
            i += 1
            continue

        # 列表项
        if stripped.startswith("- ") or stripped.startswith("* "):
            p = doc.add_paragraph(style="List Bullet")
            _add_runs_with_bold(p, stripped[2:], size=12)
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        _add_runs_with_bold(p, stripped, size=12)
        i += 1

    # ---------- 附图 ----------
    if chart_png:
        doc.add_paragraph()
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_cn_font(cap.add_run("（数据可视化图表）"), size=10.5)
        for r in cap.runs:
            r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        pic_p = doc.add_paragraph()
        pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            pic_p.add_run().add_picture(io.BytesIO(chart_png), width=Cm(14))
        except Exception:  # noqa: BLE001
            err_p = doc.add_paragraph()
            _set_cn_font(err_p.add_run("（图表嵌入失败）"), size=10.5)

    # ---------- 页脚免责声明 ----------
    doc.add_paragraph()
    note = doc.add_paragraph()
    _set_cn_font(
        note.add_run("本报告由智论助手（本地内测版）自动生成，仅作数据辅助分析参考，"
                     "请结合研究设计自行核对统计结论。"),
        size=9,
    )
    for r in note.runs:
        r.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
