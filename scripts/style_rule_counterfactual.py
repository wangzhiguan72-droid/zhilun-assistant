# -*- coding: utf-8 -*-
"""零成本反事实：量化「护栏 disagreement 分支再放宽」的收益与代价。

只读已落盘的评测单元 + 缓存，**不调任何 API、不改产品**。

⚠ 口径已变（v2.62 之后读这里）：
    本工具当初评估的候选修法**已经发布**，就是 style_triage.reconcile 里的
    `soft_signal_cleared` 分支（方法/结果段 + 完整摘录 + 仅密度型信号 +
    模型放行 → 假）。发布后那批记录不再落在 source=='disagreement' 上，
    所以现在跑本工具，「现行」列**已含该修法**，触发数量是**残余**的
    disagreement 条数，不是当初那 100 条。把它读成「候选没收益」是错的。

    同时截断守卫也已加宽：local_signals 的 truncated 字段不再只认显式省略号，
    句末标点缺失同样算截断（style_triage._is_truncated）。当初「认不出无标记
    截断」正是不采纳无守卫版本的原因，现在这个代价已被堵住。

残余 population 仍然值得量：剩下的 disagreement 里，有多少是截断摘录、
有多少含硬事实维度——这回答「截断守卫是不是过度保守」。

用法：
    .venv/Scripts/python.exe scripts/style_rule_counterfactual.py
前置：_style_eval/ 与 _style_eval_synthetic/ 里已有跑满的护栏单元。
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import style_triage as st  # noqa: E402
from scripts.style_eval import CORPORA, score  # noqa: E402

METRICS = ('严格一致率', '方向一致率', '特异度', '假族覆盖')


def counterfactual(rec, item, skip_truncated):
    """返回 (原判定, 反事实判定, 是否触发候选规则)。"""
    old = rec['llm']
    if rec.get('source') != 'disagreement':
        return old, old, False
    section = (rec.get('model_opinion') or {}).get('section_type', '') or ''
    if not any(k in section for k in st._METHOD_SECTIONS):
        return old, old, False
    sig = st.local_signals(item['excerpt'])
    if skip_truncated and sig.get('truncated'):
        return old, old, False
    eff = st.effective_dimensions(item['reasons'], section)
    corro = [d for d in eff if d in sig['present']]
    if not corro or set(corro) & st.HARD_FACT_DIMENSIONS:
        return old, old, False
    return old, '假', True


def complete_models(summary):
    """只取三臂齐全的模型——缺臂的模型进均值会把口径搅浑。"""
    return [m['label'] for m in summary['models']
            if all((m['arms'].get(a) or {}).get('available')
                   for a in ('generic', 'vertical', 'guarded'))]


def evaluate(corp, subdir, skip_truncated):
    summary = json.loads((ROOT / subdir / f'汇总_{corp}.json').read_text(encoding='utf-8'))
    labels = complete_models(summary)
    by_no = {it['序号']: it for it in CORPORA[corp]()}
    cells = []
    for p in (ROOT / subdir).glob('*guarded*.json'):
        c = json.loads(p.read_text(encoding='utf-8'))
        if c['label'] in labels and c.get('complete'):
            cells.append(c)
    if not cells:
        print(f'  [SKIP] {corp} 没有可用的护栏单元')
        return
    base, cf = collections.Counter(), collections.Counter()
    fired = collections.Counter()
    flips = collections.defaultdict(collections.Counter)
    for c in cells:
        old_rs, new_rs = [], []
        for r in c['results']:
            item = by_no.get(r['序号'])
            if item is None:
                old_rs.append(r)
                new_rs.append(r)
                continue
            o, n, hit = counterfactual(r, item, skip_truncated)
            if hit:
                key = (r['序号'], r['case'], r['human'])
                fired[key] += 1
                flips[key][f'{o}→{n}'] += 1
            old_rs.append(dict(r, llm=o))
            new_rs.append(dict(r, llm=n))
        so, sn = score(old_rs), score(new_rs)
        for k in METRICS:
            base[k] += so[k] or 0
            cf[k] += sn[k] or 0
        base['fn'] += so['漏真FN']
        cf['fn'] += sn['漏真FN']
    n = len(cells)
    print(f'  --- {"排除截断摘录" if skip_truncated else "不排除截断"} ---')
    for k in METRICS:
        print('    %-8s 现行 %7.3f → %7.3f  (%+.3f)'
              % (k, base[k] / n, cf[k] / n, (cf[k] - base[k]) / n))
    print('    漏真FN 合计 现行 %d → %d' % (base['fn'], cf['fn']))
    for key, cnt in sorted(fired.items(), key=lambda x: -x[1]):
        print('      #%-3s %-30s 标签%s 触发%3d %s'
              % (key[0], key[1], key[2], cnt, dict(flips[key])))


def main():
    for corp, subdir in (('real28', '_style_eval'), ('synthetic', '_style_eval_synthetic')):
        print('=' * 84)
        print(corp)
        for skip in (False, True):
            evaluate(corp, subdir, skip)


if __name__ == '__main__':
    main()