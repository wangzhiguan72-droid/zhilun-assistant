# -*- coding: utf-8 -*-
"""install_hooks 的端到端测试：在临时仓库里真的 git commit，验证拦得住。

不写任何真实隐私串。拦截能力用**合成的长 API Key**验证——那走的是 privacy_gate
里 KEY_RE 的真实检测路径，与真实密钥同一条代码分支。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('install_hooks', BASE / 'scripts/install_hooks.py')
ih = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ih)

#: 长到足以越过 ALNUM_RUN 的 20 字符门槛，形态与真实密钥一致。
#: 运行时拼装——字面量若直接写进本文件，隐私门会把测试文件自己拦下来。
FAKE_KEY = 'sk-' + 'a1b2c3d4' * 4
CLEAN = '这是一段干净的说明文字，没有任何标识符。\n'


def git(repo, *args, **kw):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
               GIT_CONFIG_NOSYSTEM='1', HOME=str(repo))
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                          text=True, encoding='utf-8', errors='replace', env=env, **kw)


class HookRepoTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)
        git(self.repo, 'init', '-q')
        (self.repo / 'scripts').mkdir()
        shutil.copy2(BASE / 'scripts/privacy_gate.py', self.repo / 'scripts/privacy_gate.py')
        r = ih.install(self.repo)
        self.assertEqual(r, 0)
        self.hook = self.repo / '.git/hooks/pre-commit'

    def tearDown(self):
        self._td.cleanup()

    def commit(self, name, text):
        (self.repo / name).write_text(text, encoding='utf-8')
        git(self.repo, 'add', name)
        return git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                   'commit', '-m', 'test')

    def test_hook_is_lf_and_executable(self):
        raw = self.hook.read_bytes()
        self.assertNotIn(b'\r\n', raw, 'CRLF 会让 sh 找不到解释器')
        self.assertTrue(raw.startswith(b'#!/bin/sh\n'))
        self.assertTrue(os.access(self.hook, os.X_OK))

    def test_clean_commit_passes(self):
        r = self.commit('干净文件.txt', CLEAN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_key_commit_blocked(self):
        r = self.commit('泄漏.txt', f'配置如下：\nAPI_KEY={FAKE_KEY}\n')
        self.assertNotEqual(r.returncode, 0, '长密钥必须拦住')
        self.assertIn('隐私门', r.stdout + r.stderr)
        self.assertNotIn(FAKE_KEY, r.stdout + r.stderr, '报错不得回显密钥本身')
        log = git(self.repo, 'log', '--oneline')
        self.assertEqual(log.stdout.strip(), '', '被拦的提交不该进历史')

    def test_block_reports_line_number(self):
        r = self.commit('多行.txt', CLEAN + CLEAN + f'key={FAKE_KEY}\n')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(':3', r.stdout + r.stderr)

    def test_staged_deletion_of_key_not_blocked(self):
        p = self.repo / '旧.txt'
        p.write_text(f'key={FAKE_KEY}\n', encoding='utf-8')
        git(self.repo, 'add', '旧.txt')
        r = git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                'commit', '--no-verify', '-m', 'seed')
        self.assertEqual(r.returncode, 0, r.stderr)
        p.write_text(CLEAN, encoding='utf-8')
        git(self.repo, 'add', '旧.txt')
        r = git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                'commit', '-m', 'scrub')
        self.assertEqual(r.returncode, 0,
                         '删除密钥的提交不该被 diff 的 - 行拦住：' + r.stdout + r.stderr)

    def test_missing_gate_fails_closed(self):
        (self.repo / 'scripts/privacy_gate.py').unlink()
        r = self.commit('干净.txt', CLEAN)
        self.assertNotEqual(r.returncode, 0, '扫描器缺失必须拦，不能静默放行')
        self.assertIn('失败即拦', r.stdout + r.stderr)

    def test_reinstall_idempotent_no_backup(self):
        before = self.hook.read_bytes()
        ih.install(self.repo)
        self.assertEqual(self.hook.read_bytes(), before)
        self.assertEqual(list(self.repo.glob('.git/hooks/pre-commit.bak-*')), [])

    def test_foreign_hook_backed_up_not_destroyed(self):
        self.hook.write_text('#!/bin/sh\necho 别人的钩子\n', encoding='utf-8')
        ih.install(self.repo)
        baks = list((self.repo / '.git/hooks').glob('pre-commit.bak-*'))
        self.assertEqual(len(baks), 1)
        self.assertIn('别人的钩子', baks[0].read_text(encoding='utf-8'))
        self.assertIn(ih.MARKER, self.hook.read_text(encoding='utf-8'))

    def test_uninstall_only_removes_ours(self):
        self.hook.write_text('#!/bin/sh\necho 别人的钩子\n', encoding='utf-8')
        ih.uninstall(self.repo)
        self.assertTrue(self.hook.exists(), '不是本工具写的钩子不能删')
        ih.install(self.repo)
        ih.uninstall(self.repo)
        self.assertFalse(self.hook.exists())

    def test_check_reflects_state(self):
        self.assertEqual(ih.check(self.repo), 0)
        self.hook.unlink()
        self.assertEqual(ih.check(self.repo), 1)

    def test_check_rejects_crlf(self):
        self.hook.write_bytes(self.hook.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(ih.check(self.repo), 1)

    def test_hook_works_with_chinese_repo_path(self):
        sub = self.repo / '中文目录 空格'
        sub.mkdir()
        r = self.commit('中文目录 空格/文件.txt', CLEAN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(sub.is_dir())


class RealRepoTest(unittest.TestCase):
    def test_gate_and_installer_present(self):
        self.assertTrue((BASE / 'scripts/privacy_gate.py').exists())
        self.assertTrue((BASE / 'scripts/install_hooks.py').exists())

    def test_installer_source_has_no_plaintext_secrets(self):
        sys.path.insert(0, str(BASE / 'scripts'))
        import privacy_gate as pg
        for name in ('scripts/install_hooks.py', 'install_hooks_test.py'):
            hits = pg.scan_text((BASE / name).read_text(encoding='utf-8'))
            self.assertEqual(hits, [], f'{name} 自身命中隐私门: {hits}')


if __name__ == '__main__':
    unittest.main(verbosity=2)