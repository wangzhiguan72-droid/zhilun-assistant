# -*- coding: utf-8 -*-
"""版本戳审计的安全性质测试（零 API、零网络、不碰真实结果目录）。

核心不变量：**戳是证据不是标签**。只有「缓存全命中 + 判词逐条一致 +
解析模式逐条一致」的单元格才算 clean，才允许改戳。任何一条不满足都必须
拦下来——否则一次静默改戳就把过期结果冒充成当前口径的结果。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

import style_eval as se  # noqa: E402
import style_parser_stamp_audit as A  # noqa: E402

ITEM = {"序号": 1, "case": "t-1", "human": "假",
        "reasons": ["空泛套话"], "excerpt": "综上所述，本研究具有重要意义。"}
BY_NO = {1: ITEM}


def cell(arm="generic", verdict="假", mode="strict"):
    return {"model": "M", "label": "M(测试)", "backend": "gateway", "arm": arm,
            "trial": 0, "items": 1, "parser_version": "v2-salvage",
            "results": [{"序号": 1, "case": "t-1", "human": "假",
                         "llm": verdict, "llm_reason": "r", "err": None,
                         "model_called": True, "parse_mode": mode}]}


def raw(verdict="假"):
    return json.dumps({"verdict": verdict, "reason": "r"}, ensure_ascii=False)


class AuditCellTest(unittest.TestCase):
    def setUp(self):
        self._cache_get = se.cache_get
        self._cache_key = se.cache_key
        self._sleep = se.time.sleep
        se.time.sleep = lambda *a, **k: None
        self.tmp = Path(se.BASE) / "_stamp_audit_test_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.path = self.tmp / "M__generic__r0.json"

    def tearDown(self):
        se.cache_get = self._cache_get
        se.cache_key = self._cache_key
        se.time.sleep = self._sleep
        for p in self.tmp.glob("*.json"):
            p.unlink()
        self.tmp.rmdir()

    def write(self, c):
        self.path.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        return self.path

    def test_guarded_arm_is_out_of_scope(self):
        """护栏臂由 --replay 负责，本审计不得插手（它会真重算判定）。"""
        self.assertIsNone(A.audit_cell(self.write(cell(arm="guarded")), BY_NO))

    def test_identical_replay_is_clean(self):
        se.cache_get = lambda k: raw("假")
        r = A.audit_cell(self.write(cell()), BY_NO)
        self.assertTrue(r["clean"])
        self.assertEqual(r["diffs"], [])
        self.assertEqual(r["mode_diffs"], {})
        self.assertEqual(r["reasons"], {"ok": 1})

    def test_verdict_change_blocks_clean(self):
        se.cache_get = lambda k: raw("真")
        r = A.audit_cell(self.write(cell(verdict="假")), BY_NO)
        self.assertFalse(r["clean"])
        self.assertEqual(r["diffs"], [{"序号": 1, "case": "t-1",
                                       "old": "假", "new": "真"}])

    def test_parse_mode_change_blocks_clean(self):
        """判词相同但解析路径不同也不算一致：抢救出来的和严格解析出来的
        可信度不同，混在一个戳下报出去会掩盖截断率的变化。"""
        se.cache_get = lambda k: raw("假")
        r = A.audit_cell(self.write(cell(mode="salvaged")), BY_NO)
        self.assertFalse(r["clean"])
        self.assertEqual(r["diffs"], [])
        self.assertEqual(r["mode_diffs"], {("salvaged", "strict"): 1})

    def test_cache_miss_blocks_clean(self):
        se.cache_get = lambda k: None
        r = A.audit_cell(self.write(cell()), BY_NO)
        self.assertFalse(r["clean"])
        self.assertEqual(r["reasons"], {"miss": 1})
        self.assertEqual(r["diffs"], [], "取不到响应不得凭空造差异")

    def test_missing_corpus_item_blocks_clean(self):
        se.cache_get = lambda k: raw("假")
        r = A.audit_cell(self.write(cell()), {})
        self.assertFalse(r["clean"])
        self.assertEqual(r["reasons"], {"no-item": 1})

    def test_each_arm_replays_its_own_system_prompt(self):
        """generic/vertical 的系统提示不同 → 缓存 key 不同。
        若两臂共用一个 key，就会拿另一臂的响应来「证明」本臂一致。"""
        seen = []
        se.cache_key = lambda b, m, system, p, t, temp, mt: (
            seen.append(system) or "k")
        se.cache_get = lambda k: raw("假")
        for arm, want in (("generic", se.SYSTEM_GENERIC),
                          ("vertical", se.SYSTEM_VERTICAL)):
            seen.clear()
            p = self.tmp / f"M__{arm}__r0.json"
            p.write_text(json.dumps(cell(arm=arm), ensure_ascii=False),
                         encoding="utf-8")
            r = A.audit_cell(p, BY_NO)
            self.assertTrue(r["clean"], arm)
            self.assertEqual(seen, [want], arm)
        self.assertNotEqual(se.SYSTEM_GENERIC, se.SYSTEM_VERTICAL)

    def test_excerpt_window_matches_run_one(self):
        """旧双臂喂的是 excerpt[:500]。窗口取错就重建不出当初的 key，
        审计会全量 miss 却看起来像「缓存坏了」。"""
        long_item = dict(ITEM, excerpt="字" * 900)
        seen = []
        se.cache_key = lambda b, m, s, p, t, temp, mt: (seen.append(p) or "k")
        se.cache_get = lambda k: raw("假")
        A.audit_cell(self.write(cell()), {1: long_item})
        self.assertEqual(len(seen), 1)
        self.assertIn("字" * 500, seen[0])
        self.assertNotIn("字" * 501, seen[0])


class RestampGateTest(unittest.TestCase):
    """真跑 main()：改戳只允许落在 clean 单元格上，且必须留下原戳。"""

    def setUp(self):
        self._corpora, self._dir = se.CORPORA, se.use_corpus_dir
        self._cache_get, self._argv = se.cache_get, sys.argv
        self._sleep = se.time.sleep
        se.time.sleep = lambda *a, **k: None
        self.tmp = Path(se.BASE) / "_stamp_gate_test_tmp"
        self.tmp.mkdir(exist_ok=True)
        se.CORPORA = {"fake": lambda: [ITEM]}
        se.use_corpus_dir = lambda name: self.tmp
        se.cache_get = lambda k: raw("假")

    def tearDown(self):
        se.CORPORA, se.use_corpus_dir = self._corpora, self._dir
        se.cache_get, sys.argv = self._cache_get, self._argv
        se.time.sleep = self._sleep
        for p in self.tmp.glob("*.json"):
            p.unlink()
        self.tmp.rmdir()

    def put(self, arm, verdict):
        p = self.tmp / f"M__{arm}__r0.json"
        p.write_text(json.dumps(cell(arm=arm, verdict=verdict), ensure_ascii=False),
                     encoding="utf-8")
        return p

    def run_main(self, *flags):
        sys.argv = ["audit", "--corpus", "fake", *flags]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = A.main()
        return rc, buf.getvalue()

    def read(self, p):
        return json.loads(p.read_text(encoding="utf-8"))

    def test_clean_cell_restamped_and_original_kept(self):
        p = self.put("generic", "假")
        rc, out = self.run_main("--restamp")
        self.assertEqual(rc, 0, out)
        d = self.read(p)
        self.assertEqual(d["parser_version"], se.PARSER_VERSION)
        self.assertEqual(d["restamped_from"], "v2-salvage")
        self.assertIn("已改戳 1 个", out)

    def test_dirty_cell_never_restamped(self):
        """判词对不上就是结果过期，改戳等于伪造证据。"""
        good, bad = self.put("generic", "假"), self.put("vertical", "真")
        rc, out = self.run_main("--restamp")
        self.assertEqual(rc, 1, "有差异必须非零退出，不许报绿")
        self.assertEqual(self.read(good)["parser_version"], se.PARSER_VERSION)
        self.assertEqual(self.read(bad)["parser_version"], "v2-salvage")
        self.assertNotIn("restamped_from", self.read(bad))
        self.assertIn("不得改戳", out)

    def test_audit_only_writes_nothing(self):
        p = self.put("generic", "假")
        before = p.read_text(encoding="utf-8")
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_cache_miss_cell_not_restamped_even_without_diffs(self):
        """没验到 ≠ 验过一致。缺失条目必须拦住改戳。"""
        p = self.put("generic", "假")
        se.cache_get = lambda k: None
        rc, out = self.run_main("--restamp")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(p)["parser_version"], "v2-salvage")
        self.assertIn("已改戳 0 个", out)
        self.assertIn("有缓存缺失条目未验到", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
