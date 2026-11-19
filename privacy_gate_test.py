# -*- coding: utf-8 -*-
"""隐私门测试。

设计约束：**真实禁止串绝不写进本文件**（写进来就等于重新提交进仓库）。
所以命中能力用「运行时注入合成禁止串」来验证机制，真实值只以哈希存在于
scripts/privacy_gate.py。另加两条集成测试，专门覆盖 v2.61 那轮造成假绿的坑：
中文路径八进制转义、以及把 git diff 的删除行误当成引入。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("privacy_gate", BASE / "scripts" / "privacy_gate.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

SYN = "测试禁止串"          # 6 字，合成，非真实个人信息
SYN_DIGIT = "9876543210"     # 10 位，合成
SYN_MAIL = "synthetic-probe@example.invalid"
SYN_MIXED = "Ab3混合串9z"     # 既非纯数字也非纯中文也非邮箱 → 必须走全文滑窗


def _inject():
    """临时把合成禁止串加进门，测完还原。"""
    return (
        (len(SYN), pg.sha(SYN), "合成中文串", "cjk"),
        (len(SYN_DIGIT), pg.sha(SYN_DIGIT), "合成数字串", "digit"),
        (len(SYN_MAIL), pg.sha(SYN_MAIL), "合成邮箱", "email"),
    )


class MechanismTest(unittest.TestCase):
    def setUp(self):
        self._orig = pg.FORBIDDEN
        pg.FORBIDDEN = self._orig + _inject()

    def tearDown(self):
        pg.FORBIDDEN = self._orig

    def test_catches_synthetic_cjk_and_reports_line(self):
        text = "第一行普通文字。\n第二行含" + SYN + "的内容。\n第三行。\n"
        hits = pg.scan_text(text)
        cats = [c for c, _ in hits]
        self.assertIn("合成中文串", cats)
        off = [o for c, o in hits if c == "合成中文串"][0]
        self.assertEqual(pg.line_of(text, off), 2)

    def test_catches_digit_embedded_in_longer_run(self):
        text = "编号 12" + SYN_DIGIT + "34 结束"
        self.assertIn("合成数字串", [c for c, _ in pg.scan_text(text)])

    def test_catches_synthetic_email(self):
        text = "联系 " + SYN_MAIL + " 获取"
        self.assertIn("合成邮箱", [c for c, _ in pg.scan_text(text)])

    def test_scan_result_never_contains_the_matched_text(self):
        text = "含" + SYN + "与 " + SYN_MAIL
        for cat, off in pg.scan_text(text):
            self.assertIsInstance(cat, str)
            self.assertIsInstance(off, int)
            self.assertNotIn(SYN, cat)
            self.assertNotIn(SYN_MAIL, cat)

    def test_unknown_kind_falls_back_to_full_slide(self):
        """未知形态必须退回全文滑窗，而不是静默跳过。"""
        orig = pg.FORBIDDEN
        pg.FORBIDDEN = orig + ((len(SYN_MIXED), pg.sha(SYN_MIXED), "合成混合串", "未知形态"),)
        try:
            text = "前文 " + SYN_MIXED + " 后文"
            self.assertIn("合成混合串", [c for c, _ in pg.scan_text(text)])
        finally:
            pg.FORBIDDEN = orig

    def test_mixed_literal_not_found_by_cheap_strategies(self):
        """反证：混合串确实不在数字/中文/邮箱任何一种廉价定位的覆盖里。"""
        self.assertEqual(pg._hits_in_runs(SYN_MIXED, len(SYN_MIXED),
                                          pg.sha(SYN_MIXED), pg.DIGIT_RUN), [])
        self.assertEqual(pg._hits_in_runs(SYN_MIXED, len(SYN_MIXED),
                                          pg.sha(SYN_MIXED), pg.CJK_RUN), [])

    def test_multiple_hits_all_reported(self):
        text = SYN + "\n" + SYN + "\n" + SYN_DIGIT
        hits = pg.scan_text(text)
        self.assertEqual(sum(1 for c, _ in hits if c == "合成中文串"), 2)
        self.assertEqual(pg.line_of(text, [o for c, o in hits if c == "合成中文串"][1]), 2)


class FalsePositiveTest(unittest.TestCase):
    def test_scrubbed_placeholders_are_clean(self):
        text = ("单位代码： 10000 密 级：公开\n学号 2022000000\n"
                "指导教师 示例老师\n为作者的账号昵称\n")
        self.assertEqual(pg.scan_text(text), [])

    def test_fixture_fake_keys_are_clean(self):
        for k in ("sk-user-supplied-key-12345", "sk-SECRET-TOKEN-1234567890"):
            self.assertEqual(pg.scan_text("raise RuntimeError(%r)" % k), [], k)

    def test_realistic_long_key_is_caught(self):
        fake = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
        self.assertIn("疑似 API Key（sk- 形态）", [c for c, _ in pg.scan_text(fake)])

    def test_anthropic_style_segmented_key_is_caught(self):
        fake = "sk-ant-api03-" + "Zk9mQ2vL7pR4tY8wJ1nB6cD3hF5gA0sE"
        self.assertIn("疑似 API Key（sk- 形态）", [c for c, _ in pg.scan_text(fake)])

    def test_empty_and_plain_text_clean(self):
        self.assertEqual(pg.scan_text(""), [])
        self.assertEqual(pg.scan_text("普通中文段落，没有任何标识。"), [])


class AccountIdTest(unittest.TestCase):
    """Kimi 429 正文回显的 org-/ak- 账号标识：真形态要拦，合成夹具要放行。"""

    CAT = "疑似平台账号标识（org-/ak- 形态）"
    #: 合成 ID 运行时拼出：源码里不留形似真 ID 的长串，否则本文件会被自己的门拦死。
    FAKE_ORG = "org-" + "0123456789abcdef" * 2
    FAKE_AK = "ak-" + "abcdefghij0123456789"

    def test_realistic_org_id_is_caught(self):
        self.assertIn(self.CAT,
                      [c for c, _ in pg.scan_text("account " + self.FAKE_ORG)])

    def test_realistic_ak_id_is_caught(self):
        self.assertIn(self.CAT, [c for c, _ in pg.scan_text(self.FAKE_AK)])

    def test_kimi_429_body_shape_is_caught(self):
        text = ("Your account " + self.FAKE_ORG + " " + self.FAKE_AK
                + " request reached organization max RPM: 3")
        self.assertEqual(len([c for c, _ in pg.scan_text(text) if c == self.CAT]), 2)

    def test_synthetic_single_char_fixtures_are_clean(self):
        """测试夹具用全 a / 全 b —— 单一字符重复的不是真标识，必须放行，
        否则 compat_agent_test 与公平性套件会被自己的钩子拦死。"""
        for fake in ("org-" + "a" * 32, "ak-" + "b" * 24):
            self.assertEqual(pg.scan_text(fake), [], fake)

    def test_scrubbed_placeholder_is_clean(self):
        self.assertEqual(
            pg.scan_text("Your account <账号标识已擦除> <账号标识已擦除> hit RPM"), [])

    def test_short_runs_are_not_flagged(self):
        self.assertEqual(pg.scan_text("org-12345 ak-abc"), [])

    def test_hit_never_contains_the_matched_text(self):
        for cat, off in pg.scan_text("x " + self.FAKE_ORG + " y"):
            self.assertNotIn(self.FAKE_ORG, cat)
            self.assertIsInstance(off, int)


class ForbiddenTableTest(unittest.TestCase):
    def test_hashes_are_wellformed(self):
        self.assertGreaterEqual(len(pg.FORBIDDEN), 5)
        for n, h, cat, kind in pg.FORBIDDEN:
            self.assertGreater(n, 0)
            self.assertEqual(len(h), 64)
            self.assertTrue(all(c in "0123456789abcdef" for c in h))
            self.assertTrue(cat)
            self.assertIn(kind, ("digit", "cjk", "email", "any"))

    def test_table_contains_no_plaintext_identifier(self):
        src = (BASE / "scripts" / "privacy_gate.py").read_text(encoding="utf-8")
        # 门自身不能含任何被它禁止的串
        self.assertEqual(pg.scan_text(src), [])


class GitIntegrationTest(unittest.TestCase):
    """造临时 git 仓库，覆盖两个真实踩过的坑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "probe@example.invalid")
        self._git("config", "user.name", "probe")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args, **kw):
        r = subprocess.run(["git", "-C", str(self.repo)] + list(args),
                           capture_output=True, **kw)
        return r

    def _run_gate(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(BASE / "scripts" / "privacy_gate.py")] + list(args),
            cwd=str(self.repo), capture_output=True, env=env)

    def test_chinese_filename_is_not_octal_escaped(self):
        name = "中文文件名测试.txt"
        (self.repo / name).write_text("干净内容\n", encoding="utf-8")
        self._git("add", name)
        # 直接在临时仓库里调 git，验证 -z 解码出真实中文名
        raw = subprocess.run(["git", "-C", str(self.repo), "diff", "--cached",
                              "--name-only", "-z", "--diff-filter=ACMR"],
                             capture_output=True).stdout
        decoded = [p for p in raw.decode("utf-8").split("\0") if p]
        self.assertEqual(decoded, [name])
        self.assertNotIn("\\", decoded[0])

    def test_staged_deletion_of_forbidden_string_is_not_flagged(self):
        """核心回归：扫 diff 会把删除行误报成引入。必须扫 staged blob。"""
        pg_path = BASE / "scripts" / "privacy_gate.py"
        f = self.repo / "doc.md"
        f.write_text("含" + SYN + "的旧内容\n", encoding="utf-8")
        self._git("add", "doc.md")
        self._git("commit", "-qm", "old")
        # 现在把禁止串删掉并 stage
        f.write_text("已脱敏的干净内容\n", encoding="utf-8")
        self._git("add", "doc.md")
        orig = pg.FORBIDDEN
        pg.FORBIDDEN = orig + _inject()
        try:
            paths = [p for p in subprocess.run(
                ["git", "-C", str(self.repo), "diff", "--cached", "--name-only",
                 "-z", "--diff-filter=ACMR"], capture_output=True
            ).stdout.decode("utf-8").split("\0") if p]
            self.assertEqual(paths, ["doc.md"])
            data = subprocess.run(["git", "-C", str(self.repo), "show", ":doc.md"],
                                  capture_output=True).stdout
            text = pg.decode(data)
            self.assertEqual(pg.scan_text(text), [])
            # 对照：diff 文本里确实还有禁止串（在 - 行上），证明扫 diff 会假阳性
            diff = subprocess.run(["git", "-C", str(self.repo), "diff", "--cached"],
                                  capture_output=True).stdout.decode("utf-8")
            self.assertIn(SYN, diff)
        finally:
            pg.FORBIDDEN = orig

    def test_staged_introduction_is_flagged(self):
        f = self.repo / "new.md"
        f.write_text("第一行\n第二行含" + SYN + "\n", encoding="utf-8")
        self._git("add", "new.md")
        orig = pg.FORBIDDEN
        pg.FORBIDDEN = orig + _inject()
        try:
            data = subprocess.run(["git", "-C", str(self.repo), "show", ":new.md"],
                                  capture_output=True).stdout
            hits = pg.scan_text(pg.decode(data))
            self.assertTrue(hits)
            self.assertEqual(pg.line_of(pg.decode(data), hits[0][1]), 2)
        finally:
            pg.FORBIDDEN = orig


class RepoAuditTest(unittest.TestCase):
    def test_whole_repo_is_clean(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, str(BASE / "scripts" / "privacy_gate.py"), "--all"],
                           cwd=str(BASE), capture_output=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0,
                         r.stdout.decode("utf-8", "replace")[-1500:])

    def test_selftest_passes(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, str(BASE / "scripts" / "privacy_gate.py"), "--selftest"],
                           cwd=str(BASE), capture_output=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)