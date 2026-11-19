# -*- coding: utf-8 -*-
"""三语料汇总独立复核：从原始单元格重算，逐子字段比对汇总。零 API。

为什么需要它：`style_eval.py` 每次调用都只用**自己那批** specs/arms 覆盖汇总文件。
只跑 Kimi 就会把主口径 8 个模型从汇总里抹掉，而文件看起来照样完整。
汇总字段自证不了自己，必须有一份**不 import 被测代码**的独立实现来对账。

独立性边界：本文件自己实现打分与汇总语义，不调用 `style_eval.score/summarize`，
也不读汇总里的任何指标值来「算」——汇总只作为**被比对的一方**。
唯一共享的是文件名约定（`{slug(model)}__{arm}__r{trial}.json`）与交付版本戳。

为什么比对到子字段：只比 `{mean, sd}` 会漏掉 `min/max/per_trial` 错位，
而 per_trial 正是「哪一轮出的数」的唯一凭据。列表逐元素比，标量逐个比。

为什么 `fallbacks` 缺失不算异常：旧单元格没有这个落盘字段，设计上以 `results`
为唯一真源现算（见 `style_eval.fallback_count`）。只有**字段存在且与现算不符**才是问题。

语料缺席时如实跳过该语料（real28 的单元格在仓库外私有目录派生的本机目录里，
新克隆上可能没有），退出码仍为 0；三套全缺才算失败。

用法：.venv/Scripts/python.exe scripts/style_eval_verify.py
退出码：0 全部一致（或如实跳过）/ 1 存在不符 / 2 无可核对语料。
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics as st
import sys
from collections import Counter

BASE = pathlib.Path(__file__).resolve().parents[1]
CORPORA = [('real28', '_style_eval', 28),
           ('synthetic', '_style_eval_synthetic', 30),
           ('holdout', '_style_eval_holdout', 24)]
METRICS = ('严格一致率', '方向一致率', '特异度', '假族覆盖')
STAMP = 'v6-quote-boundary'


def slug(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '__', s.replace('/', '__'))


def my_score(results):
    n = len(results)
    c = Counter()
    for r in results:
        v, lab = r['llm'], r['human']
        c['strict'] += (v == lab)
        ht, lt = lab == '真', v == '真'
        c['directional'] += (ht == lt)
        if not ht:
            c['spec_den'] += 1
            c['spec_num'] += (not lt)
            if lab == '假':
                c['ff_den'] += 1
                c['ff_num'] += (v == '假')
        elif v == '假':
            c['fn'] += 1

    def pct(a, b):
        return round(100.0 * a / b, 4) if b else None

    return {'n': n, '严格一致率': pct(c['strict'], n),
            '方向一致率': pct(c['directional'], n),
            '特异度': pct(c['spec_num'], c['spec_den']),
            '假族覆盖': pct(c['ff_num'], c['ff_den']), '_counts': c}


def fallbacks(cell):
    return sum(1 for r in cell['results'] if r.get('parse_mode') == 'error_fallback')


def my_summarize(cells):
    good = [c for c in cells if c.get('complete') and not fallbacks(c)]
    bad = [c for c in cells if not c.get('complete')]
    if not good:
        return {'available': 0, 'incomplete': len(bad)}
    scores = [my_score(c['results']) for c in good]
    out = {'available': len(good), 'incomplete': len(bad), 'n': scores[0]['n'],
           'model_calls_mean': round(st.mean(c['model_calls'] for c in good), 2),
           '漏真FN_total': sum(s['_counts']['fn'] for s in scores),
           'sources': dict(Counter(r.get('source', '')
                                   for c in good for r in c['results']))}
    for k in METRICS:
        vals = [s[k] for s in scores if s[k] is not None]
        if not vals:
            out[k] = None
            continue
        out[k] = {'mean': round(st.mean(vals), 2),
                  'sd': round(st.stdev(vals), 2) if len(vals) > 1 else 0.0,
                  'min': round(min(vals), 4), 'max': round(max(vals), 4),
                  'per_trial': vals}
    return out


def compare(mine, theirs, path, mism, counter):
    if isinstance(mine, dict):
        if not isinstance(theirs, dict):
            counter[0] += 1
            mism.append((path, '类型不符', type(theirs).__name__))
            return
        for k in mine:
            compare(mine[k], theirs.get(k), f'{path}.{k}', mism, counter)
    elif isinstance(mine, list):
        if not isinstance(theirs, list) or len(mine) != len(theirs):
            counter[0] += 1
            mism.append((path, mine, theirs))
            return
        for i, (a, b) in enumerate(zip(mine, theirs)):
            compare(a, b, f'{path}[{i}]', mism, counter)
    else:
        counter[0] += 1
        if mine != theirs:
            mism.append((path, mine, theirs))


def verify_corpus(name, folder, nitems):
    """返回 (单元格数, 比对字段数, 不符列表, 戳异常列表, 无fallbacks字段数)。"""
    agg_path = BASE / folder / f'汇总_{name}.json'
    if not agg_path.exists():
        return None
    d = json.loads(agg_path.read_text(encoding='utf-8'))
    if d.get('items') != nitems:
        return None, None, [(f'{name} 汇总 items', nitems, d.get('items'))], [], 0
    by_slug = {slug(m['model']): m for m in d['models']}
    cells, stamps, no_fb = {}, [], [0]
    for p in sorted((BASE / folder).glob('*__r*.json')):
        sm, arm, _rt = p.stem.rsplit('__', 2)
        c = json.loads(p.read_text(encoding='utf-8'))
        if c.get('parser_version') != STAMP or c.get('items') != nitems:
            stamps.append((p.name, c.get('parser_version'), c.get('items')))
        if 'fallbacks' in c and c['fallbacks'] != fallbacks(c):
            stamps.append((p.name, 'fallbacks 与 results 不符', c['fallbacks']))
        if 'fallbacks' not in c:
            no_fb[0] += 1
        cells.setdefault((sm, arm), []).append(c)
    mism, counter = [], [0]
    for (sm, arm), cs in sorted(cells.items()):
        m = by_slug.get(sm)
        if m is None:
            mism.append((f'{sm}/{arm}', '汇总里没有这个模型', None))
            continue
        compare(my_summarize(cs), m['arms'][arm], f'{m["label"]}/{arm}', mism, counter)
    return sum(len(v) for v in cells.values()), counter[0], mism, stamps, no_fb[0]


def main():
    tot_cells = tot_fields = tot_mism = tot_stamp = tot_nofb = 0
    done = 0
    for name, folder, nitems in CORPORA:
        r = verify_corpus(name, folder, nitems)
        if r is None:
            print(f'[SKIP] {name}：汇总文件不在本机（仓库外私有语料派生），跳过')
            continue
        nc, nf, mism, stamps, nofb = r
        done += 1
        print(f'[{name}] 单元格 {nc}  比对字段 {nf}  不符 {len(mism)}  戳异常 {len(stamps)}')
        for x in mism[:8]:
            print('    MISMATCH', x)
        for x in stamps[:8]:
            print('    STAMP', x)
        tot_cells += nc or 0
        tot_fields += nf or 0
        tot_mism += len(mism)
        tot_stamp += len(stamps)
        tot_nofb += nofb
    if not done:
        print('[FAIL] 三套语料的汇总一个都不在本机，无从核对')
        return 2
    print(f'\n合计：单元格 {tot_cells}，比对字段 {tot_fields}，'
          f'不符 {tot_mism}，戳异常 {tot_stamp}'
          f'（另有 {tot_nofb} 个旧单元格无 fallbacks 字段，按设计以 results 为真源）')
    if tot_fields == 0:
        print('[FAIL] 一个字段都没比对到——这不是「通过」，是核对没发生')
        return 1
    ok = tot_mism == 0 and tot_stamp == 0
    print('结论：', '独立重算与汇总逐子字段一致' if ok else '存在不符，必须查清后再交付')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
