"""三臂重试预算公平性 + 服务端等待提示（全 mock，零 API 调用）
==============================================================
事故：real28 两个官方 Kimi 的 guarded 臂全灭（各 12 条回落、可用重复=0），
而**同一通道同一时刻** generic/vertical 都是 28/28 成功。

探针取到的 429 全文（账号标识已擦除）：
    type=rate_limit_reached_error
    "request reached organization max RPM: 3, please try again after 1 seconds"
欠费判定词命中为空 —— 不是欠费，是瞬时限流。

根因是装置对护栏臂不公平：
    judge_old   外层 3 次 × make_complete 内层 3 次 = 9 次真调
    guarded_one 只调 complete 一次              = 3 次真调
RPM 打满时护栏臂预算只有旧双臂的 1/3，先耗尽就回落纯本地、整单元被排除，
于是「智能体让模型变差」的假象被装置自己造了出来。

本套件钉住：
    1. retry_wait 按服务端正文提示等待（1s 而非一律 30s），无提示才兜底
    2. 两臂在持续瞬时限流下真调次数**完全相同**（各 9 次）
    3. 反证：旧的「只调一次」写法确实只有 3 次，少于旧双臂
    4. 欠费仍然一次就放弃，不被新重试层放大
    5. 回落 err 先擦账号标识再截断，且截断后仍留着可诊断的 RPM 字样
    6. RPM 之外的第二道闸（组织级并发=1）也被遵守，且异常路径不漏还位子、
       缓存命中不占位（重放仍是零 API）
"""
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

import style_eval as se  # noqa: E402

BASE = Path(__file__).resolve().parent
TMP = BASE / '_retry_fairness_test_tmp'

#: 合成账号标识（全 a / 全 b），绝不使用真实 ID。
FAKE_ORG = 'org-' + 'a' * 32
FAKE_AK = 'ak-' + 'b' * 24
RPM_429 = (
    "Error code: 429 - {'error': {'message': 'Your account " + FAKE_ORG + ' '
    + FAKE_AK + " request reached organization max RPM: 3, please try again"
    " after 1 seconds', 'type': 'rate_limit_reached_error'}}")
SUSPENDED = (
    "Error code: 429 - {'error': {'message': 'Your account " + FAKE_ORG
    + " is suspended due to insufficient balance, please recharge your account"
      " or check your plan and billing details',"
      " 'type': 'exceeded_current_quota_error'}}")
OVERLOADED = ("Error code: 429 - {'error': {'message': 'The engine is currently"
              " overloaded, please try again later',"
              " 'type': 'engine_overloaded_error'}}")
ITEM = {'序号': 1, 'case': 'c1', 'human': '假',
        'reasons': ['空泛套话'], 'excerpt': '综上所述，本文具有重要的理论意义与现实意义。' * 6}

#: 每条最多真调次数：call_with_retry 外层 3 × make_complete 内层 3。
EXPECTED_ATTEMPTS = 9


class RetryWaitTest(unittest.TestCase):
    """服务端明说等 1 秒，就不该睡 30 秒。"""

    def test_body_hint_honored(self):
        self.assertEqual(se.retry_wait(RPM_429), 1.5)

    def test_fractional_hint(self):
        self.assertEqual(se.retry_wait('please try again after 2.5 seconds'), 3.0)

    def test_hint_case_insensitive(self):
        self.assertEqual(se.retry_wait('Please Try Again After 7 Seconds'), 7.5)

    def test_hint_survives_scrub_and_truncate(self):
        """真实链路里 last 先擦标识再截 600 —— 提示必须活下来。"""
        last = se.scrub_reason(repr(RuntimeError(RPM_429)))[:600]
        self.assertEqual(se.retry_wait(last), 1.5)

    def test_no_hint_429_falls_back_to_30(self):
        self.assertEqual(se.retry_wait(OVERLOADED), 30)

    def test_non_rate_error_waits_4(self):
        self.assertEqual(se.retry_wait('500 Internal Server Error'), 4)

    def test_zero_hint_still_waits(self):
        self.assertGreater(se.retry_wait('try again after 0 seconds'), 0)


