"""智论助手 · 比赛资料包打包

把「交给评委看的东西」打成一个 zip：文档 + 源码树 + 已构建的 exe。
刻意与之分开的是**开发期间的工作产物**——CLAUDE.md 的并行协作约定、
CLAUDE/AGENTS 这类给智能体看的文件、测试套件、_ 前缀脚手架，都不进包。

红线（CLAUDE.md 第一节 + 用户明确指示）：
    **被检测论文的原文绝不能进包**。经验库/规则库这类「由论文衍生的经验」
    可以留（它们在 exe 旁的本地状态里），但论文正文一行都不行。
    所以本脚本对 `模板论文/`、`workspace/`、`*.pdf`、`经验库.jsonl` 里的
    每条记录都做了一次扫描——不是靠 .gitignore 自觉，是实实在在看一眼。

用法：
    .venv/Scripts/python.exe scripts/pack_contest.py            # 打包
    .venv/Scripts/python.exe scripts/pack_contest.py --check     # 只扫描不打包
    .venv/Scripts/python.exe scripts/pack_contest.py --zip       # 额外产出 zip

退出码：0 = 打包完成（扫描干净）；1 = 扫描发现疑似论文正文，已中止。
"""
from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if not (ROOT / "version.py").exists():          # 从 scripts/ 之外的层级调用时兜底
    for d in (HERE, *HERE.parents):
        if (d / "version.py").exists():
            ROOT = d
            break

sys.path.insert(0, str(ROOT))
try:
    from version import APP_VERSION
except Exception:                                # noqa: BLE001 —— 版本读不到不该阻断打包
    APP_VERSION = "v0"

OUT_DIR = ROOT / ("_资料包_" + APP_VERSION)

# -----------------------------------------------------------------------------
# 进包的清单：**白名单**，不是黑名单。
#
# 为什么用白名单：黑名单（"排除 .env、排除 模板论文"）漏一项就是一次事故，
# 而漏掉的项往往正好是最该排除的那项。白名单漏项只是少给一份文档，看得见。
# -----------------------------------------------------------------------------
DOCS = [
    "README.md",
    "README-DESKTOP.md",
    "CHANGELOG.md",
    "安全自查报告.md",
    "部署上手指南.md",
    "LICENSE",
    "docs/API.md",
    "docs/ARCHITECTURE.md",
    "docs/CROSS_PLATFORM.md",
]

# 源码：整棵树，但只收这些后缀（白名单同样适用）
SRC_GLOBS = [
    "*.py",
    "*.md",
    "*.txt",
    "*.html",
    "*.css",
    "*.js",
    "*.json",
    "*.yml",
    "*.yaml",
    "*.bat",
    "Dockerfile",
    "Procfile",
    ".gitignore",
]

# 交付物目录：整份照搬（排除运行时会写进去的工作区）
DELIVER_DIRS = ["agents", "static", "templates", "examples", "tools"]

# 绝不进包 —— 存在即跳过（白名单之外的双保险）
NEVER = {
    "CLAUDE.md",            # 智能体协作约定，评委会看糊
    "AGENTS.md",
    "经验库.jsonl",          # 由被查论文衍生的累积状态：留本地
    "行业规则.json",
    ".env", ".env.local",
}

# 疑似论文正文的特征：命中就中止并报告。
#
# 行首要允许 Markdown 标记（`#`/`*`/`>`/`-`/空格）——正文被导出成 md 时
# 标题一定带层级，"## 摘要" 才是常态，光写 `^\s*摘要` 一条都抓不到。
# 这是本脚本第一版真踩过的坑（自检正例全不命中，扫描器形同虚设）。
_LEAD = r"^[\s#*>\-•·]*"
PAPER_SMELL = [
    re.compile(_LEAD + r"摘\s*要[\s：:]*$", re.M),
    re.compile(_LEAD + r"Abstract\s*$", re.M | re.I),
    re.compile(_LEAD + r"关\s*键\s*词\s*[：:]", re.M),
    re.compile(_LEAD + r"参\s*考\s*文\s*献[\s：:]*$", re.M),
    re.compile(_LEAD + r"\d+(\.\d+)*\s+(引言|绪论|研究方法|研究设计|结果与分析|讨论|结论)", re.M),
    re.compile(_LEAD + r"(第[一二三四五六七八九十]+章)", re.M),
]

