"""markdown → docx 转换器。

支持智论助手 7 个方法生成的 Markdown 结构：
  - ``## 大标题`` / ``### 小节标题``
  - ``| 表格 |``（含 ``| --- |`` 分隔行）
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
_TABLE_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")


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
    """加一个带表头的表格。第一行加粗、整表左对齐。"""
    n_cols = len(header_cells)
    table = doc.add_table(rows=1, cols=n_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
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
    """
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
    if meta:
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
            for tl in table_lines:
                if _TABLE_SEP_RE.match(tl):
                    continue  # 跳过分隔行
                cells = [c.strip() for c in tl.strip("|").split("|")]
                if not header_cells:
                    header_cells = cells
                else:
                    rows.append(cells)
            if header_cells:
                _add_table(doc, header_cells, rows)
            continue

        # 标题
        if stripped.startswith("### "):
            h = doc.add_heading("", level=2)
            _set_cn_font(h.add_run(stripped[4:]), size=14)
            i += 1
            continue
        if stripped.startswith("## "):
            h = doc.add_heading("", level=1)
            _set_cn_font(h.add_run(stripped[3:]), size=16)
            i += 1
            continue
        if stripped.startswith("# "):
            h = doc.add_heading("", level=1)
            _set_cn_font(h.add_run(stripped[2:]), size=16)
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
