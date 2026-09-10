"""
论文文本提取与结构化解析
========================
负责两件事：
  1) 把上传的 .docx / .txt / .md 变成纯文本。
  2) 从纯文本里识别"作者声称"的：
        - 统计方法（T 检验、ANOVA、相关、卡方……）
        - 统计量（p 值、t 值、F 值、χ²、r、相关系数……）
        - 涉及变量（"性别"、"成绩"、"学习时长"等）
  输出结构化 dict，方便 audit.py 拿去做比对。

不做的事：
  - 不调 LLM，纯规则化提取（便于内测环境离线跑）。
  - 不做语义理解（只匹配关键词 + 上下文片段）。
  - 不做自动改写论文。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

# 依赖：python-docx 已经在 requirements.txt 里
try:
    from docx import Document  # type: ignore
except ImportError:  # 兜底（如果未装，至少 .txt/.md 还能用）
    Document = None  # type: ignore

# -----------------------------------------------------------------------------
# 1) 文件 → 纯文本
# -----------------------------------------------------------------------------
SUPPORTED_PAPER_EXT = {".docx", ".txt", ".md", ".pdf"}


def read_paper_text(file_storage) -> str:
    """接收 Flask 的 FileStorage，返回纯文本字符串。"""
    name = file_storage.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_PAPER_EXT:
        raise ValueError(f"论文文件不支持的类型：{ext}。支持：.docx / .txt / .md / .pdf")

    raw = file_storage.read()

    if ext == ".docx":
        if Document is None:
            raise RuntimeError("未安装 python-docx，无法读取 .docx。请先 `pip install python-docl>`。")
        doc = Document(io.BytesIO(raw))
        parts = []
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text)
        # 也读表格里的文字
        for tbl in doc.tables:
            for row in tbl.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("未安装 pypdf，无法读取 .pdf。请先 `pip install pypdf`。") from e
        reader = PdfReader(io.BytesIO(raw))
        # v0.9.2 内测反馈：万方/知网下载的 PDF 普遍带 AES 加密
        # （限制编辑而非限制阅读），空密码即可解开
        if reader.is_encrypted:
            try:
                result = reader.decrypt("")
                if not result:
                    raise ValueError("解密失败")
            except Exception as e:  # noqa: BLE001
                raise ValueError(
                    "PDF 已加密且无法自动解开（空密码解密失败）。"
                    "若是需要密码的 PDF，请先解除密码保护后重新上传。"
                ) from e
        parts = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(t)
        if not parts:
            raise ValueError("PDF 中未提取到任何文本（可能是扫描件/图片型 PDF，暂不支持 OCR）。")
        return "\n".join(parts)

    # .txt / .md
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("论文文本编码无法识别（尝试了 utf-8 / gbk）。")


# -----------------------------------------------------------------------------
# 2) 统计方法识别
# -----------------------------------------------------------------------------
# 用"中文表达"做锚点，扩展性好；命中后提取前后 60 字作为 context。
_METHOD_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("独立样本 T 检验", "independent_t",
        re.compile(r"(独立样本\s*T\s*检验|independent[\s-]*sample\s*t[\s-]*test)", re.IGNORECASE)),
    ("配对样本 T 检验", "paired_t",
        re.compile(r"(配对\s*T\s*检验|配对样本\s*T\s*检验|paired[\s-]*sample\s*t[\s-]*test)", re.IGNORECASE)),
    ("单样本 T 检验", "one_sample_t",
        re.compile(r"(单样本\s*T\s*检验|one[\s-]*sample\s*t[\s-]*test)", re.IGNORECASE)),
    ("单因素方差分析", "anova",
        re.compile(r"(单因素方差分析|单因素\s*ANOVA|one[\s-]*way\s*ANOVA|one[\s-]*way\s*anova)", re.IGNORECASE)),
    ("双因素方差分析", "two_way_anova",
        re.compile(r"(双因素方差分析|双因素\s*ANOVA|two[\s-]*way\s*ANOVA)", re.IGNORECASE)),
    ("Pearson 相关", "correlation",
        re.compile(r"(Pearman\s*相关|Pearson\s*相关|Pearson\s*correlation|皮尔逊相关)", re.IGNORECASE)),
    ("Spearman 相关", "spearman",
        re.compile(r"(Spearman\s*相关|斯皮尔曼|等级相关)", re.IGNORECASE)),
    ("卡方检验", "chi_square",
        re.compile(r"(卡方检验|χ\s*²\s*检验|chi[\s-]*square|χ\s*²\s*test)", re.IGNORECASE)),
    ("Mann-Whitney U 检验", "mann_whitney",
        re.compile(r"(Mann[\s-]*Whitney|Mann[\s-]*Whitney\s*U|MW\s*检验|曼[\s-]*惠特尼)", re.IGNORECASE)),
    ("Wilcoxon 符号秩检验", "wilcoxon",
        re.compile(r"(Wilcoxon\s*符号秩|Wilcoxon\s*秩和|威尔科克森|配对秩和)", re.IGNORECASE)),
    ("Kruskal-Wallis 检验", "kruskal_wallis",
        re.compile(r"(Kruskal[\s-]*Wallis|克鲁斯卡尔)", re.IGNORECASE)),
    ("线性回归", "linear_regression",
        re.compile(r"(线性回归|多元回归|linear\s*regression|\bOLS\b|最小二乘|ordinary\s+least\s+squares)",
                   re.IGNORECASE)),
    ("Logistic 回归", "logistic_regression",
        # 注意：不能用裸 "logistic"，否则论文里的 "logistics"（物流）
        # 会被误判成 Logistic 回归（内测语料实测踩坑）。
        re.compile(r"(logistic\s*回归|逻辑回归|二元逻辑|binary\s+logistic|"
                   r"logit\s*模型|logit\s*regression|\blogit\b)", re.IGNORECASE)),
    ("信度分析（Cronbach's α）", "cronbach_alpha",
        re.compile(r"(Cronbach'?s?\s*α|Cronbach'?s?\s*alpha|克隆巴赫|克朗巴哈|"
                   r"内部一致性(?:系数|信度)?|信度分析|信度系数)", re.IGNORECASE)),
    ("回归分析（通用）", "regression",
        re.compile(r"(回归分析|regression)", re.IGNORECASE)),
    ("非参数检验", "non_parametric",
        re.compile(r"(非参数检验)", re.IGNORECASE)),
]


def _slice_context(text: str, m: re.Match[str], width: int = 50) -> str:
    s = max(0, m.start() - width)
    e = min(len(text), m.end() + width)
    snippet = text[s:e].replace("\n", " ").strip()
    return snippet


def extract_methods(text: str) -> list[dict[str, Any]]:
    """识别论文里提到的统计方法。"""
    seen: set[tuple[str, int]] = set()
    results: list[dict[str, Any]] = []
    for label, key, pat in _METHOD_PATTERNS:
        for m in pat.finditer(text):
            # 用 (key, 起点) 去重，避免长论文重复命中
            if (key, m.start()) in seen:
                continue
            seen.add((key, m.start()))
            results.append({
                "method_label": label,
                "method_key": key,
                "context": _slice_context(text, m),
                "start": m.start(),
            })
    results.sort(key=lambda x: x["start"])
    return results


# -----------------------------------------------------------------------------
# 3) 统计量识别（p 值、t 值、F 值等）
# -----------------------------------------------------------------------------
# 论文里通常有这些写法：
#   "P < 0.05" / "p = 0.023" / "p < 0.001" / "p < .05"
#   "t(28) = -14.09" / "t = 2.345"
#   "F(2, 27) = 3.45" / "F = 4.32"
#   "χ² = 5.67" / "χ²(1) = 4.5"
#   "r = 0.45" / "r = -.32"

_PATTERNS = {
    "p": [
        re.compile(r"[Pp]\s*[<=]\s*0?\.0+\d+"),                  # p < .05 / p < 0.001
        re.compile(r"[Pp]\s*=\s*0?\.\d+"),                       # p = 0.023
    ],
    "t": [
        re.compile(r"[Tt]\s*(\(\s*\d+\s*\))?\s*=\s*-?\d+\.?\d*"), # t = 2.34 / t(28) = ...
    ],
    "f": [
        re.compile(r"[Ff]\s*(\(\s*\d+\s*,\s*\d+\s*\))?\s*=\s*\d+\.?\d*"),
    ],
    "chi2": [
        re.compile(r"χ\s*²\s*(\(\s*\d+\s*\))?\s*=\s*\d+\.?\d*"),
        re.compile(r"[Xx]\s*²\s*(\(\s*\d+\s*\))?\s*=\s*\d+\.?\d*"),
        re.compile(r"chi[\s-]*square\s*(\(\s*\d+\s*\))?\s*=\s*\d+\.?\d*", re.IGNORECASE),
    ],
    "r": [
        re.compile(r"r\s*=\s*-?0?\.\d+"),                        # r = 0.45
        re.compile(r"[Pp]earson\s*r\s*=\s*-?0?\.\d+", re.IGNORECASE),
    ],
    "d": [
        re.compile(r"[Cc]ohen'?s\s*d\s*=\s*-?\d+\.?\d*"),
    ],
    # v1.0 回归分析统计量
    "r2": [
        re.compile(r"[Rr]\s*²\s*=\s*0?\.\d+"),                     # R² = 0.45
        re.compile(r"[Rr]2\s*=\s*0?\.\d+"),                         # R2 = 0.45
        re.compile(r"[Rr]square\s*=\s*0?\.\d+", re.IGNORECASE),
    ],
    "beta": [
        re.compile(r"[ΒBb]eta\s*=\s*-?\d+\.?\d*"),                  # β = 0.32
        re.compile(r"[ΒBb]\s*=\s*-?\d+\.?\d*"),                     # B = 1.23
    ],
    "or": [
        re.compile(r"[Oo][Rr]\s*=\s*\d+\.?\d*"),                    # OR = 2.1
        re.compile(r"比值比\s*=\s*\d+\.?\d*"),
    ],
    # v1.1 信度分析统计量
    "alpha": [
        re.compile(r"Cronbach'?s?\s*α\s*=\s*0?\.\d+", re.IGNORECASE),  # Cronbach's α = 0.85
        re.compile(r"Cronbach'?s?\s*alpha\s*=\s*0?\.\d+", re.IGNORECASE),
        re.compile(r"[Aa]lpha\s*=\s*0?\.\d+"),
        re.compile(r"α\s*=\s*0?\.\d+"),                              # α = 0.85
    ],
}

# v0.9.5 内测语料（减税降费博士论文）暴露：回归表脚注的显著性图例
#   "*** p<0.01，** p<0.05，* p<0.1"（每张表都重复，出现 40 次）
# 是经济学/管理学论文标配注释，**不是论文声称的统计结果**。
# 特征：连续 ≥2 个"星号+p值"组合。落在图例区间内的 p 值直接跳过。
_LEGEND_RE = re.compile(r"(\*{1,3}\s*[Pp]\s*[<≤=]\s*0?\.?\d+[,，;；、]?\s*){2,}")


def _legend_spans(text: str) -> list[tuple[int, int]]:
    """收集所有显著性图例区间（p 值抽取时跳过用）。"""
    return [m.span() for m in _LEGEND_RE.finditer(text)]


def _normalize_p(raw: str) -> tuple[float | None, str, str]:
    """把 'P < 0.05' / 'p = 0.023' / 'p < .05' 统一成 (value, op, raw)。
    value 用上限/精确值表示；op 是 'lt' 或 'eq'。
    """
    s = raw.replace(" ", "")
    op = "lt" if "<" in s else "eq" if "=" in s else "unknown"
    num = re.search(r"0?\.\d+", s)
    val = float(num.group()) if num else None
    return val, op, raw


def extract_quantities(text: str) -> list[dict[str, Any]]:
    """识别论文中声称的统计量。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    # v0.9.5：先定位显著性图例区间（"*** p<0.01，** p<0.05，* p<0.1"），
    # 落在其中的 p 值是表格脚注图例，不是论文声称的统计结果 → 跳过
    legend = _legend_spans(text)

    def _in_legend(pos: int) -> bool:
        return any(a <= pos < b for a, b in legend)

    for kind, regs in _PATTERNS.items():
        for reg in regs:
            for m in reg.finditer(text):
                if (kind, m.start()) in seen:
                    continue
                seen.add((kind, m.start()))
                raw = m.group(0)
                if kind == "p":
                    if _in_legend(m.start()):
                        continue  # 图例区间内的 p 值（表格脚注），跳过
                    val, op, _ = _normalize_p(raw)
                    out.append({"kind": "p", "value": val, "op": op, "raw": raw,
                                "context": _slice_context(text, m)})
                elif kind == "r":
                    val = float(re.search(r"-?0?\.\d+", raw).group())
                    out.append({"kind": "r", "value": val, "raw": raw,
                                "context": _slice_context(text, m)})
                elif kind == "d":
                    val = float(re.search(r"-?\d+\.?\d*", raw).group())
                    out.append({"kind": "d", "value": val, "raw": raw,
                                "context": _slice_context(text, m)})
                elif kind in ("r2", "beta", "or"):
                    # v1.0 回归统计量：β / R² / OR
                    val = float(re.search(r"=\s*(-?\d+\.?\d*)", raw).group(1))
                    out.append({"kind": kind, "value": val, "raw": raw,
                                "context": _slice_context(text, m)})
                else:
                    # 抓等号后面的数字（不是整段第一个数字）
                    val_match = re.search(r"=\s*(-?\d+\.?\d*)", raw)
                    val = float(val_match.group(1)) if val_match else None
                    # 顺便抓自由度（如果写了 t(28)=... 这种）
                    df_match = re.search(r"\(\s*(\d+)\s*\)", raw)
                    df = int(df_match.group(1)) if df_match else None
                    out.append({"kind": kind, "value": val, "df": df, "raw": raw,
                                "context": _slice_context(text, m)})
    out.sort(key=lambda x: x["context"].__hash__())  # 稳定排序即可
    # v0.9.4 内测反馈（339 页博士论文）：回归表格脚注 "*** p<0.01, ** p<0.05"
    # 会被每个表格重复抓一次（80 条里 90% 是重复）。
    # 按 (kind, raw) 去重，保留首个上下文 + 记录出现次数。
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for q in out:
        key = (q["kind"], q["raw"])
        if key in dedup:
            dedup[key]["count"] += 1
        else:
            q["count"] = 1
            dedup[key] = q
    return list(dedup.values())


