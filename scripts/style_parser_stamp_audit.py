# -*- coding: utf-8 -*-
"""旧双臂（generic/vertical）单元格版本戳审计 —— 零 API。

【为什么需要这个】`style_eval --replay` 只重算 guarded 臂。generic/vertical
不走本地门，于是它们的 `parser_version` 停在当初真跑那一代（real28/synthetic
是 v2-salvage，holdout 是 v4/v5），而汇总顶层照样写 `gate_version: v3`。
审计者翻单元格会看到一份报告里混着四代戳，分不清哪些数字出自当前口径。

【不能直接改戳】戳是证据，不是标签。改戳前必须证明：用**当前**解析代码重放
缓存里的原始响应，逐条判词与解析模式都和旧记录一致。一致才说明旧戳只是标签
过期；不一致就说明结果本身过期，必须重算而不是改戳。

用法：
    python scripts/style_parser_stamp_audit.py            # 只审计，不写
    python scripts/style_parser_stamp_audit.py --restamp   # 零差异才改戳
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'scripts'))

import style_eval as se  # noqa: E402

OLD_ARMS = ('generic', 'vertical')


def rebuild(cell, rec, items_by_no):
    """从缓存取回当初的原始响应，用当前代码重判。取不到返回 (None, None, 'miss')。"""
    it = items_by_no.get(rec['序号'])
    if it is None:
        return None, None, 'no-item'
    system = se.SYSTEM_GENERIC if cell['arm'] == 'generic' else se.SYSTEM_VERTICAL
    prompt = se.TEMPLATE.format(reasons='；'.join(it['reasons']),
                                excerpt=it['excerpt'][:500])
    temp, mt = se.call_params(cell['backend'])
    raw = se.cache_get(se.cache_key(cell['backend'], cell['model'], system,
                                    prompt, cell['trial'], temp, mt))
    if raw is None:
        return None, None, 'miss'
    v, _reason, _err, mode = se.judge_old(lambda p, s: raw, system,
                                          it['reasons'], it['excerpt'])
    return v, mode, 'ok'


def audit_cell(path, items_by_no):
    cell = json.loads(path.read_text(encoding='utf-8'))
    if cell.get('arm') not in OLD_ARMS:
        return None
    diffs, modes, reasons = [], Counter(), Counter()
    for rec in cell['results']:
        v, mode, why = rebuild(cell, rec, items_by_no)
        reasons[why] += 1
        if why != 'ok':
            continue
        if v != rec['llm']:
            diffs.append({'序号': rec['序号'], 'case': rec.get('case'),
                          'old': rec['llm'], 'new': v})
        if mode != rec.get('parse_mode'):
            modes[(rec.get('parse_mode'), mode)] += 1
    return {'path': path, 'cell': cell, 'diffs': diffs,
            'mode_diffs': dict(modes), 'reasons': dict(reasons),
            'clean': not diffs and not modes and reasons.get('ok') == len(cell['results'])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', default='all', choices=['all'] + sorted(se.CORPORA))
    ap.add_argument('--restamp', action='store_true',
                    help='仅对「零缓存缺失 + 零判词差异 + 零模式差异」的单元格改戳')
    args = ap.parse_args()

    # 重放的是定死的响应，重试不可能改变结果；不关掉 sleep，
    # 每条解析失败的旧记录会白等 3×4 秒。
    se.time.sleep = lambda *_a, **_k: None

    names = sorted(se.CORPORA) if args.corpus == 'all' else [args.corpus]
    total = Counter()
    dirty = []
    for name in names:
        here = se.use_corpus_dir(name)
        items = se.CORPORA[name]()
        by_no = {it['序号']: it for it in items}
        stamps = Counter()
        n_cells = n_clean = 0
        for path in sorted(here.glob('*.json')):
            if path.name.startswith('汇总'):
                continue
            r = audit_cell(path, by_no)
            if r is None:
                continue
            n_cells += 1
            stamps[r['cell'].get('parser_version')] += 1
            total['items'] += len(r['cell']['results'])
            for k, v in r['reasons'].items():
                total[k] += v
            total['verdict_diffs'] += len(r['diffs'])
            total['mode_diffs'] += sum(r['mode_diffs'].values())
            if r['clean']:
                n_clean += 1
                if args.restamp and r['cell'].get('parser_version') != se.PARSER_VERSION:
                    old = r['cell'].get('parser_version')
                    r['cell']['restamped_from'] = old
                    r['cell']['parser_version'] = se.PARSER_VERSION
                    path.write_text(json.dumps(r['cell'], ensure_ascii=False, indent=1),
                                    encoding='utf-8')
                    total['restamped'] += 1
            else:
                dirty.append((name, path.name, r['diffs'][:4], r['mode_diffs'], r['reasons']))
        print(f'[{name}] 旧双臂单元格 {n_cells} 个，可证一致 {n_clean} 个；'
              f'原戳分布 {dict(stamps)}')
        total['cells'] += n_cells
        total['clean'] += n_clean

    print('\n总计 %d 个单元格 / %d 条判定；缓存命中 %d，缺失 %d，语料无此条 %d'
          % (total['cells'], total['items'], total['ok'], total['miss'], total['no-item']))
    print('判词差异 %d 条，解析模式差异 %d 条' % (total['verdict_diffs'], total['mode_diffs']))
    if args.restamp:
        print('已改戳 %d 个（保留 restamped_from 原戳）' % total['restamped'])
    for name, fname, diffs, modes, reasons in dirty:
        print(f'  [DIRTY] {name}/{fname} 判词差异={diffs} 模式差异={modes} 取响应={reasons}')
    if total['verdict_diffs'] or total['mode_diffs']:
        print('\n结论：旧双臂结果与当前解析口径**不一致**，不得改戳，必须重算。')
        return 1
    if total['miss'] or total['no-item']:
        print('\n结论：判词无差异，但有缓存缺失条目未验到；只对全命中单元格改戳。')
    else:
        print('\n结论：旧双臂逐条判词与解析模式均与当前代码一致 —— 旧戳只是标签过期。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
