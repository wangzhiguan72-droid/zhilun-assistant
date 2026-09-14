#!/usr/bin/env python3
"""⑦ CLI 跨端 —— 同一套 Flask 后端，命令行外壳
================================================
后端零改：直接 import app 里的函数，不启 Flask 服务。
支持：
  - analyze : 运行统计方法，输出 JSON / Markdown
  - audit   : 论文排查，输出 Markdown 报告
  - simulate: 生成模拟数据，输出 CSV
  - methods : 列出支持的方法

用法：
  python cli.py analyze data.csv --method independent_t --group gender --value score
  python cli.py analyze data.csv --method independent_t --group gender --value score -f json
  python cli.py audit paper.pdf data.csv [--directive "只看T检验"] [--llm]
  python cli.py simulate independent_t --seed 42 --n 30
  python cli.py methods

设计原则（护栏）：
  1. CLI 不写文件、只打印（除非 --output 指定）
  2. 数据全内存，不落盘
  3. 与网页端走同一套纯函数（call_method / build_audit_report），结果一致
  4. 红线自检同样适用：CLI 不是绕过学术不端约束的后门
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd


def _load_df(csv_path: str) -> pd.DataFrame:
    """加载 CSV（自动尝试 utf-8 / gbk）。"""
    p = Path(csv_path)
    if not p.exists():
        print(f"错误：文件不存在 {csv_path}", file=sys.stderr)
        sys.exit(1)
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(p, encoding=enc)
        except UnicodeDecodeError:
            continue
    print("错误：CSV 文件编码无法识别，请尝试另存为 UTF-8 或 GBK。", file=sys.stderr)
    sys.exit(1)


def cmd_analyze(args: argparse.Namespace) -> None:
    """运行统计方法，输出 JSON / Markdown。"""
    from methods_registry import call_method, method_keys, get_spec

    df = _load_df(args.data)
    method = args.method

    if method not in method_keys():
        print(f"错误：未知方法 {method}。当前支持：{' / '.join(method_keys())}", file=sys.stderr)
        sys.exit(1)

    payload = {
        "method": method,
        "group_col": args.group,
        "value_col": args.value,
        "value_col2": args.value2,
        "x_cols": args.x_cols.split(",") if args.x_cols else None,
        "item_cols": args.item_cols.split(",") if args.item_cols else None,
    }

    try:
        result = call_method(method, df, payload)
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        # 字段缺失时，把该方法需要什么、当前该用哪个参数说清楚
        spec = get_spec(method)
        if spec is not None and getattr(spec, "fields", None):
            bits = []
            for f in spec.fields:
                tag = []
                if getattr(f, "numeric", False):
                    tag.append("数值")
                mi = getattr(f, "min_items", 1) or 1
                if mi > 1:
                    tag.append(f"≥{mi} 个")
                bits.append(f.name + (f"（{'/'.join(tag)}）" if tag else ""))
            print(f"提示：{method} 需要的字段：{', '.join(bits)}", file=sys.stderr)
            print(
                f"      当前数据列：{' / '.join(map(str, df.columns))}",
                file=sys.stderr,
            )
            print(
                "      参数对应：value_col=--value，value_col2=--value2，"
                "group_col=--group，x_cols=--x-cols，item_cols=--item-cols",
                file=sys.stderr,
            )
        elif spec is not None:
            print(
                f"      当前数据列：{' / '.join(map(str, df.columns))}",
                file=sys.stderr,
            )
        sys.exit(1)

    if args.format == "json":
        # 去掉 markdown 字段（太长），只留统计量
        out = {k: v for k, v in result.items() if k != "markdown"}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        md = result.get("markdown", "")
        print(md)

    if args.output:
        Path(args.output).write_text(
            result.get("markdown", ""), encoding="utf-8"
        )
        print(f"\n已写入 {args.output}", file=sys.stderr)


def _read_paper_text(path: Path) -> str:
    """读取论文文本，复用网页端同一个 extract_paper.read_paper_text。

    该函数只依赖 ``.filename`` 和 ``.read()`` 两个属性，这里用一个最小 shim
    模拟 Flask FileStorage，从而让 CLI 与网页端走完全相同的解析链路
    （.docx / .pdf / .txt / .md 全部支持，含加密 PDF 与扫描件报错）。
    """
    from extract_paper import read_paper_text

    class _Shim:
        def __init__(self, name: str, data: bytes) -> None:
            self.filename = name
            self._data = data

        def read(self) -> bytes:
            return self._data

    return read_paper_text(_Shim(path.name, path.read_bytes()))


def cmd_audit(args: argparse.Namespace) -> None:
    """论文排查，输出 Markdown 报告。

    与网页端 `/api/check_paper` 走同一条链路：
      paper_claims = {methods, quantities, variables, ...}
      → build_audit_report(paper_claims, df, columns, directive=...)
    唯一差别是不含 LLM 段落（CLI 默认离线；--llm 才启用）。
    """
    from app import _summarize_column
    from audit import build_audit_report, _red_line_scan as red_line_scan
    from extract_paper import extract_methods, extract_quantities, extract_variables

    paper_path = Path(args.paper)
    if not paper_path.exists():
        print(f"错误：论文文件不存在 {args.paper}", file=sys.stderr)
        sys.exit(1)

    try:
        paper_text = _read_paper_text(paper_path)
    except (ValueError, RuntimeError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    if not paper_text.strip():
        print(
            "错误：未能从论文文件中提取到文本（PDF 可能是扫描件，需先 OCR）。",
            file=sys.stderr,
        )
        sys.exit(1)
    # 指令红线自检：与网页端同一条约束，CLI 不是后门
    directive = (args.directive or "").strip()
    red_line = red_line_scan(directive)
    if red_line.get("blocked"):
        print("错误：该指令涉及学术不端，本工具无法执行。", file=sys.stderr)
        for hit in red_line.get("hits") or []:
            # hits 在 v1.5+ 是「说明字符串」列表（早期版本为 dict）
            if isinstance(hit, dict):
                cat = hit.get("category") or hit.get("kind") or ""
                txt = hit.get("phrase") or hit.get("text") or hit.get("note") or ""
                print(f"  - [{cat}] {txt}".rstrip(" []"), file=sys.stderr)
            else:
                print(f"  - {hit}", file=sys.stderr)
        usage = (red_line.get("correct_usage") or "").strip()
        if usage:
            print("\n" + usage, file=sys.stderr)
        sys.exit(1)

    df = _load_df(args.data)
    columns = [_summarize_column(df[c]) for c in df.columns]

    methods = extract_methods(paper_text)
    quantities = extract_quantities(paper_text)
    variables = extract_variables(paper_text)
    if not methods and not quantities:
        variables = [
            v for v in variables if set(v.get("sources", [])) - {"after_keyword"}
        ]
    paper_claims = {
        "methods": methods,
        "quantities": quantities,
        "variables": variables,
        # v2.12：表格交叉核查（table_check）需要全文解析 Markdown/管道表
        "raw_text": paper_text,
        "raw_text_excerpt": paper_text[:1500],
        "raw_text_length": len(paper_text),
    }

    result = build_audit_report(paper_claims, df, columns, directive=directive)
    md = result.get("markdown", "")

    if args.llm:
        from llm_audit import audit_paper_with_llm

        section, err, _meta = audit_paper_with_llm(
            paper_text, result, directive, force=args.llm_force
        )
        if section:
            md = md + "\n\n" + section
        elif err:
            print(f"（AI 深度审计不可用，已降级为纯规则报告：{err}）", file=sys.stderr)

    print(md)

    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"\n已写入 {args.output}", file=sys.stderr)


def cmd_simulate(args: argparse.Namespace) -> None:
    """生成模拟数据，输出 CSV。"""
    from simulate import generate, available_methods

    method = args.method
    if method not in available_methods():
        print(f"错误：未知方法 {method}。当前支持：{' / '.join(available_methods())}", file=sys.stderr)
        sys.exit(1)

    # v2.17：generate() 对非法入参抛 ValueError（n<3 / NaN 等）。CLI 要把它
    # 变成一行可读的错误 + 退出码 1，而不是甩一屏 traceback 给用户。
    try:
        df, truth = generate(
            method=method,
            effect_size=args.effect_size,
            n_per_group=args.n,
            seed=args.seed,
            noise=args.noise,
        )
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        df.to_csv(args.output, index=False)
        print(f"已写入 {args.output}", file=sys.stderr)
        print(f"真值：{json.dumps(truth, ensure_ascii=False, indent=2, default=str)}")
    else:
        # 打印到 stdout（可被管道接收）
        print(df.to_csv(index=False), end="")
        if args.verbose:
            print(f"\n# 真值：{json.dumps(truth, ensure_ascii=False)}", file=sys.stderr)


def _render_datacheck_md(filename: str, report: dict[str, Any]) -> str:
    """渲染体检 Markdown（复用 `datacheck.render_markdown`，保证 CLI 与流水线同一文案）。"""
    from datacheck import render_markdown

    return render_markdown(report, filename=filename)


def cmd_check_data(args: argparse.Namespace) -> None:
    """数据体检（产品入口）—— 与网页端 `/api/datacheck` 共用同一个 `datacheck` 模块。

    **零 LLM、零外部依赖、不落盘**；只报「可疑，请核对」，永不判定造假。

    退出码：发现高优先级问题时返回 1（便于脚本 / CI 门控），否则 0；
    加 `--exit-zero` 可强制始终返回 0。
    """
    from datacheck import run_datacheck

    df = _load_df(args.data)
    try:
        report = run_datacheck(df)
    except Exception as e:  # noqa: BLE001 - 体检失败给出可读错误，不吐堆栈
        print(f"错误：数据体检失败 {e}", file=sys.stderr)
        sys.exit(2)

    s = report.get("summary", {}) or {}
    text = (json.dumps(report, ensure_ascii=False, indent=2)
            if args.format == "json"
            else _render_datacheck_md(Path(args.data).name, report))

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已写入：{args.output}")
    else:
        print(text)

    if not args.exit_zero and s.get("high"):
        print(f"\n⚠️ 体检未通过：发现 {s['high']} 处高优先级问题", file=sys.stderr)
        sys.exit(1)


def cmd_methods(args: argparse.Namespace) -> None:
    """列出支持的方法。"""
    from methods_registry import method_keys, method_labels

    labels = method_labels()
    for key in method_keys():
        label = labels.get(key, key)
        print(f"{key:30s} {label}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zhilun",
        description="智论助手 CLI —— 同一套后端，命令行外壳",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = sub.add_parser("analyze", help="运行统计方法")
    p_analyze.add_argument("data", help="CSV 数据文件路径")
    p_analyze.add_argument("--method", "-m", required=True, help="方法 key（见 methods 子命令）")
    p_analyze.add_argument("--group", "-g", dest="group", help="分组列名")
    p_analyze.add_argument("--value", "-v", dest="value", help="因变量列名")
    p_analyze.add_argument("--value2", dest="value2", help="第二因变量列（相关/配对/双因素）")
    p_analyze.add_argument("--x-cols", dest="x_cols", help="自变量列（逗号分隔，回归用）")
    p_analyze.add_argument("--item-cols", dest="item_cols", help="题项列（逗号分隔，信度用）")
    p_analyze.add_argument("--format", "-f", choices=["json", "markdown"], default="markdown", help="输出格式")
    p_analyze.add_argument("--output", "-o", help="输出文件路径（可选）")
    p_analyze.set_defaults(func=cmd_analyze)

    # audit
    p_audit = sub.add_parser("audit", help="论文排查")
    p_audit.add_argument("paper", help="论文文件路径（PDF / DOCX / TXT / MD）")
    p_audit.add_argument("data", help="CSV 数据文件路径")
    p_audit.add_argument("--directive", "-d", default="",
                         help="用户指令（用于过滤核查项；含代写等意图会被红线拦截）")
    p_audit.add_argument("--llm", action="store_true",
                         help="启用 AI 深度审计段落（需配置 API Key；失败自动降级）")
    p_audit.add_argument("--llm-force", dest="llm_force", action="store_true",
                         help="强制刷新 LLM 缓存（忽略命中）")
    p_audit.add_argument("--output", "-o", help="输出 Markdown 文件路径（可选）")
    p_audit.set_defaults(func=cmd_audit)

    # simulate
    p_sim = sub.add_parser("simulate", help="生成模拟数据")
    p_sim.add_argument("method", help="方法 key（见 methods 子命令）")
    p_sim.add_argument("--effect-size", "-e", type=float, default=0.5, help="效应量（默认 0.5）")
    p_sim.add_argument("--n", "-n", type=int, default=30, help="每组样本量（默认 30）")
    p_sim.add_argument("--seed", "-s", type=int, default=42, help="随机种子（默认 42）")
    p_sim.add_argument("--noise", type=float, default=0.1, help="噪声比例（默认 0.1）")
    p_sim.add_argument("--output", "-o", help="输出 CSV 文件路径（可选）")
    p_sim.add_argument("--verbose", "-v", action="store_true", help="打印真值到 stderr")
    p_sim.set_defaults(func=cmd_simulate)

    # check-data
    p_dc = sub.add_parser("check-data", help="数据体检：找数据里的硬矛盾与可疑规律")
    p_dc.add_argument("data", help="CSV 数据文件路径")
    p_dc.add_argument("--format", "-f", choices=["json", "markdown"], default="markdown",
                      help="输出格式")
    p_dc.add_argument("--output", "-o", help="输出文件路径（可选）")
    p_dc.add_argument("--exit-zero", dest="exit_zero", action="store_true",
                      help="即使发现高优先级问题也返回 0（默认返回 1，便于脚本门控）")
    p_dc.set_defaults(func=cmd_check_data)

    # methods
    p_methods = sub.add_parser("methods", help="列出支持的方法")
    p_methods.set_defaults(func=cmd_methods)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
