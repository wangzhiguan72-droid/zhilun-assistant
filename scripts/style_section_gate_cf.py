"""零成本反事实：section_gate 在截断摘录上是否该把「假」降为「存疑」。

背景：留出集 h-cut-quote-unclosed（引号未闭合 + 切在句中，人工「存疑」，
构造依据写明「分不清是受访者原话还是作者转述」）在 8 模型 × 5 重复的
39 个有效护栏单元里 **39/39 被判假**，来源全是 section_gate——
模型把它识别成「引语或访谈」，本地就直接放行成「假」。

这与 style_triage 自己的红线冲突：「证据不足一律存疑，绝不塌缩成假」。
封面/参考文献的豁免有**窗口内可判定的本地硬证据**（字段族 ≥3 + 字段排版 /
文献标识正则），截断不影响这些事实；而「这段是引语」只有模型的识别，
引号没闭合时恰恰是识别不可靠的情形。

本脚本只读缓存与已落盘单元，**不调任何 API**，用来在改产品之前量清：
  变体 A（宽）：section_gate + 截断 → 存疑
  变体 B（窄）：section_gate + 截断 + 模型识别为引语/访谈 → 存疑
分别给出三语料的指标变化与逐条目得失（新增误判必须点名，不许只报收益）。

用法：.venv/Scripts/python.exe scripts/style_section_gate_cf.py
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import style_eval as se          # noqa: E402
import style_triage as st        # noqa: E402

BASE = pathlib.Path(__file__).resolve().parent.parent
METRICS = ('严格一致率', '方向一致率', '特异度', '假族覆盖')


def cells(corpus: str, arm: str = 'guarded'):
    d = BASE / ('_style_eval' if corpus == 'real28' else f'_style_eval_{corpus}')
    for p in sorted(d.glob(f'*__{arm}__r*.json')):
        c = json.loads(p.read_text(encoding='utf-8'))
        if c.get('complete') and not se.fallback_count(c):
            yield p, c


def unclosed_quote(text: str, curly: bool = False) -> bool:
    """窗口内是否有开了没关的直接引号。截断本身不是判据——
    real28 #2 是**无引号的第三人称转述**且被截断，人工仍判假。"""
    pairs = [('「', '」'), ('『', '』')]
    if curly:
        pairs += [('“', '”'), ('‘', '’')]
    return any(text.count(o) > text.count(c) for o, c in pairs)


def is_quote_section(res: dict) -> bool:
    sec = ((res.get('model_opinion') or {}).get('section_type') or '')
    return '引语' in sec or '访谈' in sec


def variant(res: dict, excerpt: str, name: str) -> str:
    v = res['llm']
    if name == 'A':      # 宽：section_gate + 截断
        if res.get('source') == 'section_gate' and st._is_truncated(excerpt):
            return '存疑'
    elif name == 'B':    # 窄：section_gate + 截断 + 引语/访谈
        if (res.get('source') == 'section_gate' and st._is_truncated(excerpt)
                and is_quote_section(res)):
            return '存疑'
    elif name == 'C':    # 未闭合直角引号 + 引语/访谈识别（只看 section_gate）
        if (res.get('source') == 'section_gate' and is_quote_section(res)
                and unclosed_quote(excerpt)):
            return '存疑'
    elif name == 'C2':   # C 再加弯引号
        if (res.get('source') == 'section_gate' and is_quote_section(res)
                and unclosed_quote(excerpt, curly=True)):
            return '存疑'
    elif name == 'D':    # 未闭合引号 → 任何来源都存疑（边界不可判定是全局问题）
        if unclosed_quote(excerpt) and v != '存疑':
            return '存疑'
    elif name == 'D2':
        if unclosed_quote(excerpt, curly=True) and v != '存疑':
            return '存疑'
    return v


VARIANTS = (('A', '宽：section_gate + 截断'),
            ('B', '窄：section_gate + 截断 + 引语/访谈'),
            ('C', '未闭合直角引号 + 引语识别（限 section_gate）'),
            ('C2', 'C + 弯引号'),
            ('D', '未闭合直角引号 → 任何来源都存疑'),
            ('D2', 'D + 弯引号'))


def main() -> int:
    for corpus, loader in se.CORPORA.items():
        items = loader()
        by_no = {it['序号']: it for it in items}
        # real28 的 case 会重复（同一 case 拆多条），所以一律按 序号 连接摘录；
        # 展示时带上 case 只为可读，不参与键。
        assert len(by_no) == len(items), '序号 不唯一，无法连接摘录'
        got = list(cells(corpus))
        if not got:
            print(f'\n########## {corpus}：无有效护栏单元，跳过')
            continue
        print(f'\n########## {corpus}（{len(items)} 条，{len(got)} 个有效护栏单元）')
        gate_items = Counter()
        gate_trunc = Counter()
        for _, c in got:
            for r in c['results']:
                if r.get('source') == 'section_gate':
                    ex = by_no[r['序号']]['excerpt']
                    gate_items[(f"#{r['序号']} {r['case']}", r['human'])] += 1
                    if st._is_truncated(ex):
                        gate_trunc[(f"#{r['序号']} {r['case']}", r['human'])] += 1
        print('  section_gate 命中条目：', dict(gate_items))
        print('  其中摘录截断的：    ', dict(gate_trunc))
        uq = [(f"#{it['序号']} {it['case']}", it['human'])
              for it in items if unclosed_quote(it['excerpt'])]
        uq2 = [(f"#{it['序号']} {it['case']}", it['human'])
               for it in items if unclosed_quote(it['excerpt'], curly=True)]
        print('  未闭合直角引号的条目（变体 D 的作用域）：', uq)
        print('  再加弯引号后多出的（D2 − D）：', [x for x in uq2 if x not in uq])
        for key, name in VARIANTS:
            changed = Counter()
            base_scores, new_scores = [], []
            for _, c in got:
                new = []
                for r in c['results']:
                    ex = by_no[r['序号']]['excerpt']
                    v = variant(r, ex, key)
                    if v != r['llm']:
                        changed[(f"#{r['序号']} {r['case']}", r['human'], r['llm'], v)] += 1
                    new.append(dict(r, llm=v))
                base_scores.append(se.score(c['results']))
                new_scores.append(se.score(new))
            print(f'  --- 变体 {name}')
            if not changed:
                print('      无任何条目改判（规则在本语料不触发）')
                continue
            for k, v in sorted(changed.items(), key=lambda x: -x[1]):
                tag = '收益' if k[1] == k[3] else ('新增误判' if k[1] == k[2] else '中性')
                print(f'      {k[0]}  人工{k[1]}  {k[2]}→{k[3]}  ×{v}  [{tag}]')
            for m in METRICS:
                b = [s[m] for s in base_scores if s[m] is not None]
                n = [s[m] for s in new_scores if s[m] is not None]
                if not b:
                    continue
                mb, mn = sum(b) / len(b), sum(n) / len(n)
                print(f'      {m}: {mb:.2f} → {mn:.2f}  ({mn - mb:+.2f}pt)')
            fb = sum(s['_counts']['fn'] for s in base_scores)
            fn = sum(s['_counts']['fn'] for s in new_scores)
            print(f'      漏真FN 合计: {fb} → {fn}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