class _BudgetBase(unittest.TestCase):
    """raw_call 持续瞬时限流，数真调次数与 sleep。"""

    def setUp(self):
        self.calls = []
        self.sleeps = []
        self.exc = RuntimeError(RPM_429)
        p1 = mock.patch.object(se, 'raw_call', side_effect=self._raise)
        p2 = mock.patch.object(se.time, 'sleep', side_effect=self.sleeps.append)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def _raise(self, *a, **k):
        self.calls.append(a)
        raise self.exc

    def _complete(self):
        return se.make_complete('kimi', 'kimi-k2.6', 0)

    def _count_guarded(self):
        self.calls.clear()
        self.sleeps.clear()
        with self.assertRaises(RuntimeError):
            se.guarded_one(ITEM['reasons'], ITEM['excerpt'], self._complete())
        return len(self.calls)

    def _count_old(self):
        self.calls.clear()
        self.sleeps.clear()
        v, _r, err, mode = se.judge_old(self._complete(), se.SYSTEM_GENERIC,
                                        ITEM['reasons'], ITEM['excerpt'])
        self.assertIsNone(v)
        self.assertEqual(mode, 'failed')
        self.assertTrue(err)
        return len(self.calls)


class FairBudgetTest(_BudgetBase):
    """两臂预算必须一样 —— 这是「装置不许让护栏臂看起来更差」的硬契约。"""

    def test_both_arms_make_same_api_attempts(self):
        self.assertEqual(self._count_old(), self._count_guarded())

    def test_old_arm_budget_is_nine(self):
        self.assertEqual(self._count_old(), EXPECTED_ATTEMPTS)

    def test_guarded_budget_is_nine(self):
        self.assertEqual(self._count_guarded(), EXPECTED_ATTEMPTS)

    def test_old_single_call_only_three(self):
        """反证：护栏臂旧写法（只调 complete 一次）确实只有 3 次，少于旧双臂。

        这条把事故根因钉成可执行事实：3 < 9，限流下先耗尽的是护栏臂。
        """
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self._complete()('p', 's')
        self.assertEqual(len(self.calls), 3)
        self.assertLess(len(self.calls), self._count_old())

    def test_all_sleeps_use_server_hint(self):
        self._count_guarded()
        self.assertTrue(self.sleeps)
        self.assertEqual(set(self.sleeps), {1.5},
                         '不得再一律睡 30s（服务端明说等 1 秒）')

    def test_budget_source_is_single_constant(self):
        """预算只有一处真源，两臂都走它，不会再各自漂移。"""
        self.assertEqual(se.API_OUTER_ATTEMPTS, 3)
        with mock.patch.object(se, 'call_with_retry',
                               side_effect=RuntimeError('x')) as c:
            with self.assertRaises(RuntimeError):
                se.guarded_one(ITEM['reasons'], ITEM['excerpt'], self._complete())
            self.assertEqual(c.call_count, 1)