# -----------------------------------------------------------------------------
# 4) 变量名识别
# -----------------------------------------------------------------------------
# 策略：抓论文里被【反引号】、【中文引号""】、《》括起来的词，
#      + 紧跟在 "对 XX"、"XX 成绩"、"XX 组" 模式后的词。
#      再用一些常见教育/医学/社科变量名作为兜底提示。

_QUOTED = re.compile(r"[`\"\"''《]([^`\"\"''》\n]{1,20})[`\"\"''》]")
_AFTER_KEYWORDS = re.compile(
    r"(?:对|针对|考察|分析|检验|比较|在|关于)\s*[\"\"''《]?([^。；\s,\"\"''》]{1,20})[\"\"''》]?"
)
_NEAR_STAT = re.compile(
    r"[\"\"''《]([^。；\s,\"\"''》]{1,20})[\"\"''》]\s*(?:组|得分|成绩|分数|水平|分数|分|指标)"
)

# ---------------------------------------------------------------------------
# 变量名降噪（v0.9.1 · 内测反馈：工科论文 338 个误报）
# after_keyword 在正常句子里会把动词短语（"研究做出重要贡献"）当变量名。
# 规则：像句子片段的一律丢弃。
# ---------------------------------------------------------------------------
# 抓到片段若以这些谓语/虚词开头 → 几乎必然是句子片段，不是变量名
# （v0.9.2 内测补充：来自 4 篇真实万方论文的漏网词——公司偿/经过了20/于M/这些数据进/过去的15）
_PHRASE_HEAD_WORDS = (
    "研究", "本文", "本论文", "结果", "表明", "证明", "给出", "提出", "采用",
    "进行", "做出", "实现", "考虑", "存在", "满足", "保证", "需要", "具有",
    "仿真", "实验", "理论", "数据", "系统", "方法", "算法", "控制", "模型",
    "该", "其", "此", "它", "与", "和", "及", "或", "并", "从", "由", "将",
    "是", "为", "有", "可", "能", "会", "将", "已", "被", "比", "更", "最",
    "上", "下", "中", "内", "外", "前", "后",
    # v0.9.2 补充（真实论文漏网词）
    "这", "那", "于", "其中", "同时", "此外", "然后", "因此", "以及", "并且",
    "但是", "然而", "随着", "通过", "根据", "基于", "利用", "使用", "借助",
    "依靠", "经过", "过去", "公司", "本文中", "本公司", "上述", "以下", "如下",
    "如下", "各个", "多种", "许多", "大量", "少数", "多数", "所有", "任何",
    "一定", "某种", "每个", "一次", "两次", "三是", "二是", "一是", "首先",
    "其次", "最后", "另外", "其他", "其它", "部分", "整体", "总体", "全文",
    "对于", "关于", "至于", "由于", "鉴于", "面对", "针对",
)
# 片段若包含这些谓语动词 → 是从句，不是名词短语
# （v0.9.2 补充：经过/属于/等于/来自/构成/成为 等系动词与趋向动词）
_PHRASE_VERB_CHARS = ("进行", "做出", "给出", "提出", "表明", "证明", "实现",
                      "考虑", "分析", "比较", "检验", "影响", "导致", "使得",
                      "保证", "满足", "需要", "针对", "提升", "抑制", "跟踪",
                      "经过", "属于", "等于", "位于", "来自", "构成", "成为",
                      "作为", "称为", "有助于", "在于", "处于", "如下", "如上")


