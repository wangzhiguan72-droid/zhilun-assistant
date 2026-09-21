"""智论助手 · 敏感信息守门扫描（v2.29）

为什么要有这个文件：
    仓库要开源。真实 API Key 一旦进 Git 历史，**删掉文件也没用**（历史可回溯）。
    本脚本在 CI / 提交前对**所有已跟踪文件**做一遍密钥形态扫描：
    - sk- 前缀密钥（硅基流动 / DeepSeek / Kimi / DashScope 等，16 位以上）
    - 智谱 GLM 形态密钥（32 位 hex + "." + 长段）
    - GitHub PAT（ghp_ / github_pat_）
    - .env 本体被误跟踪（应只有 .env.example 入库）

已知误报源（测试里的合成假 Key）走显式白名单，不靠"看起来假"猜：
    compat_agent_test.py / secrets_guard_test.py / paid_brake_test.py 等
    里的 sk-abcdefghijklmnop、sk-user-own-key 是测试夹具，写进
    ALLOW_FIXTURES，形态照旧匹配但路径+内容双确认后放行。

用法：
    .venv/Scripts/python.exe scripts/secrets_scan.py        # 扫描已跟踪文件
    退出码：0 = 干净；1 = 发现疑似真实密钥或 .env 被跟踪。

注意：它只扫「已入库/将入库」的内容（git ls-files），不扫工作区里的
.env / uploads/ / .review_share/ 等运行时产物——那些本来就不该入库，
.gitignore 管它们；本脚本管的是"漏进 git 的那一步"。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 密钥形态（按真实平台格式写，不追求大而全，追求「零误报」）────────────
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # sk- 后接 16+ 位字母数字（无连字符）：硅基流动 / DeepSeek / Kimi / 百炼
    ("sk-* 形态密钥", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    # 智谱 GLM：32 位 hex + "." + 16+ 位（如 xxxx.yyyy）
    ("智谱 GLM 形态密钥", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9_-]{16,}\b")),
    # GitHub PAT：ghp_36 位 / github_pat_ 细粒度
    ("GitHub PAT", re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    # 阿里云 AK：LTAI 开头 24 位
    ("阿里云 AccessKey", re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b")),
]

# ── 已知测试夹具（路径 + 原文双确认，防有人把真 Key 改名塞进测试）─────────
# 形式：{文件相对路径: {允许出现的密钥字面量}}
ALLOW_FIXTURES: dict[str, set[str]] = {
    "compat_agent_test.py": {"sk-abcdefghijklmnop"},
    "secrets_guard_test.py": {"sk-abcdefghijklmnop"},
    "paid_brake_test.py": {"sk-user-own-key"},
    "kimi_mimo_test.py": {"sk-test-kimi", "sk-test-mimo"},
    "byok_test.py": set(),   # 该文件的 Key 均为短假名（sk-A / sk-B 形态），不会命中形态
}

# 形态判定补充：连字符分隔的假 Key（sk-user-own-key）根本不会被
# sk-[A-Za-z0-9]{16,} 命中（不含 - 字符），白名单只是双保险。


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        print("[FATAL] git ls-files 失败：", out.stderr.strip())
        sys.exit(2)
    return [p for p in out.stdout.splitlines() if p.strip()]


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except OSError:
        return True


def main() -> int:
    files = _tracked_files()
    findings: list[str] = []

    # 1) .env 本体绝不能被跟踪（.env.example / .env.sample 可以）
    for p in files:
        name = Path(p).name
        if name == ".env" or name.endswith(".env") and "example" not in name and "sample" not in name:
            if name == ".env":
                findings.append(f"[ENV] {p} —— .env 本体被 git 跟踪了！")

    # 2) 逐文件扫密钥形态
    scanned = 0
    for rel in files:
        p = ROOT / rel
        if not p.is_file() or _looks_binary(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        allowed = ALLOW_FIXTURES.get(rel, set())
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                token = m.group(0)
                if token in allowed:
                    continue
                # 掩码形态（sk-w***）不会命中形态；到这里的就是真疑似
                line_no = text[: m.start()].count("\n") + 1
                findings.append(
                    f"[{label}] {rel}:{line_no} —— 命中 {token[:6]}…{token[-4:]}"
                )

    print(f"已扫描 {scanned} 个已跟踪文本文件（共 {len(files)} 个跟踪条目）")
    if findings:
        print("\n⚠️ 发现疑似敏感信息：")
        for f_ in findings:
            print("  " + f_)
        print("\n处理建议：若是真 Key——立即去平台作废重置，并用 git filter-repo")
        print("清理历史；若是测试夹具——把字面量加进 scripts/secrets_scan.py 的")
        print("ALLOW_FIXTURES（路径+原文双确认），不要用注释标记绕过。")
        return 1
    print("✅ 未发现疑似真实密钥；.env 未被跟踪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
