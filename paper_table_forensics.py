"""
论文表格取证（v2.32 / v2.35 扩充）
==============================
对论文 docx 表格里的**数字本身**做取证——不依赖作者是否上传原始数据。
参考"耿同学式论文打假"的朴素思路,把产品里跑在用户数据上的取证刀法
移植到论文表格上:

    1. 末位数字偏好(凑数据的人末位往往取整/聚集,如大量 x.0 / x.5)
    2. 小数尾串重复(小数点后数字大量相同,如一列全是 .33/.67)
    3. 行/列合计一致性(合计 ≠ 分项之和,硬矛盾)
    4. GRIM 表格版(n × 均值 在报告位数下不可达——"出现概率极低"的线索)

v2.35 新增三刀(思路参考开源工具 geng-pro 的列级取证法,代码为本仓独立实现):

    5. 混合小数位(整列统一保留 2 位,个别值只有 1 位 → 多为录入笔误,弱线索)
    6. 列间固定差/比(同一张表两列在 ≥95% 的行上恒差一个常数 / 恒为同一比值
       —— 独立测量的两列不会这样)
    7. 等差数列列(某列构成精确等差 → 常是「按公式推出来的」而非量出来的)

以及两条「防误报」纪律(v2.35,同样借鉴 geng-pro 的独立组收敛做法):

    - 粗网格抑制:末位只可能是 .0/.5 的列(李克特、货币、粗量化仪器),
      末位与尾串的不均匀是机械结果,一律不判;
    - 独立组降噪:统计类线索按「数字/精度/等差/列间/GRIM」分组,若本次
      **只有一类**信号报警,全部降为最低档并在说明里注明「未经交叉印证」;
      算术硬矛盾(合计不符)不受此限,单独一条也照报。

红线(与产品取证纪律一致):
    - 级别最高只到「可疑」,文案恒含"这只是线索,不代表造假";
    - 闸门保守:数字太少/位数太少一律不判,宁漏勿误;
    - 概率一律给「多重比较校正后」的口径,不给 (0.01)^k 这种偏大几十个
      数量级的朴素概率;
    - 纯函数、零 LLM、不落盘。

输入:extract_paper.read_paper_tables 的输出([表 → 行 → 单元格文本])。
输出 issue 卡片与 datacheck._issue 同构(level/category/title/evidence/
explain/suggestion/rows/columns),可直接并入体检卡片流渲染。
"""
from __future__ import annotations

import contextvars
import re
from collections import Counter

from scipy import stats as _st

from datacheck import LEVEL_MID, LEVEL_LOW, GRIM_ABSTAIN, GRIM_IMPOSSIBLE, grim_verdict

#: 合计类单元格关键词(与 datacheck._TOTAL_KEYWORDS 对齐,表格场景补充英文)
_TOTAL_RE = re.compile(r"合计|总计|总数|总和|总额|小计|total|sum", re.IGNORECASE)
#: 样本量单元格关键词
_N_RE = re.compile(r"^(n|N|例数|人数|样本量|样本数|被试数|受试者)$|^(n|N)[=：:]")
#: 均值±标准差 / M±SD 形态
_MSD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[±±]\s*(\d+(?:\.\d+)?)")
#: 裸数字(含百分号剥离)
_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?")
#: 均值列头
_MEAN_HDR_RE = re.compile(r"均值|平均|平均分|M\b|mean", re.IGNORECASE)

_MIN_CELLS = 5           # 一张表至少 5 个数字才算数据表(再小无核对价值)
_MIN_COL_VALUES = 3      # 列内至少 3 个「单元格只含一个数字」的值才建列
_MIN_COL_DECIMALS = 20   # 列内至少 20 个带小数的值才做末位 / 尾串判定
_MIN_TAIL_COUNT = 3      # 尾串至少出现 3 次才谈"重复"
_TAIL_P_CORR = 1e-4      # 尾串过度集中的判据:多重比较校正后 p < 1e-4
_MIN_REL_ROWS = 8        # 列间固定差 / 比至少 8 行
_MIN_AP_RUN = 5          # 等差数列至少连续 5 项

#: 统计类线索的独立组标签 —— 单组报警时会统一降档(见 audit_paper_tables)。
#: 合计矛盾与 GRIM 不可达属于「算术上不可能同时成立」,不在此列,单独一条也照报。
_STAT_GROUPS = ("数字", "精度", "尾串", "等差", "列间")
#: 设计轴列名(序号 / 剂量 / 浓度 / 时间…):本身就是等差,不参与等差与列间判定
_DESIGN_RE = re.compile(
    r"序号|编号|编码|组别|分组|年份|时间|剂量|浓度|梯度|水平|温度|重复|"
    r"(?<![A-Za-z0-9])(?:id|no|index|year|time|dose|conc|level)(?![A-Za-z0-9])",
    re.IGNORECASE)


def _issue(level: str, category: str, title: str, evidence: str,
           explain: str, suggestion: str, group: str = "") -> dict:
    return {"level": level, "category": category, "title": title,
            "evidence": evidence, "explain": explain, "suggestion": suggestion,
            "rows": [], "columns": [], "group": group}


