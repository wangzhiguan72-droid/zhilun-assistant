"""CI 契约核查：真实语料 real28 缺席时，评测套件必须显式跳过而不是报错。

real28 的段落与台账经 .git/info/exclude 排除在仓库外，新克隆上必然不存在。
本脚本把这两个文件伪装成不存在，再跑三个依赖语料的套件，断言：
    1. 退出码 0（不打红全量回归）；
    2. 确实产生了 skip（不是把断言悄悄删掉换来的绿）；
    3. 没有 error / failure。

用法：.venv/Scripts/python.exe scripts/real28_absent_check.py
"""
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'scripts'))

PRIVATE = {'_复核抽样_风格段落.json', '复核台账-2026-10-21.csv'}
SUITES = ['style_eval_test', 'style_eval_corpus_test', 'style_eval_holdout_test']

_real_exists = pathlib.Path.exists


def _absent(self):
    if self.name in PRIVATE:
        return False
    return _real_exists(self)


def main():
    pathlib.Path.exists = _absent
    ok = True
    try:
        for name in SUITES:
            mod = __import__(name)
            suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
            skipped = len(res.skipped)
            good = res.wasSuccessful() and skipped > 0
            ok = ok and good
            print(f'[{"OK " if good else "BAD"}] {name}: '
                  f'run={res.testsRun} skip={skipped} '
                  f'fail={len(res.failures)} err={len(res.errors)}')
            for t, why in res.skipped:
                print(f'        跳过 {t.id().split(".")[-1]} → {why}')
            if not res.wasSuccessful():
                for t, tb in (res.failures + res.errors)[:2]:
                    print('        ', t.id(), tb.strip().splitlines()[-1][:160])
    finally:
        pathlib.Path.exists = _real_exists
    print('\n结论：', '语料缺席时如实跳过，CI 契约成立' if ok else '契约不成立，必须修')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