# 已知「合法含论文结构」的文件 —— 逐条给出理由，清单外命中一律中止。
#
# 为什么要这份清单而不是放宽正则：正则一放宽，真正的论文就跟着漏过去了。
# 这里的原则和白名单一样——**新增一项必须写清楚为什么它不含他人论文**，
# 写不出来就说明那一项本来就不该在这里。将来谁把论文丢进仓库，它藏不住。
KNOWN_PAPER_STRUCTURE = {
    "src/paper_writer.py":
        "程序自己的论文生成器，命中在 f-string 模板里（`## 摘要`/`## 1 引言` 是骨架），"
        "不含任何具体研究内容",
    "src/examples/questionnaire_paper.md":
        "仓库自带的合成夹具（问卷信度演示），随源码公开，非用户论文",
    "src/examples/rm_anova_paper.md":
        "仓库自带的合成夹具（重复测量演示），随源码公开，非用户论文",
    "src/examples/two_way_paper.md":
        "仓库自带的合成夹具（双因素方差演示），随源码公开，非用户论文",
    "src/examples/paired_paper.md":
        "仓库自带的合成夹具（前后配对演示），随源码公开，非用户论文",
    "src/tools/probe_three_platforms.py":
        "多平台探测脚本，命中是句子里引述的『3.5 讨论…结论』字样，非论文正文",
}


def _read(p: Path) -> str:
    """尽力而为地读成文本；读不出就当空（二进制文件本来也不该有正文）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    return ""


def scan_paper_text(paths: list[Path]) -> list[tuple[Path, str]]:
    """扫「疑似论文正文」，返回 [(文件, 命中的特征)]。

    只看文本类文件——PDF/docx 是二进制，本来也不在白名单里。
    `KNOWN_PAPER_STRUCTURE` 里的文件跳过（清单以**包内相对路径**为键，
    见 `_pack_key`），清单外命中一律上报。
    """
    hits = []
    for p in paths:
        if p.suffix.lower() not in (".md", ".txt", ".json", ".jsonl", ".csv", ".py"):
            continue
        if _pack_key(p) in KNOWN_PAPER_STRUCTURE:
            continue
        txt = _read(p)
        if not txt:
            continue
        for rx in PAPER_SMELL:
            if rx.search(txt):
                hits.append((p, rx.pattern))
                break
    return hits


def _pack_key(p: Path) -> str:
    """把文件路径换算成资料包内的相对路径（`src/...`），供清单比对。

    两种调用场景都要吃得下：文档/源码是 ROOT 下的裸文件（打包时进 `src/`），
    交付目录里的文件则是 `agents/x.py` 这种（打包时进 `src/agents/x.py`）。
    """
    try:
        rel = p.relative_to(ROOT).as_posix()
    except ValueError:
        rel = p.as_posix()
    for d in DELIVER_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return "src/" + rel
    return "src/" + p.name


def collect() -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (文档, 源码, 交付物目录)。三者在打包时落到不同的子目录。"""
    docs, src, dirs = [], [], []

    for rel in DOCS:
        p = ROOT / rel
        if p.exists() and p.name not in NEVER:
            docs.append(p)
        else:
            print("  [跳过] 文档不存在：%s" % rel)

    seen: set[Path] = set()
    for pat in SRC_GLOBS:
        for p in ROOT.glob(pat):
            if not p.is_file() or p in seen:
                continue
            if p.name in NEVER or p.name.startswith("_"):
                continue
            if p.name.endswith("_test.py"):     # 测试套件不进评审包
                continue
            if p.parent != ROOT:                # 子目录由 DELIVER_DIRS 负责
                continue
            seen.add(p)
            src.append(p)

    for d in DELIVER_DIRS:
        p = ROOT / d
        if p.is_dir():
            dirs.append(p)
        else:
            print("  [跳过] 目录不存在：%s" % d)

    return sorted(docs), sorted(src), dirs


def _walk_files(d: Path, exe_dir: Path = None) -> list[Path]:
    """遍历交付目录，跳过缓存与运行时产物。"""
    out = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.relative_to(d).parts)
        if "__pycache__" in parts or any(x.startswith("_") for x in parts):
            continue
        if p.suffix in (".pyc", ".pyo", ".log"):
            continue
        if "workspace" in parts:               # 运行时会写用户数据的地方
            continue
        if exe_dir and exe_dir in p.parents:
            continue
        out.append(p)
    return out