# ── 参评台账 ──────────────────────────────────────────────────────────
#: 七族的展示名与顺序:与产品文档 0.1 / 0.2 节的两张表同构,
#: 用户看到「7 族中 3 族参评」时能一眼对上是哪三族。
FAMILIES = (
    ("F1", "末位偏好"), ("F2", "尾数重复"), ("F3", "小数位"),
    ("F4", "等差/等比"), ("F5", "列间关系"), ("F6", "合计"),
    ("F7", "GRIM 可达性"),
)

#: 每族的参评自述。各刀在自己的闸门处报到:参评 = 本次真的凑齐了判定
#: 所需的样本,无论最后有没有报警。没用 contextvar 之外的传参方式,
#: 是为了不把七把纯函数的签名全改一遍。
_LEDGER: contextvars.ContextVar = contextvars.ContextVar("_ptf_ledger")


def _note(fam: str, why: str) -> None:
    """记一笔「本族参评了」。`why` 写人话,直接进产物给用户看。"""
    led = _LEDGER.get(None)
    if led is not None:
        led["applicable"].setdefault(fam, why)


def _abstain(fam: str, why: str) -> None:
    """记一笔「本族弃权」——**弃权不是通过**,必须与「查了没事」分开说。"""
    led = _LEDGER.get(None)
    if led is not None:
        led["abstained"].setdefault(fam, why)


def _nums_in(cell: str) -> list[float]:
    return [float(m.group(1)) for m in _NUM_RE.finditer(cell or "")]


def _decimals_in(cell: str) -> list[int]:
    out = []
    for m in _NUM_RE.finditer(cell or ""):
        frac = (m.group(1).split(".") + [""])[1]
        out.append(len(frac))
    return out


def _table_numbers(tables):
    """扁平化:所有表格里的 (表号, 值, 原文本, 小数位)。"""
    flat = []
    for ti, rows in enumerate(tables, 1):
        for ri, row in enumerate(rows):
            for cell in row:
                raw = cell or ""
                for m in _NUM_RE.finditer(raw):
                    val = float(m.group(1))
                    frac = (m.group(1).split(".") + [""])[1]
                    flat.append((ti, ri, val, m.group(1), len(frac)))
    return flat


def _cols_of_table(rows, header) -> list[dict]:
    """把一张表切成「列」:只收「单元格里恰好一个数字」的纯数值列。

    带文字 / 带 ± 的格子(如 "3.47±0.82")整体跳过——那些是复合统计量,
    混进列里会让等差 / 固定差判定全是噪声。
    """
    ncol = max((len(r) for r in rows), default=0)
    out = []
    for ci in range(ncol):
        name = (header[ci] if ci < len(header) else "") or f"第{ci + 1}列"
        vals: list[tuple[float, str, int]] = []   # (值, 原文本, 小数位)
        ok = True
        for r in rows:
            if ci >= len(r):
                continue
            cell = (r[ci] or "").strip()
            if not cell or not _NUM_RE.search(cell):
                continue
            if len(_NUM_RE.findall(cell)) != 1:
                ok = False
                break
            m = _NUM_RE.search(cell)
            frac = (m.group(1).split(".") + [""])[1]
            vals.append((float(m.group(1)), m.group(1), len(frac)))
        if ok and len(vals) >= _MIN_COL_VALUES:
            out.append({"name": name.strip(), "vals": vals})
    return out


def _col_stats(vals) -> dict:
    """列的数字特征:是否粗网格、是否纯计数列。"""
    raws = [raw for _v, raw, _d in vals]
    decs = [d for _v, _raw, d in vals]
    #: 粗网格:整列都落在 0.5 的格子上(李克特 .0/.5、百分比一位小数、整数
    #: 计数)。这类列的末位与尾串不均匀是机械结果,一律不判。
    #: 注意不能写成「末位只有 ≤2 种」——一列全 .33 也满足那个条件,
    #: 而那恰恰是本工具要抓的东西。
    coarse = len(raws) >= 5 and all(
        abs(v * 2 - round(v * 2)) < 1e-9 for v, _r, _d in vals)
    #: 列级整数判定:整列都是整数(无小数)且值域宽 → 多半是计数列,不是测量列
    int_col = all(d == 0 for d in decs) and len({v for v, _r, _d in vals}) >= 5
    return {"coarse": coarse, "int_col": int_col, "dec_hist": Counter(decs)}


def _is_design_col(name: str) -> bool:
    return bool(_DESIGN_RE.search(name or ""))


def _tail2(v: float) -> int:
    """小数后两位(0..99),按数值量级取 —— 整数部分不参与。"""
    f = abs(v) - int(abs(v))
    return int(round(f * 100)) % 100


