"""欠费/停用必须快速失败，且不得冒充「已测到」（零 API 调用）。

实测事故：Kimi 官方零余额回 HTTP 429 + 「is suspended due to insufficient
balance … billing details」。判定词其实**已在** QUOTA_PAT 里（billing），但
make_complete 把 repr(e) 截到 160 字符，而账号标识 org-<32hex>/ak-<id> 正好
占满这 160 字符 —— 判定词被切掉，于是被当成瞬时限流，28 条 ×3 次 ×30s
空转了 9.5 小时，还写出一堆 complete=False 的单元格。

本套件锁三件事：
    1. 先擦账号标识再截断，判定词必须活下来 → 立刻 BackendUnavailable；
    2. BackendUnavailable 必须一路传上去，不能被 judge_old / run_one 的
       宽 except 吞掉变成「失败判词」或「本地回落」——那等于把没测到写成测到了；
    3. 主循环中途遇到停用，必须停掉该后端剩余任务并写进 unavailable，
       不能静默从汇总里消失。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

import style_eval as se  # noqa: E402

BASE = Path(__file__).resolve().parent
TMP = BASE / '_quota_failfast_test_tmp'

#: 合成账号标识（全 a / 全 b），绝不使用真实 ID。长度复现实测：32 位 hex。
FAKE_ORG = 'org-' + 'a' * 32
FAKE_AK = 'ak-' + 'b' * 24
SUSPENDED = (
    "AgentError(\"Kimi（月之暗面） kimi-k3.1-pro 429 限流（已轮转 1 个 Key 仍失败）："
    "Error code: 429 - {'error': {'message': 'Your account " + FAKE_ORG + ' ' + FAKE_AK
    + " is suspended due to insufficient balance, please recharge your account"
      " or check your plan and billing details', 'type': 'exceeded_current_quota_error'}}\")")
OVERLOADED = ("Error code: 429 - {'error': {'message': 'The engine is currently"
              " overloaded, please try again later', 'type': 'engine_overloaded_error'}}")
ITEM = {'序号': 1, 'case': 'c1', 'human': '假',
        'reasons': ['空泛套话'], 'excerpt': '综上所述，本文具有重要的理论意义与现实意义。' * 6}


class QuotaDetectTest(unittest.TestCase):
    """make_complete：判定词必须活过截断，且只调一次、不 sleep。"""

    def setUp(self):
        self.calls = []
        self.sleeps = []
        p1 = mock.patch.object(se, 'raw_call', side_effect=self._raise)
        p2 = mock.patch.object(se.time, 'sleep', side_effect=self.sleeps.append)
        self.raw = p1.start()
        self.addCleanup(p1.stop)
        p2.start()
        self.addCleanup(p2.stop)
        self.exc = RuntimeError(SUSPENDED)

    def _raise(self, *a, **k):
        self.calls.append(a)
        raise self.exc

    def test_kimi_suspension_fails_fast(self):
        with self.assertRaises(se.BackendUnavailable) as cm:
            se.make_complete('kimi', 'kimi-k3.1-pro', 0)('p', 's')
        self.assertEqual(len(self.calls), 1, '欠费不该重试')
        self.assertEqual(self.sleeps, [], '欠费不该等待')
        self.assertIn('insufficient balance', str(cm.exception))

    def test_reason_carries_no_account_id(self):
        with self.assertRaises(se.BackendUnavailable) as cm:
            se.make_complete('kimi', 'kimi-k3.1-pro', 0)('p', 's')
        self.assertNotIn(FAKE_ORG, str(cm.exception))
        self.assertNotIn(FAKE_AK, str(cm.exception))

    def test_truncation_really_ate_the_keyword(self):
        """反证：旧的 160 字符截断确实命中不了任何判定词（事故根因）。"""
        old = (repr(self.exc)[:160] + ' ')[:400]
        self.assertFalse(any(p in old.lower() for p in se.QUOTA_PAT))
        self.assertTrue(any(p in SUSPENDED.lower() for p in se.QUOTA_PAT))

    def test_quota_type_without_billing_word(self):
        """只有 type 字段命中时也要判欠费 —— 证明新增词不是摆设。"""
        self.exc = RuntimeError(
            "Error code: 429 - {'error': {'message': 'request denied',"
            " 'type': 'exceeded_current_quota_error'}}")
        with self.assertRaises(se.BackendUnavailable):
            se.make_complete('kimi', 'kimi-k3.1-pro', 0)('p', 's')
        self.assertEqual(len(self.calls), 1)

    def test_transient_overload_is_not_quota(self):
        """瞬时过载仍走重试，不能被误判成欠费而放弃整个后端。"""
        self.exc = RuntimeError(OVERLOADED)
        with self.assertRaises(RuntimeError) as cm:
            se.make_complete('kimi', 'kimi-k3.1-pro', 0)('p', 's')
        self.assertNotIsInstance(cm.exception, se.BackendUnavailable)
        self.assertEqual(len(self.calls), 3, '瞬时错误应重试 3 次')
        self.assertEqual(self.sleeps, [30, 30, 30])

    def test_empty_content_still_retries(self):
        self.raw.side_effect = lambda *a, **k: '   '
        with self.assertRaises(RuntimeError) as cm:
            se.make_complete('kimi', 'kimi-k3.1-pro', 0)('p', 's')
        self.assertNotIsInstance(cm.exception, se.BackendUnavailable)
        self.assertEqual(self.raw.call_count, 3)


class PropagationTest(unittest.TestCase):
    """BackendUnavailable 不许被宽 except 吞成「测到了但失败」。"""

    def setUp(self):
        self.sleeps = []
        p = mock.patch.object(se.time, 'sleep', side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)
        self.boom = mock.Mock(side_effect=se.BackendUnavailable(SUSPENDED))

    def test_judge_old_propagates(self):
        with self.assertRaises(se.BackendUnavailable):
            se.judge_old(self.boom, se.SYSTEM_GENERIC, ITEM['reasons'], ITEM['excerpt'])
        self.assertEqual(self.boom.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_judge_old_transient_still_returns_failed(self):
        boom = mock.Mock(side_effect=RuntimeError(OVERLOADED))
        v, _r, err, mode = se.judge_old(boom, se.SYSTEM_GENERIC,
                                       ITEM['reasons'], ITEM['excerpt'])
        self.assertIsNone(v)
        self.assertEqual(mode, 'failed')
        self.assertIn('overloaded', err)
        self.assertEqual(boom.call_count, 3)


class RunOneTest(unittest.TestCase):
    """中途停用时不许落盘单元格：写了就等于宣称这一轮测过。"""

    def setUp(self):
        TMP.mkdir(exist_ok=True)
        self._old_here = se.HERE
        se.HERE = TMP
        self.addCleanup(self._restore)
        p = mock.patch.object(se.time, 'sleep', lambda *_a, **_k: None)
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        se.HERE = self._old_here
        shutil.rmtree(TMP, ignore_errors=True)

    def test_guarded_does_not_write_fallback_cell(self):
        with mock.patch.object(se, 'guarded_one',
                               side_effect=se.BackendUnavailable(SUSPENDED)):
            with self.assertRaises(se.BackendUnavailable):
                se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(list(TMP.glob('*.json')), [], '不得写出回落单元格')

    def test_generic_does_not_write_failed_cell(self):
        with mock.patch.object(se, 'judge_old',
                               side_effect=se.BackendUnavailable(SUSPENDED)):
            with self.assertRaises(se.BackendUnavailable):
                se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'generic', 0, [ITEM])
        self.assertEqual(list(TMP.glob('*.json')), [])

    def test_guarded_transient_still_falls_back(self):
        """单条瞬时失败仍按产品行为回落本地 —— 不能把这条也一并禁掉。"""
        with mock.patch.object(se, 'guarded_one', side_effect=RuntimeError('boom')):
            d = se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(d['fallbacks'], 1)
        self.assertEqual(d['results'][0]['parse_mode'], 'error_fallback')

    # ---- 复用判定：带回落的单元格不算「已完成」，否则永久 SKIP ----
    def _write_cell(self, mode, with_field=True):
        rec = {'序号': 1, 'case': 'c1', 'human': '假', 'llm': '存疑',
               'llm_reason': '', 'err': None, 'parse_mode': mode,
               'source': 'local_only', 'guards': [], 'model_called': False,
               'model_opinion': None}
        d = {'model': 'kimi-k3.1-pro', 'label': 'K', 'backend': 'kimi',
             'arm': 'guarded', 'trial': 0, 'items': 1, 'results': [rec],
             'complete': True, 'bad_items': [],
             'parser_version': se.PARSER_VERSION, 'salvaged': 0,
             'model_calls': 0}
        if with_field:
            d['fallbacks'] = 1 if mode == 'error_fallback' else 0
        p = TMP / 'kimi-k3.1-pro__guarded__r0.json'
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        return p

    def test_fallback_cell_is_not_skipped(self):
        self._write_cell('error_fallback')
        clean = {'verdict': '假', 'reason': 'r', 'source': 'local_plus_model',
                 'guards': [], 'model_called': True, 'model_opinion': '假',
                 'parse_mode': 'strict'}
        with mock.patch.object(se, 'guarded_one', return_value=clean) as g:
            d = se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(g.call_count, 1, '带回落的单元格必须重算，不能 SKIP')
        self.assertEqual(d['fallbacks'], 0)

    def test_fallback_cell_without_field_is_not_skipped(self):
        """旧进程不写 fallbacks 字段（读出来 None）——必须从 results 现算。"""
        self._write_cell('error_fallback', with_field=False)
        clean = {'verdict': '假', 'reason': 'r', 'source': 'local_plus_model',
                 'guards': [], 'model_called': True, 'model_opinion': '假',
                 'parse_mode': 'strict'}
        with mock.patch.object(se, 'guarded_one', return_value=clean) as g:
            se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(g.call_count, 1)

    def test_clean_cell_is_still_skipped(self):
        """干净单元格仍须零 API 跳过 —— 不能把缓存复用一并改坏。"""
        self._write_cell('exempt')
        with mock.patch.object(se, 'guarded_one') as g:
            d = se.run_one('kimi', 'kimi-k3.1-pro', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(g.call_count, 0)
        self.assertEqual(d['results'][0]['parse_mode'], 'exempt')


class MainLoopTest(unittest.TestCase):
    """主循环：中途停用 → 停掉该后端剩余任务 + 如实写进 unavailable。"""

    def setUp(self):
        TMP.mkdir(exist_ok=True)
        self._old_here = se.HERE
        self.addCleanup(self._restore)
        self.calls = []

    def _restore(self):
        se.HERE = self._old_here
        shutil.rmtree(TMP, ignore_errors=True)

    def _run_main(self):
        # 桩函数若瞬时返回，工作线程会在主线程处理失败之前跑完全部任务，
        # cancel() 一个都取消不掉 —— 测不出取消路径。让后续 Kimi 任务阻塞
        # 1s（主线程取消只需微秒级），留出确定可观察的窗口。
        block = threading.Event()

        def fake_run_one(b, m, lab, arm, trial, items):
            if b == 'kimi' and self.calls and self.calls[-1][0] == 'kimi':
                block.wait(1.0)
            self.calls.append((b, lab, arm, trial))
            if b == 'kimi':
                raise se.BackendUnavailable(SUSPENDED)
            return {'model': m, 'label': lab, 'backend': b, 'arm': arm,
                    'trial': trial, 'items': len(items), 'results': [],
                    'complete': True, 'bad_items': [],
                    'parser_version': se.PARSER_VERSION, 'salvaged': 0,
                    'fallbacks': 0, 'model_calls': 0}

        def fake_dir(corpus):
            se.HERE = TMP
            return TMP

        argv = ['style_eval.py', '--corpus', 'fake', '--backend', 'all',
                '--repeats', '3', '--workers', '1', '--arms', 'guarded']
        env = dict(os.environ)
        env['KIMI_API_KEY'] = 'sk-test-only-not-a-real-key-000000'
        env.pop('MIMO_API_KEY', None)
        buf = io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, env, clear=True))
            st.enter_context(mock.patch.dict(se.CORPORA, {'fake': lambda: [ITEM]}))
            st.enter_context(mock.patch.object(se, 'GATEWAY_MODELS', [('GW', 'gw-1')]))
            st.enter_context(mock.patch.object(se, 'use_corpus_dir', fake_dir))
            st.enter_context(mock.patch.object(se, 'preflight',
                                               lambda specs, timeout=45: (list(specs), [])))
            st.enter_context(mock.patch.object(se, 'run_one', fake_run_one))
            st.enter_context(mock.patch.object(se.time, 'sleep', lambda *_a, **_k: None))
            st.enter_context(mock.patch.object(sys, 'argv', argv))
            st.enter_context(contextlib.redirect_stdout(buf))
            se.main()
        return json.loads((TMP / '汇总_fake.json').read_text(encoding='utf-8')), buf.getvalue()

    def test_dead_backend_declared_and_remaining_cancelled(self):
        out, log = self._run_main()
        # 3 个网关重复跑完；Kimi 6 个任务里只有 2 个真进函数（首个失败 +
        # 一个已在飞、cancel 不掉的），其余 4 个必须在队列里被取消。
        self.assertEqual(len(self.calls), 5, f'实际调用 {self.calls}')
        self.assertEqual(log.count('取消 '), 4, log)
        self.assertEqual([r['label'] for r in out['models']], ['GW'])
        dead = out.get('unavailable') or []
        self.assertEqual(sorted(d['label'] for d in dead),
                         ['Kimi-K2.6(官方)', 'Kimi-K3.1-Pro(官方)'])
        for d in dead:
            self.assertIn('insufficient balance', d['reason'])
            self.assertNotIn(FAKE_ORG, d['reason'])
            self.assertNotIn(FAKE_AK, d['reason'])
        self.assertIn('停掉该后端剩余任务', log)
        self.assertIn('取消', log)


class PreflightTest(unittest.TestCase):
    """探活：瞬时过载要重试后才判死，欠费立刻判死。"""

    def setUp(self):
        self.sleeps = []
        p = mock.patch.object(se.time, 'sleep', side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)
        self.spec = [('kimi', 'kimi-k3.1-pro', 'K3')]

    def _call(self, side_effect):
        with mock.patch.object(se, 'raw_call', side_effect=side_effect) as rc:
            alive, dead = se.preflight(self.spec, retry_wait=20)
        return alive, dead, rc.call_count

    def test_transient_overload_retried_then_alive(self):
        seq = iter([RuntimeError(OVERLOADED), '{"ok":"是"}'])

        def side(*_a, **_k):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        alive, dead, n = self._call(side)
        self.assertEqual(alive, self.spec)
        self.assertEqual(dead, [])
        self.assertEqual(n, 2)
        self.assertEqual(self.sleeps, [20])

    def test_quota_declared_dead_immediately(self):
        alive, dead, n = self._call(mock.Mock(side_effect=RuntimeError(SUSPENDED)))
        self.assertEqual(alive, [])
        self.assertEqual(len(dead), 1)
        self.assertIn('insufficient balance', dead[0]['reason'])
        self.assertNotIn(FAKE_ORG, dead[0]['reason'])
        self.assertEqual(n, 1, '欠费不该重试')
        self.assertEqual(self.sleeps, [])

    def test_persistent_transient_declared_dead_after_attempts(self):
        alive, dead, n = self._call(mock.Mock(side_effect=RuntimeError(OVERLOADED)))
        self.assertEqual(alive, [])
        self.assertEqual(len(dead), 1)
        self.assertEqual(n, 4)
        self.assertEqual(self.sleeps, [20, 20, 20])

    def test_first_try_success_costs_no_wait(self):
        alive, dead, n = self._call(mock.Mock(return_value='{"ok":"是"}'))
        self.assertEqual(alive, self.spec)
        self.assertEqual(dead, [])
        self.assertEqual(n, 1)
        self.assertEqual(self.sleeps, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