class QuotaNotAmplifiedTest(_BudgetBase):
    """新增重试层不许把欠费的「一次就放弃」放大成 9 次白烧。"""

    def setUp(self):
        super().setUp()
        self.exc = RuntimeError(SUSPENDED)

    def test_guarded_quota_fails_fast(self):
        self.calls.clear()
        self.sleeps.clear()
        with self.assertRaises(se.BackendUnavailable):
            se.guarded_one(ITEM['reasons'], ITEM['excerpt'], self._complete())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_old_quota_fails_fast(self):
        self.calls.clear()
        self.sleeps.clear()
        with self.assertRaises(se.BackendUnavailable):
            se.judge_old(self._complete(), se.SYSTEM_GENERIC,
                         ITEM['reasons'], ITEM['excerpt'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sleeps, [])


class CallWithRetryTest(unittest.TestCase):
    """共用入口自身的契约。"""

    def test_success_first_try_no_sleep(self):
        sleeps = []
        with mock.patch.object(se.time, 'sleep', side_effect=sleeps.append):
            out = se.call_with_retry(lambda p, s: 'ok', 'p', 's')
        self.assertEqual(out, 'ok')
        self.assertEqual(sleeps, [])

    def test_quota_propagates_without_retry(self):
        calls = []

        def boom(p, s):
            calls.append(p)
            raise se.BackendUnavailable(SUSPENDED)

        with mock.patch.object(se.time, 'sleep', lambda *_a: None):
            with self.assertRaises(se.BackendUnavailable):
                se.call_with_retry(boom, 'p', 's')
        self.assertEqual(len(calls), 1)

    def test_transient_then_success(self):
        seq = iter([RuntimeError(RPM_429), 'ok'])
        sleeps = []

        def side(p, s):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        with mock.patch.object(se.time, 'sleep', side_effect=sleeps.append):
            self.assertEqual(se.call_with_retry(side, 'p', 's'), 'ok')
        self.assertEqual(sleeps, [1.5])


class FallbackReasonTest(unittest.TestCase):
    """回落 err 既要擦掉账号标识，又要留着能诊断的正文。"""

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

    def test_err_scrubbed_and_diagnosable(self):
        with mock.patch.object(se, 'raw_call',
                               side_effect=RuntimeError(RPM_429)):
            d = se.run_one('kimi', 'kimi-k2.6', 'K', 'guarded', 0, [ITEM])
        self.assertEqual(d['fallbacks'], 1)
        err = d['results'][0]['err']
        self.assertNotIn(FAKE_ORG, err)
        self.assertNotIn(FAKE_AK, err)
        self.assertIn('<账号标识已擦除>', err)
        self.assertIn('RPM', err, '截断不得把诊断关键词吃掉（上一个事故就是这么来的）')
        self.assertIn('rate_limit_reached_error', err)


class KimiConcurrencyTest(unittest.TestCase):
    """RPM 之外还有第二道闸：组织级并发=1。

    实测 429 正文 "request reached max organization concurrency: 1"。
    只控 RPM 时 3 个 worker 各自合规发起却在途重叠，照样被打回——
    real28 的 K2.6 vertical r1 第 25 条因此耗尽 9 次重试、整单元作废。
    """

    CONC_429 = (
        "Error code: 429 - {'error': {'message': 'Your account " + FAKE_ORG
        + " request reached max organization concurrency: 1, please try again"
          " after 1 seconds', 'type': 'rate_limit_reached_error'}}")

    def setUp(self):
        # 本类只验并发闸，不验 RPM：把 RPM 闸停掉，免得测试真睡 60s。
        p = mock.patch.object(se, 'kimi_acquire', lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self._sem = se._KIMI_INFLIGHT

    def _drained(self):
        """信号量是否已全额归还（不阻塞地取满再放回）。"""
        got = 0
        while self._sem.acquire(blocking=False):
            got += 1
        for _ in range(got):
            self._sem.release()
        return got

    def test_limit_matches_platform_and_semaphore(self):
        self.assertEqual(se.KIMI_CONCURRENCY, 1)
        self.assertEqual(self._drained(), se.KIMI_CONCURRENCY)

    def test_concurrency_body_hint_is_parsed(self):
        self.assertEqual(se.retry_wait(self.CONC_429), 1.5)

    def test_inflight_never_exceeds_one(self):
        import threading
        import time as _t
        from concurrent.futures import ThreadPoolExecutor
        lock = threading.Lock()
        state = {'now': 0, 'peak': 0}

        class FakeAgent:
            def __init__(self, **_kw):
                pass

            def complete(self, *_a, **_k):
                with lock:
                    state['now'] += 1
                    state['peak'] = max(state['peak'], state['now'])
                _t.sleep(0.02)          # 制造重叠窗口
                with lock:
                    state['now'] -= 1
                return '{"verdict":"假","reason":"x"}'

        with mock.patch('agents.KimiAgent', FakeAgent), \
                mock.patch.object(se, 'cache_put', lambda *_a, **_k: None):
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(lambda i: se.raw_call(
                    'kimi', 'kimi-k2.6', 's', f'p{i}', 0, use_cache=False),
                    range(8)))
        self.assertEqual(state['peak'], 1, '并发闸失效：在途请求重叠了')
        self.assertEqual(state['now'], 0)
        self.assertEqual(self._drained(), se.KIMI_CONCURRENCY)

    def test_slot_released_when_call_raises(self):
        body = self.CONC_429

        class Boom:
            def __init__(self, **_kw):
                pass

            def complete(self, *_a, **_k):
                raise RuntimeError(body)

        with mock.patch('agents.KimiAgent', Boom), \
                mock.patch.object(se, 'cache_put', lambda *_a, **_k: None):
            for i in range(4):
                with self.assertRaises(RuntimeError):
                    se.raw_call('kimi', 'kimi-k2.6', 's', f'p{i}', 0,
                                use_cache=False)
        self.assertEqual(self._drained(), se.KIMI_CONCURRENCY,
                         '异常路径漏还并发位 → 后续调用全部死锁')

    def test_cache_hit_does_not_take_slot(self):
        """重放必须零 API、零占位：否则离线复算会被并发闸串行拖死。"""
        with mock.patch.object(se, 'cache_get', return_value='cached'), \
                mock.patch('agents.KimiAgent', side_effect=AssertionError('不该真调')):
            self._sem.acquire()          # 把唯一的位子占住
            try:
                out = se.raw_call('kimi', 'kimi-k2.6', 's', 'p', 0)
            finally:
                self._sem.release()
        self.assertEqual(out, 'cached')


if __name__ == '__main__':
    unittest.main(verbosity=2)