def _arith_runs(vals, *, min_run: int = _MIN_AP_RUN) -> list[tuple[int, int, float]]:
    """找列里最长的等差数列游程,返回 [(起, 止, 公差)]。

    只认「按行顺序的连续段」——交错排列的巧合不算。公差为 0(常数段)
    不在此判定,那由 datacheck 的全列常数负责。浮点比较用相对容差 1e-9。
    """
    seq = [v for v, _raw, _d in vals]
    out = []
    n = len(seq)
    i = 0
    while i < n - 2:
        d = seq[i + 1] - seq[i]
        if d == 0:
            i += 1
            continue
        tol = 1e-9 * max(abs(d), 1.0)
        j = i + 1
        while j < n and abs((seq[j] - seq[j - 1]) - d) <= tol:
            j += 1
        if j - i >= min_run:
            out.append((i, j - 1, d))
            i = j
        else:
            i += 1
    return out


def _geo_runs(vals, *, min_run: int = _MIN_AP_RUN) -> list[tuple[int, int, float]]:
    """找列里最长的等比数列游程,返回 [(起, 止, 公比)]。

    与 _arith_runs 同一套路,只是把差换成比(对数域上看就是等差)。
    比恒为 1(常数段)不判——同上交给 datacheck 的常数列。含 0 的段无意义
    (后项/前项不成立),直接跳过。
    """
    seq = [v for v, _raw, _d in vals]
    out = []
    n = len(seq)
    i = 0
    while i < n - 2:
        if seq[i] == 0:
            i += 1
            continue
        r = seq[i + 1] / seq[i]
        if abs(r - 1.0) < 1e-9:
            i += 1
            continue
        j = i + 1
        while j < n and seq[j - 1] != 0 and \
                abs(seq[j] / seq[j - 1] - r) <= 1e-6 * max(abs(r), 1.0):
            j += 1
        if j - i >= min_run:
            out.append((i, j - 1, r))
            i = j
        else:
            i += 1
    return out