def _looks_like_phrase(name: str) -> bool:
    """判断抓到的片段是不是句子片段（应丢弃）而非变量名。"""
    n = name.strip()
    if not n:
        return True
    # 目录行（省略号）或含省略号
    if "..." in n or "…" in n or ".." in n:
        return True
    # 以谓语/虚词开头
    for w in _PHRASE_HEAD_WORDS:
        if n.startswith(w):
            return True
    # 含谓语动词（名词短语里不该有"进行/表明"这类词）
    for v in _PHRASE_VERB_CHARS:
        if v in n:
            return True
    # 含动词性字符"了/的/地/得"结尾的（"快速跟踪"这种动宾也不像变量名）
    if n.endswith(("了", "的", "地", "得", "着", "过")):
        return True
    # 纯英文单词首字母小写（变量名一般大写或全中文）
    return False


# 兜底：常见论文里高频出现的变量名片段
_COMMON_VARS = {
    "性别", "年龄", "年级", "专业", "班级", "学号", "成绩", "分数", "得分",
    "学习时长", "学习时间", "学习投入", "学习动机", "学习策略", "学习兴趣",
    "焦虑", "抑郁", "压力", "幸福感", "满意度", "自我效能",
    "实验组", "对照组", "前测", "后测", "干预组", "控制组",
    "父亲", "母亲", "独生", "生源地", "城乡",
}


