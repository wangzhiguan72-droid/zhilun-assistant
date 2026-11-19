# -*- coding: utf-8 -*-
"""style_eval 评测脚本的离线测试（零 API 调用）。

重点守两条：
  1. **公平性**：guarded_one 在严格解析成功时必须与产品 st.triage_passage 逐字段
     一致。否则「给旧双臂加容错、护栏臂不加」会让对照偏袒某一臂。
  2. **失败安全**：截断抢救只能把「丢弃」变成「存疑」，绝不能凭空造出「假」或「真」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import style_triage as st  # noqa: E402

_spec = importlib.util.spec_from_file_location('style_eval', BASE / 'scripts/style_eval.py')
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

#: real28 是仓库外私有语料（.git/info/exclude 排除），新克隆上不存在。
#: 依赖它的用例显式跳过并说明原因，不报错打红、也不静默当成通过。
needs_real28 = unittest.skipUnless(
    se.real28_available(), '真实语料 real28 不在本机（仓库外私有数据），跳过')


def canned(**kw):
    base = {'section_type': '讨论', 'visible': ['拔高词没有数字支撑'],
            'needs_revision': '是', 'evidence_sufficient': '是',
            'basis': '「显著提升」后无数字'}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def canned_old(verdict='真', reason='「显著提升」后无数字'):
    return json.dumps({'verdict': verdict, 'reason': reason}, ensure_ascii=False)


#: 实测截断样本（Gemini-3.1-Flash-Lite，max_tokens=2000 被推理吃光）
TRUNC_OLD = '{"verdict": "假", "reason": "属于方法与测量说明段落，被动语态和具体赋值标准为学科惯例，不属于'
TRUNC_TRIAGE = ('{"section_type": "方法", "visible": ["被动句偏多"], '
                '"needs_revision": "否", "evidence_sufficient": "是", '
                '"basis": "「采用问卷法」属常规')


class ParityTest(unittest.TestCase):
    """严格解析成功时，评测层与产品层必须完全一致。"""

    @needs_real28
    def test_matches_product_on_real_corpus(self):
        items = se.load_real28()
        outputs = [canned(), canned(section_type='方法', visible=['被动句偏多'],
                                    needs_revision='否'),
                   canned(section_type='封面', visible=[], needs_revision='否'),
                   canned(needs_revision='无法判断', evidence_sufficient='否'),
                   canned(visible=[], needs_revision='是')]
        checked = strict = exempt = 0
        for it in items:
            for out in outputs:
                a = st.triage_passage(it['reasons'], it['excerpt'], lambda p, s: out)
                b = se.guarded_one(it['reasons'], it['excerpt'], lambda p, s: out)
                self.assertIn(b['parse_mode'], ('strict', 'exempt'), it['序号'])
                strict += b['parse_mode'] == 'strict'
                exempt += b['parse_mode'] == 'exempt'
                b.pop('parse_mode')
                self.assertEqual(a, b, f'#{it["序号"]} {out[:40]}')
                checked += 1
        self.assertEqual(checked, len(items) * len(outputs))
        self.assertEqual(strict + exempt, checked)
        self.assertGreater(strict, 40, '走到模型解析的核对太少，不算通过')
        self.assertGreater(exempt, 0, '真实语料里应有豁免段落，否则夹具失真')

    def test_exempt_never_calls_model(self):
        def boom(p, s):
            raise AssertionError('豁免段落不该调模型')
        cover = '密级：公开\n学号：2022000000\n分类号：G64\n单位代码：10000\n学位论文\n作者姓名：示例\n'
        r = se.guarded_one(['被动句偏多'], cover, boom)
        self.assertFalse(r['model_called'])
        self.assertEqual(r['verdict'], '假')
        self.assertEqual(r['parse_mode'], 'exempt')

    def test_no_model_is_local_only(self):
        r = se.guarded_one(['拔高词没有数字支撑'], '本研究显著提升了效果。', None)
        self.assertFalse(r['model_called'])
        self.assertEqual(r['parse_mode'], 'no_model')
        self.assertEqual(r['source'], 'local_only')


class SalvageOldTest(unittest.TestCase):
    def test_recovers_verdict_from_truncated(self):
        self.assertEqual(se.salvage_old(TRUNC_OLD), ('假', '属于方法与测量说明段落，被动语态和具体赋值标准为学科惯例，不属于'))

    def test_rejects_illegal_verdict(self):
        self.assertIsNone(se.salvage_old('{"verdict": "成立", "reason": "x"}'))
        self.assertIsNone(se.salvage_old(''))
        self.assertIsNone(se.salvage_old('没有 JSON'))

    def test_reason_optional(self):
        self.assertEqual(se.salvage_old('{"verdict": "真"'), ('真', ''))

    def test_judge_old_marks_mode(self):
        v, reason, err, mode = se.judge_old(lambda p, s: TRUNC_OLD, 'sys', ['被动句偏多'], '摘录')
        self.assertEqual((v, err, mode), ('假', None, 'salvaged'))
        v, _, err, mode = se.judge_old(lambda p, s: '{"verdict":"真","reason":"r"}',
                                       'sys', ['被动句偏多'], '摘录')
        self.assertEqual((v, err, mode), ('真', None, 'strict'))

    def test_judge_old_fails_after_retries(self):
        calls = []

        def bad(p, s):
            calls.append(1)
            return '模型拒答'
        v, _, err, mode = se.judge_old(bad, 'sys', ['x'], 'y')
        self.assertIsNone(v)
        self.assertEqual(mode, 'failed')
        self.assertEqual(len(calls), 3, '解析失败仍应重试 3 次，与 r6 一致')


class SalvageTriageTest(unittest.TestCase):
    def test_recovers_all_fields(self):
        d = se.salvage_triage(TRUNC_TRIAGE)
        self.assertEqual(d['section_type'], '方法')
        self.assertEqual(d['visible'], ['被动句偏多'])
        self.assertEqual(d['needs_revision'], '否')
        self.assertEqual(d['evidence_sufficient'], '是')
        self.assertTrue(d['basis'].startswith('「采用问卷法」'))

    def test_requires_section_type(self):
        self.assertIsNone(se.salvage_triage('{"visible": ["x"], "needs_revision": "是"}'))
        self.assertIsNone(se.salvage_triage(''))

    def test_truncated_visible_array_not_accepted(self):
        d = se.salvage_triage('{"section_type": "讨论", "visible": ["拔高词没有数字支撑')
        self.assertEqual(d['visible'], [], '未闭合数组不能当成有效确认')

    def test_partial_fields_never_fabricate_false(self):
        """只有 section_type 时，缺 needs/sufficient 不得走 not_visible 判假。"""
        text = '本研究显著提升了教学质量，具有重大意义。'
        reasons = ['拔高词没有数字支撑']
        for out in ('{"section_type": "讨论"',
                    '{"section_type": "讨论", "visible": []',
                    '{"section_type": "讨论", "needs_revision": "否"',
                    '{"section_type": "讨论", "evidence_sufficient": "是"'):
            d = se.salvage_triage(out)
            self.assertIsNotNone(d, out)
            r = st.reconcile(d, reasons, text)
            self.assertNotEqual(r['verdict'], '假', f'{out} → {r["source"]}')

    def test_salvage_rescues_real_truncation(self):
        """产品严格解析丢弃 → 评测层抢救回模型意见，且不再落 unparseable。"""
        text = '本研究显著提升了教学质量，具有重大意义。'
        reasons = ['拔高词没有数字支撑']
        strict = st.triage_passage(reasons, text, lambda p, s: TRUNC_TRIAGE)
        self.assertEqual(strict['source'], 'unparseable')
        salv = se.guarded_one(reasons, text, lambda p, s: TRUNC_TRIAGE)
        self.assertEqual(salv['parse_mode'], 'salvaged')
        self.assertIsNotNone(salv['model_opinion'], '模型意见必须保留，不能被丢弃')
        self.assertEqual(salv['model_opinion']['section_type'], '方法')

    def test_unparseable_still_reported(self):
        r = se.guarded_one(['x'], 'y', lambda p, s: '完全不是 JSON')
        self.assertEqual(r['parse_mode'], 'unparseable')
        self.assertEqual(r['verdict'], '存疑')


class CacheKeyTest(unittest.TestCase):
    def test_trial_in_key(self):
        a = se.cache_key('gateway', 'm', 'sys', 'p', 0)
        b = se.cache_key('gateway', 'm', 'sys', 'p', 1)
        self.assertNotEqual(a, b, 'trial 不进 key 会把方差抹平')

    def test_stable_and_sensitive(self):
        k = lambda **kw: se.cache_key(**{'backend': 'gateway', 'model': 'm',
                                         'system': 's', 'prompt': 'p', 'trial': 0, **kw})
        self.assertEqual(k(), k())
        for field, val in (('model', 'm2'), ('system', 's2'), ('prompt', 'p2'),
                           ('backend', 'official')):
            self.assertNotEqual(k(), k(**{field: val}), field)

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            old = se.CACHE
            se.CACHE = Path(td)
            try:
                key = se.cache_key('gateway', 'm', 's', 'p', 0)
                self.assertIsNone(se.cache_get(key))
                se.cache_put(key, '响应文本', {'cached_tokens': 12})
                self.assertEqual(se.cache_get(key), '响应文本')
                disk = json.loads((Path(td) / f'{key}.json').read_text(encoding='utf-8'))
                self.assertEqual(disk['usage']['cached_tokens'], 12)
                self.assertNotIn('论文摘录', json.dumps(disk, ensure_ascii=False))
                self.assertEqual(set(disk), {'response', 'usage', 'ts'},
                                 '盘上只存响应与用量，不存 prompt/摘录')
            finally:
                se.CACHE = old


class PreflightBypassesCacheTest(unittest.TestCase):
    """探活走缓存 = 假绿：探针 prompt 恒定，首次成功后永远命中，
    额度耗尽 / 模型下线一个都探不出来。这里把缓存喂饱再断网，
    preflight 仍必须判死。"""

    PROBE_SYS = '你是连通性探针。'
    PROBE_PROMPT = '只回 JSON：{"ok":"是"}'

    def _run(self, seed_cache):
        with tempfile.TemporaryDirectory() as td:
            old_cache, old_urlopen = se.CACHE, se.urllib.request.urlopen
            se.CACHE = Path(td)
            calls = []

            def no_network(*a, **kw):
                calls.append(a)
                raise OSError('network down / quota exhausted')

            se.urllib.request.urlopen = no_network
            try:
                if seed_cache:
                    se.cache_put(se.cache_key('gateway', 'm', self.PROBE_SYS,
                                              self.PROBE_PROMPT, -1),
                                 '{"ok":"是"}', {})
                alive, dead = se.preflight([('gateway', 'm', 'M')], timeout=1)
                return alive, dead, calls, td
            finally:
                se.CACHE = old_cache
                se.urllib.request.urlopen = old_urlopen

    def test_cached_probe_must_not_mask_dead_backend(self):
        alive, dead, calls, _ = self._run(seed_cache=True)
        self.assertEqual(alive, [], '缓存里有探针响应就把后端判活 = 假绿')
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]['label'], 'M')
        self.assertIn('network down', dead[0]['reason'])
        self.assertTrue(calls, '探活必须真发请求，不能只读缓存')

    def test_probe_not_written_to_cache(self):
        with tempfile.TemporaryDirectory() as td:
            old_cache, old_urlopen = se.CACHE, se.urllib.request.urlopen
            se.CACHE = Path(td)
            se.urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(
                OSError('down'))
            try:
                se.preflight([('gateway', 'm', 'M')], timeout=1)
                self.assertEqual(list(Path(td).glob('*.json')), [],
                                 '探活结果不该落盘，否则下一轮又被自己骗')
            finally:
                se.CACHE = old_cache
                se.urllib.request.urlopen = old_urlopen

    def test_normal_call_still_uses_cache(self):
        with tempfile.TemporaryDirectory() as td:
            old_cache, old_urlopen = se.CACHE, se.urllib.request.urlopen
            se.CACHE = Path(td)

            def boom(*a, **kw):
                raise AssertionError('缓存命中时不该发请求')

            se.urllib.request.urlopen = boom
            try:
                se.cache_put(se.cache_key('gateway', 'm', 's', 'p', 0), '命中', {})
                self.assertEqual(se.raw_call('gateway', 'm', 's', 'p', 0), '命中')
            finally:
                se.CACHE = old_cache
                se.urllib.request.urlopen = old_urlopen


class ScoreTest(unittest.TestCase):
    def test_denominator_is_all_items(self):
        rs = [{'llm': '真', 'human': '真'}, {'llm': None, 'human': '真'}]
        s = se.score(rs)
        self.assertEqual(s['n'], 2)
        self.assertEqual(s['严格一致率'], 50.0, 'None 必须计入分母，不能静默缩小')

    def test_directional_treats_doubt_as_not_true(self):
        rs = [{'llm': '存疑', 'human': '假'}, {'llm': '假', 'human': '真'}]
        s = se.score(rs)
        self.assertEqual(s['方向一致率'], 50.0)
        self.assertEqual(s['漏真FN'], 1)

    def test_wilson_bounds(self):
        """wilson 返回百分数（0-100），与其余指标同单位。"""
        lo, hi = se.wilson(1, 1)
        self.assertGreater(lo, 0.0, '1/1 不能声称确定，下界必须离开 0')
        self.assertLessEqual(hi, 100.0)
        self.assertIsNone(se.wilson(0, 0), '零样本没有区间，不能编造')
        lo, hi = se.wilson(28, 28)
        self.assertLessEqual(hi, 100.0)
        self.assertGreater(lo, 85.0)
        lo, hi = se.wilson(14, 28)
        self.assertLess(lo, 50.0)
        self.assertGreater(hi, 50.0)
        narrow = se.wilson(140, 280)
        self.assertLess(narrow[1] - narrow[0], hi - lo, '样本量大区间必须更窄')


class QuotaVsRateLimitTest(unittest.TestCase):
    """额度耗尽与瞬时限流都是 429，但前者重试不会好转，必须立刻放弃。"""

    class FakeHTTPError(Exception):
        def __init__(self, body):
            super().__init__("HTTP Error 429: Too Many Requests")
            self._body = body.encode('utf-8')

        def read(self):
            return self._body

    def test_quota_exhausted_fails_fast_without_retry(self):
        calls = []
        slept = []
        body = '{"error":{"message":"WorkBuddy: 额度已用尽，请购买加量包"}}'

        def boom(*a, **k):
            calls.append(1)
            raise self.FakeHTTPError(body)

        old_raw, old_sleep = se.raw_call, se.time.sleep
        se.raw_call, se.time.sleep = boom, lambda s: slept.append(s)
        try:
            with self.assertRaises(se.BackendUnavailable) as ctx:
                se.make_complete('gateway', 'm', 0)('p', 's')
            self.assertIn('额度已用尽', str(ctx.exception))
        finally:
            se.raw_call, se.time.sleep = old_raw, old_sleep
        self.assertEqual(len(calls), 1, '额度耗尽不该重试')
        self.assertEqual(slept, [], '额度耗尽不该白等')

    def test_transient_429_still_retries(self):
        calls = []
        slept = []

        def boom(*a, **k):
            calls.append(1)
            raise self.FakeHTTPError('{"error":{"message":"rate limit, slow down"}}')

        old_raw, old_sleep = se.raw_call, se.time.sleep
        se.raw_call, se.time.sleep = boom, lambda s: slept.append(s)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                se.make_complete('gateway', 'm', 0)('p', 's')
            self.assertNotIsInstance(ctx.exception, se.BackendUnavailable)
        finally:
            se.raw_call, se.time.sleep = old_raw, old_sleep
        self.assertEqual(len(calls), 3, '瞬时限流应重试满 3 次')
        self.assertEqual(slept, [30, 30, 30])


class ConstantBaselineTest(unittest.TestCase):
    """偏斜语料上恒定预测就能拿高分——基线必须被算出来并摆进汇总。"""

    ITEMS = ([{'human': '真'}] * 1 + [{'human': '假'}] * 8
             + [{'human': '存疑'}] * 19)

    def test_constant_doubt_wins_strict_on_skewed_corpus(self):
        b = se.constant_baselines(self.ITEMS)
        self.assertAlmostEqual(b['存疑']['严格一致率'], 19 / 28 * 100, places=2)
        self.assertAlmostEqual(b['存疑']['方向一致率'], 27 / 28 * 100, places=2)
        self.assertGreater(b['存疑']['严格一致率'], b['真']['严格一致率'])
        self.assertGreater(b['存疑']['严格一致率'], b['假']['严格一致率'])

    def test_fn_zero_is_not_evidence_of_sensitivity(self):
        """「漏真FN=0」不能当灵敏度高——一律存疑也是 0。

        FN 只数「人工真 → 模型假」这种硬漏，存疑不算漏。所以零信息预测器
        同样能拿 FN=0，必须配合真标签召回率一起看。
        """
        b = se.constant_baselines(self.ITEMS)
        self.assertEqual(b['存疑']['漏真FN'], 0, '存疑不算硬漏，恒定存疑也是 0')
        self.assertEqual(b['假']['漏真FN'], 1, '一律假才是真漏')
        self.assertLess(b['存疑']['方向一致率'], 100.0,
                        '漏掉唯一真标签必须体现在方向分上')

    def test_balanced_corpus_strips_constant_advantage(self):
        items = ([{'human': '真'}] * 10 + [{'human': '假'}] * 10
                 + [{'human': '存疑'}] * 10)
        b = se.constant_baselines(items)
        for v in ('真', '假', '存疑'):
            self.assertAlmostEqual(b[v]['严格一致率'], 100 / 3, places=2)


class VarianceTest(unittest.TestCase):
    """重复试验必须给出方差，且不许把重复当独立观测冒充精度。"""

    @staticmethod
    def _run(strict_hits, n=28):
        rs = [{'llm': '真' if i < strict_hits else '假', 'human': '真'}
              for i in range(n)]
        return {'complete': True, 'results': rs, 'model_calls': n,
                'source': 'x'}

    def test_sd_reflects_trial_spread(self):
        s = se.summarize([self._run(10), self._run(20), self._run(15)])
        m = s['严格一致率']
        self.assertEqual(m['per_trial'], [round(10 / 28 * 100, 4),
                                          round(20 / 28 * 100, 4),
                                          round(15 / 28 * 100, 4)])
        self.assertGreater(m['sd'], 0.0, '三轮成绩不同就必须有方差')
        self.assertEqual(m['min'], min(m['per_trial']))
        self.assertEqual(m['max'], max(m['per_trial']))

    def test_single_trial_sd_is_zero_not_crash(self):
        s = se.summarize([self._run(14)])
        self.assertEqual(s['严格一致率']['sd'], 0.0)

    def test_per_trial_ci_uses_corpus_n_not_pooled(self):
        s = se.summarize([self._run(14)] * 5)
        self.assertEqual(len(s['wilson95_per_trial']), 5)
        for e in s['wilson95_per_trial']:
            self.assertEqual(e['n'], 28, '单轮区间分母必须是语料条数')
        pooled = s['wilson95']['严格']
        single = s['wilson95_per_trial'][0]['严格']
        self.assertLess(pooled[1] - pooled[0], single[1] - single[0],
                        '合并区间更窄——正是它反保守的证据')
        self.assertIn('反保守', s['wilson95_caveat'])

    def test_incomplete_trials_excluded_and_counted(self):
        bad = self._run(28)
        bad['complete'] = False
        s = se.summarize([self._run(14), bad])
        self.assertEqual(s['available'], 1)
        self.assertEqual(s['incomplete'], 1)

    def test_all_incomplete_reports_no_data(self):
        bad = self._run(28)
        bad['complete'] = False
        s = se.summarize([bad])
        self.assertEqual(s['available'], 0)
        self.assertIn('error', s)


class PartialArmsTest(unittest.TestCase):
    """只缺部分臂的模型必须点名，不许在表里显示一个 0 看着像「测到零」。

    实测缺口：synthetic 的 Kimi-K3.1 generic/vertical 各 5 个重复，
    guarded 一个单元格都没有；no_data 只抓全空模型、degraded_fallback 只抓
    回落排除，两者都不覆盖它。
    """

    ARMS = ['generic', 'vertical', 'guarded']

    @staticmethod
    def _row(label, **arms):
        return {'label': label,
                'arms': {a: arms.get(a, {'available': 0}) for a in
                         ('generic', 'vertical', 'guarded')}}

    def test_never_run_arm_is_declared(self):
        t = [self._row('Kimi-K3.1', generic={'available': 5},
                       vertical={'available': 5})]
        p = se.partial_arms(t, self.ARMS)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]['missing_arms'], ['guarded'])
        self.assertEqual(p[0]['reason'], '未执行')
        self.assertEqual(p[0]['measured_arms'], ['generic', 'vertical'])

    def test_fallback_excluded_arm_has_different_reason(self):
        t = [self._row('M', generic={'available': 5}, vertical={'available': 5},
                       guarded={'available': 0, 'degraded_fallback_cells': 5})]
        p = se.partial_arms(t, self.ARMS)
        self.assertEqual(p[0]['reason'], '跑过但回落被排除')

    def test_fully_empty_model_is_not_partial(self):
        """全空归 no_data 管，别在这里重复点名。"""
        self.assertEqual(se.partial_arms([self._row('X')], self.ARMS), [])

    def test_complete_model_is_not_partial(self):
        t = [self._row('Y', **{a: {'available': 5} for a in self.ARMS})]
        self.assertEqual(se.partial_arms(t, self.ARMS), [])

    def test_two_missing_arms_still_declared(self):
        t = [self._row('Z', generic={'available': 3})]
        p = se.partial_arms(t, self.ARMS)
        self.assertEqual(p[0]['missing_arms'], ['vertical', 'guarded'])
        self.assertEqual(p[0]['measured_arms'], ['generic'])


class FallbackExclusionTest(unittest.TestCase):
    """额度耗尽 → 护栏回落纯本地 → 判词合法 → complete=True。

    这条链原先会让一个「36% 判定根本没调模型」的单元被当成正常测量报出去。
    实测 Kimi-K3(网关) 在 holdout 上就是这样成了「护栏方向输给旧垂直」的唯一负例。
    """

    @staticmethod
    def _cell(trial=0, fallbacks=0, exempt=0, n=24):
        rs = []
        for i in range(n):
            if i < fallbacks:
                rs.append({'llm': '存疑', 'human': '真', 'model_called': False,
                           'parse_mode': 'error_fallback', 'source': 'local_only+fallback',
                           'err': "BackendUnavailable('429 Credits exhausted')"})
            elif i < fallbacks + exempt:
                # 豁免条目本来就不调模型，parse_mode 仍是 strict，不是回落
                rs.append({'llm': '假', 'human': '假', 'model_called': False,
                           'parse_mode': 'strict', 'source': 'local_gate'})
            else:
                rs.append({'llm': '真', 'human': '真', 'model_called': True,
                           'parse_mode': 'strict', 'source': 'local_plus_model'})
        return {'complete': True, 'trial': trial, 'results': rs,
                'model_calls': n - fallbacks - exempt}

    def test_fallback_cell_excluded_and_declared(self):
        s = se.summarize([self._cell(0), self._cell(1, fallbacks=9)])
        self.assertEqual(s['available'], 1)
        self.assertEqual(s['degraded_fallback_cells'], 1)
        self.assertEqual(s['degraded_fallback_items'], 9)
        self.assertIn('通道问题', s['degraded_fallback_note'])
        self.assertIn('r1', s['degraded_fallback_note'])

    def test_legacy_cell_without_count_field_still_detected(self):
        cell = self._cell(0, fallbacks=3)
        self.assertNotIn('fallbacks', cell)
        self.assertEqual(se.fallback_count(cell), 3)
        self.assertEqual(se.summarize([cell])['available'], 0)

    def test_count_field_cannot_hide_contamination(self):
        cell = self._cell(0, fallbacks=3)
        cell['fallbacks'] = 0          # 落盘计数与 results 不一致
        self.assertEqual(se.fallback_count(cell), 3, 'results 是唯一真源')
        self.assertEqual(se.summarize([cell])['available'], 0)

    def test_exempt_zero_call_items_are_not_fallbacks(self):
        s = se.summarize([self._cell(0, exempt=18)])
        self.assertEqual(s['available'], 1)
        self.assertNotIn('degraded_fallback_cells', s)

    def test_all_degraded_names_the_reason(self):
        s = se.summarize([self._cell(t, fallbacks=8) for t in range(5)])
        self.assertEqual(s['available'], 0)
        self.assertEqual(s['degraded_fallback_cells'], 5)
        self.assertIn('回落', s['error'])
        # 提前 return 的分支同样要带条数：漏掉它，汇总就会打印
        # 「5 个重复共 0 条回落」，而真值是 40 条（实测 Kimi-K3 网关是 43）。
        self.assertEqual(s['degraded_fallback_items'], 40)
        self.assertIn('40 条', s['degraded_fallback_note'])
        self.assertIn('通道问题', s['degraded_fallback_note'])

    def test_degraded_and_incomplete_are_counted_separately(self):
        bad = self._cell(2)
        bad['complete'] = False
        s = se.summarize([self._cell(0), self._cell(1, fallbacks=2), bad])
        self.assertEqual((s['available'], s['incomplete'], s['degraded_fallback_cells']),
                         (1, 1, 1))


class ParserVersionTest(unittest.TestCase):
    def test_stale_parser_forces_recompute(self):
        items = [{'序号': 1, 'case': 'c', 'human': '真',
                  'reasons': ['被动句偏多'], 'excerpt': '采用问卷法收集数据。'}]
        with tempfile.TemporaryDirectory() as td:
            old_here, old_mk = se.HERE, se.make_complete
            se.HERE = Path(td)
            se.make_complete = lambda b, m, t: (lambda p, s: canned_old())
            try:
                d1 = se.run_one('gateway', 'm', 'L', 'generic', 0, items)
                self.assertEqual(d1['parser_version'], se.PARSER_VERSION)
                self.assertTrue(d1['complete'])
                calls = []

                def counting(b, m, t):
                    calls.append(1)
                    return lambda p, s: canned_old()
                se.make_complete = counting
                se.run_one('gateway', 'm', 'L', 'generic', 0, items)
                self.assertEqual(calls, [], '同版本应 SKIP')
                p = Path(td) / f'{se.slug("m")}__generic__r0.json'
                d = json.loads(p.read_text(encoding='utf-8'))
                d['parser_version'] = 'v1-old'
                p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
                se.run_one('gateway', 'm', 'L', 'generic', 0, items)
                self.assertEqual(len(calls), 1, '解析器换代必须重算，不能静默 SKIP')
            finally:
                se.HERE, se.make_complete = old_here, old_mk

    def test_incomplete_cell_not_skipped(self):
        items = [{'序号': 1, 'case': 'c', 'human': '真',
                  'reasons': ['x'], 'excerpt': 'y'}]
        with tempfile.TemporaryDirectory() as td:
            old_here, old_mk = se.HERE, se.make_complete
            se.HERE = Path(td)
            se.make_complete = lambda b, m, t: (lambda p, s: '拒答')
            try:
                d = se.run_one('gateway', 'm', 'L', 'generic', 0, items)
                self.assertFalse(d['complete'])
                self.assertEqual(d['bad_items'], [1])
                calls = []

                def counting(b, m, t):
                    calls.append(1)
                    return lambda p, s: canned_old()
                se.make_complete = counting
                d2 = se.run_one('gateway', 'm', 'L', 'generic', 0, items)
                self.assertEqual(len(calls), 1)
                self.assertTrue(d2['complete'])
            finally:
                se.HERE, se.make_complete = old_here, old_mk


class CorpusIsolationTest(unittest.TestCase):
    def test_dirs_differ_per_corpus(self):
        old = se.HERE
        try:
            real = se.use_corpus_dir('real28')
            syn = se.use_corpus_dir('synthetic')
            self.assertNotEqual(real, syn, '共用目录会让 synthetic 覆盖 real28 结果')
            self.assertEqual(se.HERE, syn)
            self.assertEqual(real.name, '_style_eval', '真实语料沿用既有目录，旧结果不作废')
        finally:
            se.HERE = old

    def test_cache_shared_across_corpora(self):
        """缓存 key 含 prompt，不同语料天然不同 key——共用目录是安全的。"""
        old = se.HERE
        try:
            se.use_corpus_dir('real28')
            c1 = se.CACHE
            se.use_corpus_dir('synthetic')
            self.assertEqual(se.CACHE, c1)
        finally:
            se.HERE = old

    @needs_real28
    def test_real_and_synthetic_prompts_differ(self):
        real = se.load_real28()[0]
        import style_eval_corpus as corp
        syn = corp.items()[0]
        k = lambda it: se.cache_key('gateway', 'm', 'sys',
                                    st.build_triage_prompt(it['reasons'], it['excerpt']), 0)
        self.assertNotEqual(k(real), k(syn))


class BackendParamsTest(unittest.TestCase):
    """Kimi 官方温度恒为 1（平台强制），绝不能被记成主口径 0.2 混池比较。"""

    def test_kimi_params_differ_from_main(self):
        self.assertEqual(se.call_params('gateway'), (se.TEMPERATURE, se.MAX_TOKENS))
        self.assertEqual(se.call_params('official'), (se.TEMPERATURE, se.MAX_TOKENS))
        t, mt = se.call_params('kimi')
        self.assertEqual(t, 1.0)
        self.assertNotEqual((t, mt), (se.TEMPERATURE, se.MAX_TOKENS))

    def test_default_cache_key_unchanged(self):
        """加参数不能让既有 7000+ 条缓存全部失效。"""
        self.assertEqual(se.cache_key('gateway', 'm', 's', 'p', 0),
                         se.cache_key('gateway', 'm', 's', 'p', 0, None, None))
        self.assertEqual(se.cache_key('gateway', 'm', 's', 'p', 0),
                         se.cache_key('gateway', 'm', 's', 'p', 0,
                                      se.TEMPERATURE, se.MAX_TOKENS))

    def test_kimi_key_separated_by_effective_params(self):
        a = se.cache_key('kimi', 'm', 's', 'p', 0, *se.call_params('kimi'))
        b = se.cache_key('kimi', 'm', 's', 'p', 0)
        self.assertNotEqual(a, b, '温度不同必须落在不同 key，否则等于谎报口径')

    def test_trial_index_still_in_key(self):
        self.assertNotEqual(se.cache_key('kimi', 'm', 's', 'p', 0, 1.0, 4096),
                            se.cache_key('kimi', 'm', 's', 'p', 1, 1.0, 4096))


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class KimiThrottleTest(unittest.TestCase):
    """组织级 RPM=3 是**跨模型共享**的，限流必须全局，不能按模型各算各的。"""

    def test_rolling_window_caps_starts(self):
        clock = FakeClock()
        old_time, old_stamps = se.time, list(se._KIMI_STAMPS)
        se.time = clock
        se._KIMI_STAMPS.clear()
        try:
            for _ in range(se.KIMI_RPM):
                se.kimi_acquire()
            self.assertEqual(clock.slept, [], '窗口未满不该等')
            se.kimi_acquire()
            self.assertEqual(len(clock.slept), 1, '第 4 次必须等最旧那次滑出 60s')
            self.assertGreaterEqual(clock.now, 60.0)
            self.assertLessEqual(len(se._KIMI_STAMPS), se.KIMI_RPM)
        finally:
            se.time = old_time
            se._KIMI_STAMPS.clear()
            se._KIMI_STAMPS.extend(old_stamps)

    def test_window_slides_allows_later_burst(self):
        clock = FakeClock()
        old_time, old_stamps = se.time, list(se._KIMI_STAMPS)
        se.time = clock
        se._KIMI_STAMPS.clear()
        try:
            for _ in range(se.KIMI_RPM):
                se.kimi_acquire()
            clock.now += 61.0
            for _ in range(se.KIMI_RPM):
                se.kimi_acquire()
            self.assertEqual(clock.slept, [], '整窗滑出后应重新放行，不是永久 20s 串行')
        finally:
            se.time = old_time
            se._KIMI_STAMPS.clear()
            se._KIMI_STAMPS.extend(old_stamps)


class ReasonScrubTest(unittest.TestCase):
    #: 合成账号标识：运行时拼出来，源码里不留任何形似真 ID 的长串。
    #: （本夹具曾直接抄真实 429 正文里的 org-/ak-，被隐私门拦下后改成合成。）
    FAKE_ORG = 'org-' + '0123456789abcdef' * 2
    FAKE_AK = 'ak-' + 'abcdefghij0123456789'

    def test_account_ids_removed(self):
        raw = ('Your account ' + self.FAKE_ORG
               + '<' + self.FAKE_AK + '> request reached organization max RPM: 3')
        out = se.scrub_reason(raw)
        self.assertNotIn(self.FAKE_ORG, out)
        self.assertNotIn(self.FAKE_AK, out)
        self.assertIn('max RPM', out, '擦标识不该把诊断信息一起擦掉')

    def test_plain_reason_untouched(self):
        self.assertEqual(se.scrub_reason('额度已用尽'), '额度已用尽')


class CorpusGrowthTest(unittest.TestCase):
    """语料扩容后，旧单元格条目数不足——不许静默 SKIP，也不许回放冒充新分母。"""

    ITEMS1 = [{'序号': 1, 'case': 'c1', 'human': '真',
               'reasons': ['被动句偏多'], 'excerpt': '采用问卷法收集数据。'}]
    ITEMS2 = ITEMS1 + [{'序号': 2, 'case': 'c2', 'human': '假',
                        'reasons': ['空泛套话'], 'excerpt': '本研究具有重要意义。'}]

    def test_run_one_recomputes_when_item_count_grows(self):
        with tempfile.TemporaryDirectory() as td:
            old_here, old_mk = se.HERE, se.make_complete
            se.HERE = Path(td)
            calls = []

            def counting(b, m, t):
                calls.append(1)
                return lambda p, s: canned_old()
            se.make_complete = counting
            try:
                d1 = se.run_one('gateway', 'm', 'L', 'generic', 0, self.ITEMS1)
                self.assertEqual(d1['items'], 1)
                self.assertEqual(len(calls), 1)
                se.run_one('gateway', 'm', 'L', 'generic', 0, self.ITEMS1)
                self.assertEqual(len(calls), 1, '同分母应 SKIP')
                d2 = se.run_one('gateway', 'm', 'L', 'generic', 0, self.ITEMS2)
                self.assertEqual(len(calls), 2, '语料扩容必须重算，不能停在旧分母')
                self.assertEqual(d2['items'], 2)
            finally:
                se.HERE, se.make_complete = old_here, old_mk

    def test_replay_refuses_short_cell(self):
        with tempfile.TemporaryDirectory() as td:
            old_here, old_mk = se.HERE, se.make_complete
            se.HERE = Path(td)
            se.make_complete = lambda b, m, t: (lambda p, s: canned_old())
            try:
                se.run_one('gateway', 'm', 'L', 'guarded', 0, self.ITEMS1)
                p = Path(td) / f'{se.slug("m")}__guarded__r0.json'
                before = p.read_text(encoding='utf-8')
                got = se.replay_guarded('gateway', 'm', 'L', 0, self.ITEMS2)
                self.assertIsNone(got, '回放变不出新条目的响应，必须拒绝而不是盖版本戳')
                self.assertEqual(p.read_text(encoding='utf-8'), before)
            finally:
                se.HERE, se.make_complete = old_here, old_mk

    def test_replay_records_misses_and_summary_declares_them(self):
        runs = [{'complete': True, 'model_calls': 1, 'replay_misses': 3,
                 'results': [{'序号': 1, 'case': 'c', 'human': '真', 'llm': '真',
                              'source': 'local_plus_model'}]}]
        out = se.summarize(runs)
        self.assertEqual(out['stale_replay_items'], 3)
        self.assertIn('旧判词', out['stale_replay_note'])
        clean = se.summarize([dict(runs[0], replay_misses=0)])
        self.assertNotIn('stale_replay_items', clean)


if __name__ == '__main__':
    unittest.main(verbosity=2)