def _fixed_relation(a_vals, b_vals, *, min_rows: int = _MIN_REL_ROWS):
    """两列的固定差 / 固定比判定。返回 (关系名, 常数, 符合比例, 行数) 或 None。

    容差取列内取值尺度的 1%(对差)与中位比值的 1%(对比),避免把
    「几乎相同」误判成「精确关系」。至少 95% 的行符合才算固定关系,
    且常数本身不能是 0(全等两列)或 1(纯粹同比放大 —— 先由差值分支兜）。
    """
    pairs = [(a, b) for (a, _ar, ad), (b, _br, bd) in zip(a_vals, b_vals)
             if ad == bd]        # 精度不同的两列不做比(位数不同多半是不同量纲)
    n = len(pairs)
    if n < min_rows:
        return None
    scale = max(max(abs(a) for a, _b in pairs),
                max(abs(b) for _a, b in pairs), 1e-12)
    tol = 0.01 * scale + 1e-12
    diffs = [b - a for a, b in pairs]
    diffs_sorted = sorted(diffs)
    off = diffs_sorted[n // 2]
    conform = sum(1 for d in diffs if abs(d - off) <= tol) / n
    if conform >= 0.95 and abs(off) > tol:
        return ("constant-offset", off, conform, n)
    ratios = sorted(b / a for a, b in pairs if a != 0)
    if len(ratios) >= min_rows:
        rmed = ratios[len(ratios) // 2]
        rconf = sum(1 for r in ratios
                    if abs(r - rmed) <= 0.01 * abs(rmed) + 1e-9) / len(ratios)
        if rconf >= 0.95 and abs(rmed - 1.0) > 1e-6:
            return ("constant-ratio", rmed, rconf, len(ratios))
    return None


# ──────────────────────────────────────────────────────────────────────
# 检查 1 & 2:末位偏好 + 小数尾串重复(按「列」判定,非全表汇总)
# ──────────────────────────────────────────────────────────────────────
def check_digits(tables) -> list[dict]:
    """按列做末位偏好与尾串重复。

    为什么按列而不是全表汇总(v2.32 的口径):全表汇总是多种量纲混在一起,
    「比例列天然均匀、计数列天然整数」的差异会被平均掉;按列判定才看得出
    「这一列的数字长得不像量出来的」。粗网格列(.0/.5)直接跳过。
    """
    issues = []
    n_col = n_coarse = n_thin = 0
    for ti, rows in enumerate(tables, 1):
        header = rows[0] if rows else []
        for col in _cols_of_table(rows[1:] if len(rows) > 1 else rows, header):
            st = _col_stats(col["vals"])
            if st["coarse"]:
                # 粗网格列(.0/.5):末位与尾串的不均匀是机械结果,不判。
                # 这不是「查了没事」,是这两族在这张表上开不了口。
                n_coarse += 1
                continue
            dec = [(v, raw) for v, raw, d in col["vals"] if d > 0]
            n = len(dec)
            if n < _MIN_COL_DECIMALS:
                n_thin += 1
                continue
            n_col += 1
            name = col["name"]

            # 1) 末位数字分布:卡方 p < 0.001 才谈偏好(顶层显著性再作收敛要求)
            cnt = Counter(int(raw.replace("-", "").replace(".", "")[-1])
                          for _v, raw in dec)
            counts = [cnt.get(d, 0) for d in range(10)]
            exp = n / 10.0
            chi2 = sum((c - exp) ** 2 / exp for c in counts)
            p_chi = float(_st.chi2.sf(chi2, 9))
            top_d, top_c = cnt.most_common(1)[0]
            share = top_c / n
            p_share = min(1.0, 10 * float(_st.binom.sf(top_c - 1, n, 0.1)))
            p_eff = min(p_chi, p_share)
            if p_eff < 1e-3:
                issues.append(_issue(
                    LEVEL_MID, "表格取证·末位偏好",
                    f"第 {ti} 张表「{name}」列:n={n},末位数字「{top_d}」占 {share:.0%}",
                    f"该列末位分布 χ²={chi2:.1f}(df=9),最大单元格校正后 "
                    f"p≈{p_eff:.1e};最高末位 {top_d} 出现 {top_c} 次"
                    f"(均匀时每末位期望 {exp:.1f} 次)。",
                    "手工凑数 / 随手取整常表现为末位聚集(大量 .0 / .5)。"
                    "但货币、计数、仪器粗量化也会这样——所以本项只在整列"
                    "绝大多数值同精度、且非粗网格时才判。",
                    "对照原始数据复核该列;无法提供原始数据的,在论文中"
                    "说明数值来源与舍入规则。这只是线索,不代表造假。",
                    group="数字"))

            # 2) 小数尾串重复:校正后 Poisson 尾概率,不用 (0.01)^k 这种朴素口径
            tails = [_tail2(v) for v, _raw in dec]
            tcnt = Counter(tails)
            top_tail, top_k = tcnt.most_common(1)[0]
            mu = n * 0.01
            p_point = float(_st.poisson.sf(top_k - 1, mu))
            p_corr = min(1.0, 100 * p_point)
            exp_omni = n / 100.0
            omni = [tcnt.get(t, 0) for t in range(100)]
            chi2_t = (sum((c - exp_omni) ** 2 / exp_omni for c in omni)
                      if exp_omni > 0 else 0.0)
            p_omni = float(_st.chi2.sf(chi2_t, 99))
            if p_corr < _TAIL_P_CORR and top_k >= _MIN_TAIL_COUNT:
                issues.append(_issue(
                    LEVEL_MID, "表格取证·尾数重复",
                    f"第 {ti} 张表「{name}」列:小数尾串「.{top_tail:02d}」出现 "
                    f"{top_k} 次(占 {top_k / n:.0%})",
                    f"该列 {n} 个带小数的值里,小数后两位是 .{top_tail:02d} 的有 "
                    f"{top_k} 个(均匀时每种尾数期望 {mu:.1f} 个);多重比较校正后 "
                    f"p≈{p_corr:.1e}(整体均匀性 χ²={chi2_t:.0f},df=99,p≈{p_omni:.2g})。",
                    "同一列的数值若整数部分各不相同、小数尾却反复相同,通常意味着"
                    "它们是照着同一个尾数编出来的;比值、归一化、百分比换算也会"
                    "留下类似痕迹。"
                    "(注:网上流传的 (0.01)^k 概率没做多重比较校正,偏大约几十个"
                    "数量级,本工具给的是校正后的口径。)",
                    "逐行核对这些尾数对应的数值是否各自独立算出;这只是线索,"
                    "不代表造假。",
                    group="尾串"))
    #: 参评/弃权按整份材料的**列**结算(不是按表):这两族是列级刀,
    #: 「有的列能判、有的列不能」在用户眼里就是这一族参评了。
    if n_col:
        _note("F1", f"{n_col} 列达 {_MIN_COL_DECIMALS} 值门槛,做了末位卡方")
        _note("F2", f"{n_col} 列达门槛,做了尾串集中度检验")
    else:
        why = []
        if n_coarse:
            why.append(f"{n_coarse} 列是 .0/.5 粗网格(不均匀是机械结果)")
        if n_thin:
            why.append(f"{n_thin} 列带小数的值不足 {_MIN_COL_DECIMALS} 个")
        _abstain("F1", "末位分布未参评:" + (";".join(why) or "没有可判定的数值列"))
        _abstain("F2", "尾数重复未参评:" + (";".join(why) or "没有可判定的数值列"))
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 5:混合小数位(整列同精度里混进极少数异精度值)
# ──────────────────────────────────────────────────────────────────────
def check_mixed_precision(tables) -> list[dict]:
    """一列绝大多数值保留 k 位小数,却有个别值精度不同。

    真实仪器读数的小数位是恒定的;混合精度最常见的成因是录入时漏打/多打
    一位(笔误),因此本项**恒定报最低档**,只在同列另有其它信号时才值得看。
    """
    issues = []
    n_col = n_skip = 0
    for ti, rows in enumerate(tables, 1):
        header = rows[0] if rows else []
        for col in _cols_of_table(rows[1:] if len(rows) > 1 else rows, header):
            st = _col_stats(col["vals"])
            if st["coarse"] or st["int_col"]:
                # 粗网格列与纯计数列本来就不该有小数位一说 → 本族绕开,
                # 不是「查过没问题」。
                n_skip += 1
                continue
            decs = [d for _v, _raw, d in col["vals"]]
            if len(decs) < 10:
                n_skip += 1
                continue
            n_col += 1
            hist = Counter(decs)
            modal_dec, modal_n = hist.most_common(1)[0]
            minority = len(decs) - modal_n
            modal_frac = modal_n / len(decs)
            #: 少数派必须是「少数但非零」:单个异常最可能只是笔误 → 更低分
            if not (modal_frac >= 0.9 and 0 < minority < 0.1 * len(decs)):
                continue
            odd = [raw for _v, raw, d in col["vals"] if d != modal_dec][:5]
            issues.append(_issue(
                LEVEL_LOW, "表格取证·小数位不齐",
                f"第 {ti} 张表「{col['name']}」列:{modal_n}/{len(decs)} 个值保留 "
                f"{modal_dec} 位小数,另有 {minority} 个值位数不同"
                f"（如 {'、'.join(odd)}）",
                f"该列精度分布 {dict(hist)};仪器读数与统一导出的表格小数位通常"
                f"恒定。",
                "单个异常精度绝大多数情况是录入 / 格式化笔误,不是数据问题;"
                "只有当同一列还有别的取证信号时,它才多一层意义。",
                "顺手把这一列的小数位统一到与其它行一致即可。",
                group="精度"))
    if n_col:
        _note("F3", f"{n_col} 列做了精度齐整性检查")
    else:
        _abstain("F3", f"小数位未参评:{n_skip} 列是粗网格/整数计数列,"
                       f"或不足 10 个值")
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 6:列间固定差 / 固定比(独立测量的两列不会有精确关系)
# ──────────────────────────────────────────────────────────────────────
def check_relations(tables) -> list[dict]:
    """同一张表内任意两列,若 ≥95% 的行上恒差一个常数 / 恒为同一比值。

    单位换算(固定乘数)、基线+增量列(固定差)、派生列都会天然出现固定关系,
    所以本项**只报最低档**,并在文案里先给出这些良性解释。
    """
    issues = []
    n_pair = n_tab = 0
    for ti, rows in enumerate(tables, 1):
        if len(rows) < _MIN_REL_ROWS + 1:
            continue
        header = rows[0] if rows else []
        cols = [c for c in _cols_of_table(rows[1:], header)
                if not _is_design_col(c["name"])]
        if len(cols) < 2 or len(cols) > 12:    # 列太多时两两组合爆炸且多为堆砌表
            continue
        n_tab += 1
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                n_pair += 1
                got = _fixed_relation(a["vals"], b["vals"])
                if not got:
                    continue
                kind, const, conform, n = got
                what = "相差恒为" if kind == "constant-offset" else "比值恒为"
                fmt = f"{const:+g}" if kind == "constant-offset" else f"{const:.4g}"
                issues.append(_issue(
                    LEVEL_LOW, "表格取证·列间固定关系",
                    f"第 {ti} 张表「{a['name']}」与「{b['name']}」在 "
                    f"{conform:.0%} 行上{what} {fmt}",
                    f"两列各取 {n} 个同精度数值逐行配对,{conform:.0%} 的行的"
                    f"{what.replace('恒为', '恒等于')} {fmt},"
                    f"接近完全固定的线性关系。",
                    "独立测量出来的两列几乎不可能有精确固定关系;但单位换算、"
                    "「基线 + 增量」、由同一列派生的列都会天然如此——先确认"
                    "两列是否有换算或派生关系。",
                    "核对这两列是否各自独立测得;若确有换算关系,在表注里写明。"
                    "这只是线索,不代表造假。",
                    group="列间"))
    if n_pair:
        _note("F5", f"{n_tab} 张表共 {n_pair} 对列做了固定差/比检验")
    else:
        _abstain("F5", f"列间关系未参评:没有同时具备 ≥{_MIN_REL_ROWS + 1} 行"
                       f"与 2–12 个非设计轴数值列的表")
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 7:列内精确等差数列
# ──────────────────────────────────────────────────────────────────────
def check_progression(tables) -> list[dict]:
    """某一列构成精确等差或等比数列(连续 ≥5 项公差/公比恒定)。

    剂量梯度、时间序列、等距分组本身就是等差数列,所以列名带「剂量 / 浓度 /
    年份 / 序号」等设计轴词的列直接跳过;剩下的列出现精确等差/等比,多半意味着
    这列数据是「按公式推出来的」而不是量出来的。

    等差与等比放在同一把刀里(同一个 group="等差"),因为它们指的是同一种
    情形——「这一列有公式」。等比是等差在对数域的镜像,不少伪造手法直接用
    等比生成(2/4/8/16…),漏掉它这把刀只算装了一半。
    """
    issues = []
    n_col = n_skip = 0
    for ti, rows in enumerate(tables, 1):
        header = rows[0] if rows else []
        for col in _cols_of_table(rows[1:] if len(rows) > 1 else rows, header):
            if _is_design_col(col["name"]) or _col_stats(col["vals"])["int_col"]:
                # 设计轴列(序号/剂量/年份)本身就是等差,纯计数列不适用。
                n_skip += 1
                continue
            n_col += 1
            vals = col["vals"]
            a_runs = _arith_runs(vals)
            g_runs = _geo_runs(vals)
            # 同一段既可能被两边都认出来(公差 0 已排除,但 1,2,4,8 这类
            # 小整数段在两种口径下都可能勉强成立),取跨得最长的那条。
            spans = ([(r, "arith") for r in a_runs] +
                     [(r, "geo") for r in g_runs])
            if not spans:
                continue
            (i, j, k), kind = max(spans, key=lambda x: x[0][1] - x[0][0])
            raw_span = [raw for _v, raw, _dd in vals[i:j + 1]]
            shown = "、".join(raw_span[:8]) + ("…" if len(raw_span) > 8 else "")
            if kind == "arith":
                issues.append(_issue(
                    LEVEL_LOW, "表格取证·等差数列列",
                    f"第 {ti} 张表「{col['name']}」列:第 {i + 1}–{j + 1} 项构成公差 "
                    f"{k:+g} 的等差数列",
                    f"这 {j - i + 1} 个值依次相差恒为 {k:+g}({shown})。",
                    "精确等差通常出现在人工构造的数字里;真实的测量数据会有随机"
                    "波动。等差也可能是「按公式算出的理论值」列。",
                    "确认这列是实测数据还是理论推导值;若是推导值,在表注中说明。"
                    "这只是线索,不代表造假。",
                    group="等差"))
            else:
                # 等比段:说明里点名「逐项乘以同一倍数」,比给一个公比数字直观
                issues.append(_issue(
                    LEVEL_LOW, "表格取证·等比数列列",
                    f"第 {ti} 张表「{col['name']}」列:第 {i + 1}–{j + 1} 项构成公比 "
                    f"{k:g} 的等比数列",
                    f"这 {j - i + 1} 个值每一项都是上一项的 {k:g} 倍({shown})。",
                    "真实的测量数据不会逐项严格乘同一个倍数;这通常是按公式"
                    "递推生成的结果(如按比例递增的预设值、理论级数)。",
                    "确认这列是实测数据还是按公式递推的值;若是递推值,在表注中"
                    "说明生成方式。这只是线索,不代表造假。",
                    group="等差"))
    if n_col:
        _note("F4", f"{n_col} 列做了等差/等比游程扫描")
    else:
        _abstain("F4", f"等差/等比未参评:{n_skip} 列是设计轴列(序号/剂量/"
                       f"年份)或整数计数列")
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 3:行/列合计一致性(算术硬矛盾,不受独立组降噪影响)
# ──────────────────────────────────────────────────────────────────────
def check_totals(tables) -> list[dict]:
    issues = []
    n_pair = 0
    for ti, rows in enumerate(tables, 1):
        flat_cnt = sum(len(_nums_in(c)) for r in rows for c in r)
        if flat_cnt < _MIN_CELLS:
            continue
        # 行方向:某行含"合计"单元格 → 其余数字之和应等于它
        for ri, row in enumerate(rows):
            nums_per_cell = [_nums_in(c) for c in row]
            total_cells = [i for i, c in enumerate(row) if _TOTAL_RE.search(c or "")]
            if not total_cells:
                continue
            for ti_cell in total_cells:
                tv = _nums_in(row[ti_cell])
                if len(tv) != 1:
                    continue
                others = [v for i, per in enumerate(nums_per_cell)
                          if i != ti_cell for v in per]
                if len(others) < 2:
                    continue
                n_pair += 1
                s = sum(others)
                if abs(s - tv[0]) > max(0.05, 5e-3 * max(abs(s), 1)):
                    issues.append(_issue(
                        LEVEL_MID, "表格取证·合计矛盾",
                        f"第 {ti} 张表「{row[ti_cell][:12]}」行:分项之和 {s:.2f} ≠ "
                        f"合计 {tv[0]:g}",
                        f"该行其余数字之和为 {s:.4f},与写出的合计 {tv[0]:g} 不符"
                        f"(差 {abs(s-tv[0]):.4f}),超过舍入可解释的范围。",
                        "合计与分项不相等是硬矛盾——要么分项抄错,要么合计"
                        "是另一次计算的结果。",
                        "回到原始数据重算该行;若分项有缺漏(如「其他」未列),"
                        "在表注中说明。"))
        # 列方向:最后一行是"合计"行 → 每列上方的和应等于它
        if len(rows) >= 3:
            last = rows[-1]
            if any(_TOTAL_RE.search(c or "") for c in last[:2]):
                ncol = max(len(r) for r in rows)
                for ci in range(1, ncol):
                    col_vals = []
                    for r in rows[:-1]:
                        if ci < len(r):
                            vs = _nums_in(r[ci])
                            if len(vs) == 1:
                                col_vals.append(vs[0])
                    cv = _nums_in(last[ci]) if ci < len(last) else []
                    if len(cv) == 1 and len(col_vals) >= 2:
                        s = sum(col_vals)
                        if abs(s - cv[0]) > max(0.05, 5e-3 * max(abs(s), 1)):
                            issues.append(_issue(
                                LEVEL_LOW, "表格取证·合计矛盾",
                                f"第 {ti} 张表第 {ci+1} 列:上方之和 {s:.2f} ≠ "
                                f"合计行 {cv[0]:g}",
                                f"该列上方 {len(col_vals)} 个数之和为 {s:.4f},"
                                f"合计行写的是 {cv[0]:g}。",
                                "列合计不符最常见的原因是漏掉一行或把百分数"
                                "与计数混加。",
                                "核对列内每一行是否都应计入合计。"))
    if n_pair:
        _note("F6", f"检出 {n_pair} 处「合计 vs 分项」可核对关系")
    else:
        _abstain("F6", "合计未参评:表里没有「合计/总计」单元格,"
                       "或合计行的可加数字不足 2 个")
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 4:GRIM 表格版(n × 均值 可达性)
# ──────────────────────────────────────────────────────────────────────
def check_grim(tables) -> list[dict]:
    issues = []
    n_row = n_abstain = 0
    for ti, rows in enumerate(tables, 1):
        header = rows[0] if rows else []
        for ri, row in enumerate(rows[1:], start=2):
            # n 在哪:本行或表头里的"n/例数/样本量"列
            n_val = None
            for ci, cell in enumerate(row):
                hdr_is_n = (ci < len(header)
                            and bool(_N_RE.search(header[ci] or "")))
                cell_is_n = bool(_N_RE.search(cell or ""))
                if not (hdr_is_n or cell_is_n):
                    continue
                m = re.search(r"[=：:]\s*(\d+)", cell or "")
                own = _nums_in(cell)
                if m:                       # 形态一:"n=30" 自带数字
                    n_val = int(m.group(1))
                elif len(own) == 1:         # 形态二:表头 n 列的 "30"
                    n_val = int(own[0])
                elif cell_is_n and ci + 1 < len(row):  # 形态三:"n"|"30"
                    nxt = _nums_in(row[ci + 1])
                    if len(nxt) == 1:
                        n_val = int(nxt[0])
                if n_val is not None:
                    break
            if not n_val or n_val < 3 or n_val > 100000:
                continue
            # 均值:本行的 M±SD 或 均值列小数
            means = []
            for ci, cell in enumerate(row):
                msd = list(_MSD_RE.finditer(cell or ""))
                for m in msd:
                    #: 小数位按「小数点后真位数」取——"3" 是 0 位,不是 1 位。
                    #: 早先用 split(".")[-1] 会把整数均值算成 1 位小数,容差
                    #: 跟着放大 10 倍,整列判定就松了(同一模块 _decimals_in
                    #: 一直是正确写法,两处口径必须一致)。
                    frac = (m.group(1).split(".") + [""])[1]
                    means.append((float(m.group(1)), len(frac), m.group(0)))
                if msd:
                    # 本格是 M±SD 形态:SD 那半不是均值,绝不能进 GRIM
                    continue
                if ci > 0 and any(_MEAN_HDR_RE.search(h or "") for h in header[ci:ci+1]):
                    for v, d in zip(_nums_in(cell), _decimals_in(cell)):
                        if d > 0:
                            means.append((v, d, str(v)))
            for mean, dec, raw in means:
                if dec > 3:
                    continue
                n_row += 1
                #: 与 datacheck 同一口径:报告位数下的舍入容差 + 大 n 弃权。
                #: 三态里的「弃权」在这里不产生任何卡片——大 N 下 GRIM 本来
                #: 就恒真,报「通过」等于把空转冒充成核验。
                verdict = grim_verdict(mean, n_val, items=1, decimals=dec)
                if verdict == GRIM_ABSTAIN:
                    n_abstain += 1
                    continue
                if verdict != GRIM_IMPOSSIBLE:
                    continue
                issues.append(_issue(
                    LEVEL_MID, "表格取证·GRIM 可达性",
                    f"第 {ti} 张表:n={n_val} 与均值 {raw} 组合不可达",
                    f"n × 均值 = {n_val} × {mean} = {n_val * mean:.6f},"
                    f"落在任何合法取值的舍入区间之外——在整数计分数据下,"
                    f"n={n_val} 的样本**算不出**保留 {dec} 位小数的均值 "
                    f"{mean}(已按报告位数计入 ±{0.5 * 10 ** -dec:g} 的舍入容差)。",
                    "GRIM 核查(Allular/Heathers 2017)是学术取证常用线索:"
                    "这类矛盾通常意味着均值来自另一次计算、n 写错,"
                    "或数字系拼凑。",
                    "回查该表的 n 与均值是否来自同一次统计;"
                    "这只是线索,不代表造假。"))
                break  # 每行只报第一个
    if n_row:
        extra = f",其中 {n_abstain} 个因 n 过大而无信息量" if n_abstain else ""
        _note("F7", f"{n_row} 个「n + 均值」组合做了可达性检验{extra}")
    else:
        _abstain("F7", "GRIM 未参评:表里没找到「n / 例数 / 样本量」列,"
                       "或均值未标注报几位小数")
    return issues


def audit_paper_tables(tables) -> dict:
    """主入口:对论文 docx 表格做取证,返回 {ok, tables, numeric_cells, issues,
    applicable, abstained, summary, note}。

    七把刀跑完后统一过一遍「独立组降噪」:统计类线索只在**跨类印证**时才
    保留中档,单类独响一律降为最低档并注明「未经交叉印证」——这样做的原因
    是单条弱线索(某一列末位偏多、某两列像有换算关系)在真实论文里出现得
    太频繁,堆到用户面前只会变成噪声,反而不如诚实地说「这条还没被印证」。
    算术硬矛盾(合计不符)不参与降噪:那是数学上不可能同时成立的两件事。

    v2.38 起额外报「参评台账」:`applicable` / `abstained` 按族级 7 标签给出,
    与产品文档 0.1 / 0.2 节的表格同构。**弃权不是通过**——n 太大时 GRIM 恒真、
    表里没有合计单元格时合计刀无从下手,这些必须说成「本族没开口」而不是
    「查过没问题」,否则用户会把空转当成体检通过。
    """
    tables = tables or []
    flat = _table_numbers(tables)
    issues = []
    led = {"applicable": {}, "abstained": {}}
    token = _LEDGER.set(led)
    try:
        for fn in (check_digits, check_mixed_precision, check_relations,
                   check_progression, check_totals, check_grim):
            try:
                issues += fn(tables)
            except Exception:  # noqa: BLE001 — 单把刀出错不影响其它刀
                pass
    finally:
        _LEDGER.reset(token)

    # ── 独立组降噪:只有一类统计信号时全部降档 ──
    stat = [it for it in issues if it.get("group") in _STAT_GROUPS]
    if len({it["group"] for it in stat}) == 1:
        for it in stat:
            it["level"] = LEVEL_LOW
            it["explain"] += ("\n(本次表格取证里只有这一类信号报警,未经其它"
                              "类别交叉印证,已按最低档提示。)")
    issues.sort(key=lambda it: (0 if it.get("level") == LEVEL_MID else 1,
                                it.get("category", "")))

    applicable = {f: led["applicable"][f] for f, _cn in FAMILIES
                  if f in led["applicable"]}
    abstained = {f: led["abstained"][f] for f, _cn in FAMILIES
                 if f in led["abstained"]}
    #: 族 → 在 issues 里对应的 group 标签。F1/F2 的卡片 group 并不相同
    #: (末位偏好="数字"、尾数重复="尾串"),所以这两族改按 category 后缀精确
    #: 匹配——早先按 group 合并映射,只报其一也会把另一族一起算成"报警"。
    #: 注意:**参评计数**仍按 group 合并看(F1/F2 走同一道闸门,参评集合恒等),
    #: 只有报警计数需要逐族区分,两者口径不同,不可一起改。
    _FAM_GROUP = {"F3": "精度", "F4": "等差", "F5": "列间"}
    hit = {it["group"] for it in stat}
    cats = {it.get("category", "") for it in issues}
    alarmed = [f for f, _cn in FAMILIES
               if _FAM_GROUP.get(f) in hit
               or (f == "F1" and any(c.endswith("末位偏好") for c in cats))
               or (f == "F2" and any(c.endswith("尾数重复") for c in cats))
               or (f == "F6" and any(c.endswith("合计矛盾") for c in cats))
               or (f == "F7" and any(c.endswith("GRIM 可达性") for c in cats))]
    if not tables:
        summary = "本次没有可供核查的结构化表格。"
    elif not applicable:
        summary = ("7 族中 0 族参评——表格里没有凑齐任何一把刀的判定条件,"
                   "本次取证没有信息量(这不等于「核查通过」)。")
    else:
        summary = f"7 族中 {len(applicable)} 族参评:{_fam_names(applicable)}。"
        summary += (f"其中 {len(alarmed)} 族报警:"
                    f"{_fam_names({f: '' for f in alarmed})}。" if alarmed
                    else "这 7 族里已开口的都没有发现异常。")
    if abstained:
        summary += f"另有 {len(abstained)} 族未开口:{_fam_names(abstained)}。"

    return {
        "ok": True,
        "tables": len(tables),
        "numeric_cells": len(flat),
        "decimal_numbers": sum(1 for *_x, d in flat if d > 0),
        "groups_hit": sorted(hit),
        "applicable": applicable,
        "abstained": abstained,
        "summary": summary,
        "issues": issues,
        "note": "表格取证只覆盖 docx 结构化表格;每条发现都只是线索,"
                "不代表造假。阈值刻意保守(按「列」判定:列内至少 20 个带"
                "小数的值才做末位/尾串,概率均为多重比较校正后的口径);"
                "只有一类信号时自动降档,避免单条弱线索吓人。"
                "「未开口」的族是本次条件不够、没有信息量,不等于通过。",
    }


def _fam_names(fams) -> str:
    """族号 → 「F1 末位偏好 / F3 小数位」这种人话。"""
    cn = dict(FAMILIES)
    return " / ".join(f"{f} {cn[f]}" for f, _c in FAMILIES if f in fams)