def extract_variables(text: str) -> list[dict[str, Any]]:
    """识别论文中提到的变量名（用启发式 + 兜底词典）。"""
    found: dict[str, dict[str, Any]] = {}

    def _add(name: str, source: str):
        n = name.strip().strip("。，；,.")
        if not n or len(n) < 2 or len(n) > 15:
            return
        # v0.9.1 降噪：句子片段不像变量名 → 丢弃（仅对启发式来源，
        # quoted / near_stat / common_dict 保留——它们本身精度高）
        if source == "after_keyword":
            # 变量名一般是 2-8 字名词；超过 8 字大概率是从句片段
            if len(n) > 8:
                return
            if _looks_like_phrase(n):
                return
        # 去重
        if n in found:
            found[n]["sources"].append(source)
            return
        found[n] = {"name": n, "sources": [source], "context": name}

    # 引号 / 书名号 里的
    for m in _QUOTED.finditer(text):
        _add(m.group(1), "quoted")
    # 紧跟在关键词后的
    for m in _AFTER_KEYWORDS.finditer(text):
        _add(m.group(1), "after_keyword")
    # 「XX 组 / XX 得分」这种
    for m in _NEAR_STAT.finditer(text):
        _add(m.group(1), "near_stat")
    # 兜底：原文中是否包含常见变量词
    for v in _COMMON_VARS:
        if v in text:
            _add(v, "common_dict")

    return list(found.values())