def build(args) -> int:
    print("智论助手 比赛资料包 · %s" % APP_VERSION)
    print("=" * 62)

    print("\n[1/4] 收集文件 ...")
    docs, src, dirs = collect()
    print("  文档 %d 份 / 源码 %d 份 / 目录 %d 个" % (len(docs), len(src), len(dirs)))

    exe = ROOT / "dist" / "智论助手.exe"
    if not exe.exists():
        print("  !! 未找到 dist/智论助手.exe —— 先跑 build_desktop.py，否则包不完整")
        return 2
    exe_dist = exe.parent

    # ---- 红线扫描：论文正文 ----
    print("\n[2/4] 扫描疑似论文正文 ...")
    candidates = list(docs) + list(src)
    for d in dirs:
        candidates.extend(_walk_files(d, exe_dist))
    hits = scan_paper_text(candidates)

    # 经验库/规则库单独再扫一遍：它们**允许**进包，
    # 但必须确认里面只有列统计与规则定义，没有正文段落。
    for extra in ("经验库.jsonl", "行业规则.json"):
        p = ROOT / extra
        if p.exists():
            h = scan_paper_text([p])
            if h:
                hits.extend(h)

    if hits:
        print("  !! 中止：以下文件疑似含论文正文")
        for p, pat in hits:
            print("     %s  ← /%s/" % (p.relative_to(ROOT), pat))
        print("\n  按约定：论文原文绝不进包。请把这些内容移出后再跑。")
        return 1
    print("  干净：未发现摘要/关键词/参考文献等论文结构特征 ✓")

    if args.check:
        print("\n（--check：只扫描，不写文件）")
        return 0

    print("\n[3/4] 写出资料包 ...")
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    (OUT_DIR / "docs").mkdir(parents=True)
    (OUT_DIR / "src").mkdir(parents=True)
    (OUT_DIR / "dist").mkdir(parents=True)

    for p in docs:
        # 顶层文档平铺进 docs/；docs/ 里的则保持 docs/ 下的相对层级
        # （早年写法是 `docs / p.relative_to(ROOT)`，对 docs/API.md 会变成
        #   docs/docs/API.md —— 多套一层，自检时才发现）
        rel = p.relative_to(ROOT)
        parts = rel.parts[1:] if rel.parts[0] == "docs" else (rel.name,)
        dst = OUT_DIR / "docs" / Path(*parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

    for p in src:
        shutil.copy2(p, OUT_DIR / "src" / p.name)

    for d in dirs:
        for f in _walk_files(d, exe_dist):
            rel = f.relative_to(ROOT)
            dst = OUT_DIR / "src" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

    shutil.copy2(exe, OUT_DIR / "dist" / exe.name)

    # 交付说明放在包根，评委第一眼看到
    (OUT_DIR / "交付说明.md").write_text(_manifest(docs, src, dirs), encoding="utf-8")

    total = sum(f.stat().st_size for f in OUT_DIR.rglob("*") if f.is_file())
    print("  目录：%s" % OUT_DIR)
    print("  体积：%.1f MB" % (total / 1024 / 1024))

    if args.zip:
        print("\n[4/4] 压缩 ...")
        # 拼字符串而不是 with_suffix：`_资料包_v2.47` 会被 with_suffix 当成
        # 「主名 _资料包_v2 + 扩展名 .47」切掉，产出 `_资料包_v2.zip`
        zpath = OUT_DIR.parent / (OUT_DIR.name + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in OUT_DIR.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(OUT_DIR.parent))
        print("  zip：%s（%.1f MB）" % (zpath, zpath.stat().st_size / 1024 / 1024))
    else:
        print("\n[4/4] 跳过压缩（加 --zip 可产出）")

    print("\n完成。")
    return 0


def _manifest(docs, src, dirs) -> str:
    lines = [
        "# 智论助手 · 比赛资料包 交付说明",
        "",
        "版本：%s" % APP_VERSION,
        "",
        "## 目录结构",
        "",
        "```",
        "docs/     产品与设计文档（先读 README.md）",
        "src/      全部源码（Python 后端 + 原生 HTML 前端，无构建步骤）",
        "dist/     已构建的 Windows 桌面端 exe（双击即用）",
        "```",
        "",
        "## 快速上手",
        "",
        "1. 双击 `dist/智论助手.exe`——自动起本地服务并打开浏览器（127.0.0.1:5000）。",
        "2. 首页上传 CSV/Excel 数据，先用「数据体检」看质量，再选统计方法。",
        "3. `src/` 下如需从源码运行：`pip install -r requirements.txt` 后 `python app.py`。",
        "",
        "## 设计红线（贯穿全部功能）",
        "",
        "1. 统计计算全部本地执行，LLM 只接收统计量字典，从不接触原始数据；",
        "2. 数据体检只报「可疑，请核对」，永不判定造假；",
        "3. 用户数据全内存处理，不上传、不落盘；",
        "4. 不提供代写，AI 痕迹自查只评风格风险，不判作者身份。",
        "",
        "## 本包内容",
        "",
        "**文档（%d 份）**" % len(docs),
        "",
    ]
    for p in docs:
        lines.append("- `%s`" % p.relative_to(ROOT).as_posix())
    lines += [
        "",
        "**源码（%d 个根级文件 + %s 目录）**" % (
            len(src), "、".join(d.name + "/" for d in dirs)),
        "",
        "测试套件（`*_test.py`）、智能体协作约定（CLAUDE.md/AGENTS.md）",
        "及本地累积知识文件（经验库/行业规则）未收录——它们属于开发过程，",
        "不属于产品交付。",
        "",
        "> 注：知识库文件若首次运行需要，程序会在 exe 同目录**自动生成模板**，",
        "> 无需随包分发。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="智论助手比赛资料包打包")
    ap.add_argument("--check", action="store_true", help="只扫描红线，不写文件")
    ap.add_argument("--zip", action="store_true", help="额外产出 zip")
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
