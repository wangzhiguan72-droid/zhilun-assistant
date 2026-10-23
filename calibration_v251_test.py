# -*- coding: utf-8 -*-
"""v2.51 · 复核校准版契约测试
=========================================================
语料依据：复核台账-2026-10-21（222 篇四层语料机器复验 + 30 条人工复核）。
覆盖：
    1. P0 is_front_matter_page：封面/版权页判定的保守边界
    2. P1a _style_view：参考文献区剔除 + 直接引语占位
    3. P1b _passage_scan：方法段"仅被动句"不再定位，多维命中仍定位
    4. P2 _soft_match_n + compare_abstract_vs_body：分组求和 / 招募→保留链
       （复核台账 J08：84+78+71+74=307；J28：招募80 → 完成66/71）

运行：python -u calibration_v251_test.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} {detail}')


print('== P0 封面/版权页判定 ==')
from extract_paper import is_front_matter_page

cover = ("单位代码： 10293 密 级：公开\n硕 士 学 位 论 文\n论文题目：某调查\n"
         "学号：2022100351 二〇二四年五月")
check('封面页(多标记+稀疏)判为前置页', is_front_matter_page(cover))
sparse1 = ("分类号\nG444")
check('仅 1 个标记的稀疏页不剥离', not is_front_matter_page(sparse1))
body = ("5 结论与讨论\n招募出色的人才进入公共部门是各国政府治理中的关键问题。" * 20)
check('正文页(长文本)不剥离', not is_front_matter_page(body))

print('== P1a 风格视角：参考文献区剔除 + 引文占位 ==')
from ai_audit import _style_view

txt = ("正文第一段 discusses 调查结果。\n\n参考文献\n[1] Dal Bó, Finan F. 2017.\n[2] 某某. 2020.")
v = _style_view(txt)
check('参考文献区从最后一处标题起剔除', 'Dal Bó' not in v and '正文第一段' in v)
q = '受访者说：“这个平台非常方便好用而且可以随时随地查询汉语的意思”，他认为很好。'
vq = _style_view(q)
check('直接引语(≥8字)被占位替换', '非常方便好用' not in vq and '引文略' in vq and '他认为很好' in vq)

print('== P1b 方法段"仅被动句"不再定位 ==')
from ai_audit import _passage_scan, audit_ai_traces

method_par = ("2.1 被试与数据处理\n本研究的问卷数据被收集后交由 SPSS 处理。"
              "样本被随机分配到两个组别中。缺失数据被剔除。平衡性检验被首先执行。"
              "量表的信度被再次检验。数据被标准化处理。")
res = _passage_scan(method_par)
check('方法段(仅被动句命中)不再定位', len(res) == 0,
      json.dumps(res, ensure_ascii=False)[:160] if res else '')
multi_par = ("5 结论与讨论\n综上所述，本研究取得了显著提高。同时，平台得以完善。"
             "因此可见，方案被推广。值得注意的是，这具有重要的价值。"
             "此外，相关结果被充分讨论。综上所述，影响得以扩大。")
res2 = _passage_scan(multi_par)
check('多维命中段落仍会被定位', len(res2) >= 1)

print('== P2 摘要↔正文 n 值软匹配 ==')
from audit import compare_abstract_vs_body, _soft_match_n

abs_qs = [{'kind': 'n', 'raw': '选取307名', 'value': 307,
           'context': '本研究选取307名学生的作品作为样本'}]
body_qs = [{'kind': 'n', 'raw': 'n=84', 'value': 84},
           {'kind': 'n', 'raw': 'n=78', 'value': 78},
           {'kind': 'n', 'raw': 'n=71', 'value': 71},
           {'kind': 'n', 'raw': 'n=74', 'value': 74},
           {'kind': 'n', 'raw': '选取了20份', 'value': 20}]
r1 = compare_abstract_vs_body(abs_qs, body_qs)
check('分组求和(84+78+71+74=307)不再报 mismatch', len(r1['mismatches']) == 0)
check('软匹配留痕 group_sum', r1['soft_matches'] and r1['soft_matches'][0]['style'] == 'group_sum'
      and r1['soft_matches'][0]['value'] == 307)

abs_qs2 = [{'kind': 'n', 'raw': '招募80名', 'value': 80,
            'context': '研究2招募80名严重学业拖延的小学生'}]
body_qs2 = [{'kind': 'n', 'raw': '收集\n1042名', 'value': 1042},
            {'kind': 'n', 'raw': '样本量为66', 'value': 66},
            {'kind': 'n', 'raw': '共71人', 'value': 71}]
r2 = compare_abstract_vs_body(abs_qs2, body_qs2)
check('招募→保留链(80 vs 66/71)不再报 mismatch', len(r2['mismatches']) == 0)
check('软匹配留痕 recruit_chain', r2['soft_matches'] and r2['soft_matches'][0]['style'] == 'recruit_chain')

check('非 n 值不软匹配', _soft_match_n({'kind': 'p', 'raw': 'p=0.05', 'value': 0.05}, []) is None)
check('招募链下限(0.5X)外的值不软匹配',
      _soft_match_n({'kind': 'n', 'raw': '招募80名', 'value': 80,
                     'context': '招募80名'}, [{'kind': 'n', 'raw': '40人', 'value': 40}]) is None)
# 摘要与正文完全无交集且不可解释时，仍应报 mismatch（不放过真矛盾）
# v2.52：常规差距（<3倍）仍报 mismatch；跨研究实体（≥3倍+研究语境）走 multi_study 留痕
abs_qs3 = [{'kind': 'n', 'raw': '调查500名', 'value': 500, 'context': '调查500名用户'}]
body_qs3 = [{'kind': 'n', 'raw': 'n=84', 'value': 84},
            {'kind': 'n', 'raw': 'n=78', 'value': 78}]
r3 = compare_abstract_vs_body(abs_qs3, body_qs3)
check('常规差距仍报 mismatch', len(r3['mismatches']) == 1)
abs_qs4 = [{'kind': 'n', 'raw': '抽取 600 名', 'value': 600,
            'context': '抽取 600 名成年居民参与此次调查，采用 24h 尿钠法'}]
body_qs4 = [{'kind': 'n', 'raw': '共纳入 18363 例研究对象', 'value': 18363},
            {'kind': 'n', 'raw': '农村居民 9538 名', 'value': 9538}]
r4 = compare_abstract_vs_body(abs_qs4, body_qs4)
check('跨研究实体(≥3倍)不报 mismatch 留痕 multi_study',
      len(r4['mismatches']) == 0 and r4['soft_matches']
      and r4['soft_matches'][0]['style'] == 'multi_study')
abs_qs5 = [{'kind': 'n', 'raw': '共22个', 'value': 22,
            'context': '共22个题项的问卷'}]
body_qs5 = [{'kind': 'n', 'raw': '收集了749份', 'value': 749}]
r5 = compare_abstract_vs_body(abs_qs5, body_qs5)
check('题项量词(22个题项)不进 n 匹配，按 missing 处理不报 mismatch',
      len(r5['mismatches']) == 0)

print('== 端到端：风格报告在封面剥离视角下正常出报告 ==')
full = cover + "\n\n5 结论与讨论\n综上所述，本研究取得了显著提高。同时，平台得以完善。" \
       "因此可见，方案被推广。值得注意的是，这具有重要的价值。此外，相关结果被充分讨论。" \
       "综上所述，影响得以扩大。"
rep = audit_ai_traces(full)
check('audit_ai_traces 返回结构完整', isinstance(rep, dict) and 'passages' in rep)

print(f'\n==== {PASS} passed, {FAIL} failed ====')
sys.exit(1 if FAIL else 0)
