# -*- coding: utf-8 -*-
"""共享服务夹具的端到端测试：真起服务、真跑那 4 个原本 rc=2 的套件。

这是「测试没跑 ≠ 测试通过」那条纪律的验收：服务起来后，它们必须真的执行并给出
通过/失败计数，而不是 rc=2 或静默 SKIP。
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "scripts"))

import test_server as ts  # noqa: E402

PY = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable

#: 原本单独跑必 rc=2 的四个套件（走真实 HTTP，不是 test_client）
SERVER_SUITES = ("methods_test", "paper_check_test", "regression_audit_test", "smoke_test")

#: 用 5010 而不是 5000：runner 自己会在 5000 起共享服务，本套件必须能与之并存。
PORT = 5010
SUITE_ENV = {"ZHILUN_TEST_BASE": f"http://127.0.0.1:{PORT}"}


def run_suite(name, extra_env=None, args=()):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONUNBUFFERED="1", NO_PROXY="127.0.0.1,localhost,::1")
    env.update(SUITE_ENV)
    env.update(extra_env or {})
    r = subprocess.run([PY, "-u", str(BASE / (name + ".py")), *args],
                       cwd=str(BASE), capture_output=True, env=env, timeout=900)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


class EnvTest(unittest.TestCase):
    def test_credentials_stripped(self):
        env = ts.child_env(5000)
        leaked = [k for k in env if k.upper().endswith(ts.CRED_SUFFIX)]
        self.assertEqual(leaked, [], "测试服务不得携带任何凭据")
        self.assertEqual(env["RATE_LIMIT_DISABLE"], "1")
        self.assertEqual(env["HOST"], "127.0.0.1", "不得绑 0.0.0.0 暴露到局域网")
        self.assertEqual(env["DEBUG"], "0")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    def test_does_not_mutate_parent_env(self):
        before = dict(os.environ)
        ts.child_env(5001)
        self.assertEqual(dict(os.environ), before)


class LiveServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ts.TestServer(port=PORT)
        try:
            cls.srv.start()
        except RuntimeError as e:
            raise unittest.SkipTest(f"服务起不来：{e}") from e

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_health(self):
        with urllib.request.urlopen(self.srv.base + "/health", timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_no_credentials_in_server_process(self):
        """/health 不该报出任何已配置的平台 Key。"""
        with urllib.request.urlopen(self.srv.base + "/health", timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
        self.assertNotIn("sk-", body)

    def test_rate_limit_off(self):
        """连打 15 次不该吃 429——这些套件就是这么打的。"""
        codes = []
        for _ in range(15):
            try:
                with urllib.request.urlopen(self.srv.base + "/health", timeout=5) as r:
                    codes.append(r.status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        self.assertNotIn(429, codes, codes)

    def test_four_suites_really_execute(self):
        """rc=0 不够：还要看到套件自己的完成标记，且没有 ✗ 失败。

        methods_test 的 run() 失败时只 print ✗ 并 return None，不抛异常——
        光看退出码会把「七个方法全失败」当成通过。这里按输出核对。
        """
        markers = {"methods_test": "✅ 已测试：",
                   "paper_check_test": "✅ 论文排查冒烟测试通过",
                   "regression_audit_test": "✅ 回归分析专项测试通过",
                   "smoke_test": "✅ 所有冒烟测试通过"}
        problems = []
        for name in SERVER_SUITES:
            rc, txt = run_suite(name)
            if rc == 2:
                problems.append(f"{name} 仍 rc=2（服务未就绪）")
            elif rc != 0:
                problems.append(f"{name} rc={rc}\n{txt[-1200:]}")
            if "[SKIP]" in txt:
                problems.append(f"{name} 仍有 SKIP 用例")
            if markers[name] not in txt:
                problems.append(f"{name} 没跑到收尾标记，疑似中途退出")
            if "✗ 失败" in txt:
                problems.append(f"{name} 有用例失败但退出码没反映：\n"
                                + "\n".join(l for l in txt.splitlines() if "✗" in l))
            if name == "methods_test":
                n = txt.count("✓ 方法")
                if n != 7:
                    problems.append(f"methods_test 只跑了 {n}/7 个方法")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_server_dependent_skip_cases_now_run(self):
        """ai_audit / defense_pack / table_cross 的端到端段原本静默 SKIP。"""
        problems = []
        for name in ("ai_audit_test", "defense_pack_test", "table_cross_test"):
            rc, txt = run_suite(name)
            if rc != 0:
                problems.append(f"{name} rc={rc}\n{txt[-800:]}")
            elif "[SKIP]" in txt:
                problems.append(f"{name} 仍有 SKIP：\n"
                                + "\n".join(l for l in txt.splitlines() if "[SKIP]" in l))
        self.assertEqual(problems, [], "\n".join(problems))


class PortGuardTest(unittest.TestCase):
    def test_busy_port_refuses_not_reuses(self):
        with ts.TestServer(port=PORT) as srv:
            self.assertTrue(ts.port_busy(PORT))
            second = ts.TestServer(port=PORT)
            with self.assertRaises(RuntimeError) as cm:
                second.start()
            self.assertIn("已被占用", str(cm.exception))
            self.assertIsNone(second.proc, "拒绝后不得留下进程")
            self.assertTrue(srv._healthy())

    def test_stop_releases_port(self):
        srv = ts.TestServer(port=PORT).start()
        srv.stop()
        self.assertFalse(ts.port_busy(PORT), "停止后端口必须释放")


if __name__ == "__main__":
    unittest.main(verbosity=2)