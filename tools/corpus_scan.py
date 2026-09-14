"""
模板论文批量语料扫描（内测反馈流水线）
======================================
对 模板论文/ 下所有 *_提取.txt 跑识别层（方法/统计量/变量），
输出每篇的识别结果摘要 + 疑似问题标记，供人工快速复核。

判断标准（人工先看，再决定要不要修）：
  - 工科/综述/访谈类论文 → 不该识别出统计方法（0 误报为佳）
  - 财务/经济实证类 → 常见"描述统计/相关/回归"——识别出 regression 等未实装方法是预期（识别≠实装）
  - 统计量抽取 → p/t/F/r/χ² 有值即列出，人工抽查是否正文误报
  - 变量提取 → 列出来源分布，人工抽查垃圾短语
"""
import pathlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = pathlib.Path(__file__).resolve().parent
from extract_paper import extract_methods, extract_quantities, extract_variables

CORPUS = Path(__file__).resolve().parent.parent / '模板论文'

# 论文类型预标注（人工判断，用于对照识别行为）
TYPES = {
    "双摆桥式起重机": "工科控制",
    "噪声干扰": "工科信号",
    "知识付费": "文科综述",
    "光明乳业": "财务案例",
    "减税降费": "经济实证",
    "ICCP": "工科综述",
    "普惠金融": "经济路径",
    "绿色金融": "经济研究",
    "财会大数据": "财会应用",
    "赤字率": "访谈",
}

files = sorted(CORPUS.glob("*_提取.txt"))
print(f'共 {len(files)} 篇语料\n')

issues = []

for f in files:
    text = f.read_text(encoding="utf-8", errors="ignore")
    ptype = next((t for k, t in TYPES.items() if k in f.name), "?")

    methods = extract_methods(text)
    quants = extract_quantities(text)
    vars_ = extract_variables(text)

    print(f'{"=" * 70}')
    print(f'【{f.stem[:40]}】  类型：{ptype}  长度：{len(text)} 字符')
    print(f'  方法识别：{methods if methods else "（无）"}')
    print(f'  统计量：{len(quants)} 个')
    by_kind = {}
    for q in quants:
        by_kind.setdefault(q["kind"], []).append(q["value"])
    for kind, vals in sorted(by_kind.items()):
        shown = [str(v) for v in vals[:6]]
        more = f' …共{len(vals)}' if len(vals) > 6 else ''
        print(f'    {kind}: {shown}{more}')
    if vars_:
        # 变量来源分布
        by_src = {}
        for v in vars_:
            if isinstance(v, dict):
                src = v.get("source", "?")
                by_src.setdefault(src, []).append(v.get("name", v.get("var", "?")))
        if by_src:
            for src, names in sorted(by_src.items(), key=lambda x: -len(x[1])):
                shown = names[:8]
                more = f' …共{len(names)}' if len(names) > 8 else ''
                print(f'    变量[{src}]: {shown}{more}')
        else:
            print(f'    变量：{vars_[:8]}')
    else:
        print(f'  变量：（无）')

    # ---- 自动问题标记 ----
    if ptype in ("工科控制", "工科信号", "工科综述", "访谈") and methods:
        issues.append(f'{f.stem[:30]}：{ptype} 类却识别出方法 {methods}')
    if len(quants) > 40:
        issues.append(f'{f.stem[:30]}：统计量 {len(quants)} 个，可能过量抽取')

print(f'\n{"=" * 70}')
print('【自动标记的疑似问题】')
if issues:
    for it in issues:
        print(f'  ⚠️ {it}')
else:
    print('  （无——识别行为与论文类型预标注全部一致）')
