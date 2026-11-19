# -*- coding: utf-8 -*-
"""提交前隐私与密钥拦截门。

为什么需要它：v2.61 那一轮做隐私脱敏时，同一个会话**两次**把正在删除的个人信息
片段又写回文档（一次引用真实学号，一次在审计表里引用原文）。人眼审查已经被证明
不可靠，所以改成机器拦截。

为什么只存哈希：禁止串本身如果写进这个文件，就等于把它们重新提交进仓库——
正是本门要防的事。所以这里只存 (字符长度, sha256)，扫描时对文本滑窗取哈希比对。
代价是短串（3 字姓名、5 位代码）对知道候选空间的离线暴力破解抵抗力有限；
本门的目标是**防手滑再次引入**，不是防有意攻击者。

为什么扫 staged blob 而不是 git diff：diff 里的 `-` 删除行也含禁止串，
扫 diff 会把「正在删掉它」误报成「正在引入它」。必须扫 `git show :<path>` 的内容。

为什么路径用 -z：`git status/diff --name-only` 默认把非 ASCII 路径做八进制转义，
中文文件名会变成 "\\346\\226\\207..."，按字面路径去 show 会全部失败 → 静默漏扫。

用法：
    python scripts/privacy_gate.py            # 扫 staged（pre-commit 用）
    python scripts/privacy_gate.py --all      # 扫全部已跟踪文件（审计用）
    python scripts/privacy_gate.py --selftest # 自检：确认门真的能抓到
退出码：0 干净 / 1 有命中 / 2 自身出错（git 不可用等）。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

# (字符长度, sha256, 类别, 形态)。类别注释不含明文值。
# 形态决定用哪种廉价定位策略；**未知形态一律退回全文滑窗**，
# 绝不因为「不认识这个类别」就静默跳过——那正是本门要防的失败模式。
FORBIDDEN = (
    (10, "c56e56743fb8eb5a93ed95f6167c7ec17b4a1835f0492870e48a99175f436745", "真实学号", "digit"),
    (5, "d9adc0ef69e941f005a7a6069f92fd70c13b328a61eb6722405a53451093b84c", "真实单位代码", "digit"),
    (3, "21aaa3ec8d097758a8ef6f28a0d08ca6eed7e9bf47d9f2a19ea1f9610bae1758", "真实姓名", "cjk"),
    (3, "8551a69b25aa82b220bd678e87519adbf42c89473fc4649d43481346cdb0dd04", "致谢真实姓氏+称谓", "cjk"),
    (24, "3e94885805b79a277ea50c9a7222d87f56b9028733f4ecee2f075f044273f586", "旧作者真实邮箱", "email"),
)

# 通用密钥形态：不内嵌任何真实 key。
# 真 key 的特征是长段不间断字母数字（小米/硅基/DeepSeek 皆然，Anthropic 的
# sk-ant-api03-<长串> 末段同理）；测试夹具里的假 key 是词典词加连字符
# （sk-user-supplied-key-12345 / sk-SECRET-TOKEN-1234567890），最长段很短。
# 用「最长不间断段 >= 20」区分，比维护占位词表更稳，也不会放过长串真 key。
KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")
ALNUM_RUN = re.compile(r"[A-Za-z0-9]{20,}")
# Kimi 的 429 正文会回显账号标识（org-<32位hex> 与 ak-<id>）。它们不是 Key，
# 但同样能定位到具体账号，而报错文案会进前端、日志与汇总 JSON。
# 测试夹具用全 a / 全 b 的合成串：单一字符重复的不是真标识，靠「不同字符数」放行。
ACCOUNT_ID_RE = re.compile(r"\b(?:org-[0-9a-f]{8,}|ak-[0-9a-z]{8,})")
ACCOUNT_ID_MIN_DISTINCT = 4
# 数字串与邮箱先做廉价定位，再逐个哈希，避免全文滑窗
DIGIT_RUN = re.compile(r"\d+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

BINARY_EXT = {".exe", ".dll", ".pyd", ".png", ".jpg", ".jpeg", ".gif", ".ico",
              ".pdf", ".zip", ".7z", ".rar", ".woff", ".woff2", ".ttf", ".otf",
              ".mp4", ".mp3", ".wav", ".docx", ".xlsx", ".pptx", ".so", ".bin"}
MAX_SCAN_BYTES = 4 * 1024 * 1024


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _windows(run: str, n: int):
    for i in range(len(run) - n + 1):
        yield run[i:i + n]


def _hits_in_runs(text, n, h, run_re):
    """在廉价定位出的片段内滑窗，返回精确字符偏移。"""
    out = []
    for m in run_re.finditer(text):
        run = m.group(0)
        for i, w in enumerate(_windows(run, n)):
            if sha(w) == h:
                out.append(m.start() + i)
                break
    return out


def scan_text(text: str) -> list[tuple[str, int]]:
    """返回 [(类别, 字符偏移)]。绝不返回命中串本身。"""
    found = []
    for n, h, cat, kind in FORBIDDEN:
        if kind == "email":
            offs = [m.start() for m in EMAIL_RE.finditer(text)
                    if len(m.group(0)) == n and sha(m.group(0)) == h]
        elif kind == "digit":
            offs = _hits_in_runs(text, n, h, DIGIT_RUN)
        elif kind == "cjk":
            offs = _hits_in_runs(text, n, h, CJK_RUN)
        else:
            # 未知/混合形态：全文滑窗。慢一点，但绝不漏。
            offs = [i for i, w in enumerate(_windows(text, n)) if sha(w) == h]
        found.extend((cat, o) for o in offs)
    for m in KEY_RE.finditer(text):
        if ALNUM_RUN.search(m.group(0)[3:]):
            found.append(("疑似 API Key（sk- 形态）", m.start()))
    for m in ACCOUNT_ID_RE.finditer(text):
        if len(set(m.group(0).split("-", 1)[1])) >= ACCOUNT_ID_MIN_DISTINCT:
            found.append(("疑似平台账号标识（org-/ak- 形态）", m.start()))
    return found


def line_of(text: str, off: int) -> int:
    return text.count("\n", 0, off) + 1


def git(*args) -> bytes:
    r = subprocess.run(["git"] + list(args), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:200])
    return r.stdout


def staged_paths() -> list[str]:
    raw = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    return [p for p in raw.decode("utf-8").split("\0") if p]


def tracked_paths() -> list[str]:
    raw = git("ls-files", "-z")
    return [p for p in raw.decode("utf-8").split("\0") if p]


def blob_bytes(path: str, staged: bool) -> bytes | None:
    try:
        return git("show", f":{path}" if staged else f"HEAD:{path}")
    except RuntimeError:
        return None


def decode(data: bytes) -> str | None:
    if b"\x00" in data[:8000]:
        return None
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def check(paths: list[str], staged: bool) -> int:
    hits, skipped, scanned = [], [], 0
    for p in paths:
        if Path(p).suffix.lower() in BINARY_EXT:
            skipped.append(p)
            continue
        data = blob_bytes(p, staged)
        if data is None:
            skipped.append(p)
            continue
        if len(data) > MAX_SCAN_BYTES:
            skipped.append(p + "（超过 4MB 未扫）")
            continue
        text = decode(data)
        if text is None:
            skipped.append(p + "（二进制/无法解码）")
            continue
        scanned += 1
        for cat, off in scan_text(text):
            hits.append((p, line_of(text, off), cat))
    print(f"隐私门：扫描 {scanned} 个文件，跳过 {len(skipped)} 个", flush=True)
    if skipped:
        for s in skipped[:10]:
            print(f"  跳过 {s}", flush=True)
    if hits:
        print(f"\n[FAIL] 命中 {len(hits)} 处，禁止提交：", flush=True)
        for p, ln, cat in hits:
            # 只报位置与类别，绝不回显命中串
            print(f"  {p}:{ln}  ← {cat}", flush=True)
        print("\n处理：改成合成占位值后重新 git add。"
              "若确属误报，用 --explain 查看类别说明，不要直接绕过本门。", flush=True)
        return 1
    print("[PASS] 未发现禁止串或密钥形态", flush=True)
    return 0


def selftest() -> int:
    """轻量自检：占位值不误报、密钥形态可识别、干净文本零命中。

    真实禁止串的命中能力由 privacy_gate_test.py 覆盖（测试侧临时构造样本，
    不把明文写进版本化文件）。
    """
    benign = "本研究采用示例数据，单位代码 10000，学号 2022000000。\n示例老师指导。\n"
    if scan_text(benign):
        print("[FAIL] 自检：脱敏后的占位文本被误报", flush=True)
        return 1
    if not scan_text("sk-" + "a" * 40):
        print("[FAIL] 自检：sk- 形态密钥未被识别", flush=True)
        return 1
    if scan_text("") or scan_text("普通中文段落，没有任何标识。"):
        print("[FAIL] 自检：干净文本被误报", flush=True)
        return 1
    print("[PASS] 自检：占位值不误报、密钥形态可识别、干净文本零命中", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="扫全部已跟踪文件而非 staged")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    try:
        paths = tracked_paths() if args.all else staged_paths()
    except RuntimeError as e:
        print(f"[FAIL] 隐私门自身出错：{e}", flush=True)
        return 2
    if not paths:
        print("隐私门：无待扫文件", flush=True)
        return 0
    return check(paths, staged=not args.all)


if __name__ == "__main__":
    sys.exit(main())