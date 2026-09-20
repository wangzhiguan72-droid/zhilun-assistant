"""智论助手 · 全量回归 runner（单元测试 + 打包链路验证）

为什么要有这个文件：
    改完代码要跑二十多个测试套件，手工逐个跑既慢又容易漏。
    更重要的是 —— **单元测试全绿 ≠ exe 能跑**：
    exe 缺一个模块是"构建期"问题，任何单测都发现不了（2026-09-11 那次
    93.9MB 的包双击闪退，单测当时是全绿的）。所以把打包链路验证也收进来。

用法：
    .venv/Scripts/python.exe scripts/regress_run.py            # 全量（单元测试 + exe 验证）
    .venv/Scripts/python.exe scripts/regress_run.py --fast     # 只跑单元测试（跳过 exe 验证）
    .venv/Scripts/python.exe scripts/regress_run.py --only exe # 只跑 exe 验证

退出码：
    0 = 全部通过
    1 = 有套件失败
    2 = 环境未就绪（exe 不存在 / 缺依赖）—— 与项目既有约定一致

产出：`_tmp_reg.txt`（Human-readable 汇总）；有失败时另写 `_regress_<套件名>.log`。
"""
import argparse
import io
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 仓库根：本文件在 scripts/ 下，测试套件与 .venv 都在根目录
ROOT = HERE.parent if (HERE.parent / ".venv").exists() else HERE

# 绝对路径 —— 否则从别的目录调用会找不到 .venv / 测试文件
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
EXE_E2E = str(ROOT / "scripts" / "exe_e2e.py")
EXE_PATH = str(ROOT / "dist" / "智论助手.exe")
SUMMARY = str(ROOT / "_tmp_reg.txt")

# -----------------------------------------------------------------------------
# 单元测试套件：**自动发现**（v2.28）
#
# 为什么不用手工清单：CI 注释里早就写了——「自动发现，别维护手工清单，
# 手工清单一定会漂移」。v2.27 扫描报告实测了这句预言：stream_test 连打
# 11 次 /api/analyze 吃 429 崩溃，却因为不在手工清单里两轮回归都没人发现。
#
# 规则与 CI（.github/workflows/tests.yml）保持同一套：
#   1. 自动发现根目录全部 `*_test.py`；
#   2. 排除 LIVE_SUITES（真调外部 API / 真 Key，与 CI 同一清单）；
#   3. 排除 HTTP 外部服务型（需要先起 Flask 服务才能跑，单独跑必 rc=2，
#      与项目「环境未就绪」退出码约定一致，**不算失败**）。
# -----------------------------------------------------------------------------
TESTS = sorted(p.stem for p in ROOT.glob("*_test.py"))

# 真调外部 API / 需要真实 Key（与 CI 的 LIVE_SUITES 保持同一清单）
LIVE_SUITES = {"llm_cache_test", "prefix_cache_test",
               "zhipu_cache_test", "kimi_mimo_live_test"}

# HTTP 外部服务型：单独跑必 rc=2（需要 Flask 服务已启动），不计为失败
ENV_DEPENDENT = {"paper_check_test", "methods_test",
                 "regression_test", "smoke_test", "regression_audit_test"}

TESTS = [t for t in TESTS if t not in LIVE_SUITES]

# 打包链路验证脚本（慢：要真启动 exe，约 30–60 秒）—— 路径见上方 EXE_E2E / EXE_PATH


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def run_suite(name: str, timeout: int = 900, retry: bool = True):
    """跑一个测试套件，返回 (rc, summary, seconds, detail)。

    detail 是套件原始输出的尾部（失败时才有价值）—— 落盘到 `_regress_<name>.log`。
    没有它的话，汇总表只会告诉你"cli_test 有一项失败"，
    但你得手工再跑一遍（这个套件要 85 秒）才能看到失败详情。

    retry：失败且**通过数与总数不符**时自动重跑一次。
        「通过数 + 失败数 != 总数」说明有测试**根本没能跑起来**——
        CLI 依赖的 Flask 服务没起/被别的套件占用端口就算这种。
        这类是环境竞态（flaky），不是代码回归，重跑一次即可定论。
        真正挂在断言上的失败，通过数+失败数 == 总数，不会触发重跑（不掩盖真 bug）。
    """
    t0 = time.time()
    rc, summary, detail, complete = _run_once(name, timeout)
    retried = ""
    if retry and rc not in (0, 2) and not complete:
        rc, summary, detail, complete = _run_once(name, timeout)
        retried = "（重跑后仍不可完整执行）"
    return rc, summary + retried, time.time() - t0, detail


def _run_once(name: str, timeout: int):
    """跑一次套件，返回 (rc, summary, detail, complete)。

    complete=True 表示「通过数 + 失败数 == 总数」，即测试全部执行完毕；
    为 False 表示有用例没跑起来（典型：CLI 起不来 → 环境问题而非代码回归）。
    """
    try:
        r = subprocess.run([PY, name + ".py"],
                           cwd=str(ROOT), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT(%ds)" % timeout, "", False
    except Exception as e:  # noqa: BLE001
        return -2, "ERROR %s" % e, "", False

    txt = _decode(r.stdout + r.stderr)
    m = re.findall(r"(\d+)\s*通过[^\d]{0,12}(\d+)\s*失败", txt)
    if m:
        passed, failed = int(m[-1][0]), int(m[-1][1])
        summary = "%s 通过 / %s 失败" % (passed, failed)
        # 大多数套件会打印「合计 N 项」，据此判断用例是否跑全
        tot = re.findall(r"合计\s*(\d+)", txt)
        if tot and (passed + failed) != int(tot[-1]):
            summary += "  ⚠ 应有 %s 项，只跑了 %d 项（多半是服务未就绪）" % (
                tot[-1], passed + failed)
            return r.returncode, summary, txt, False
        return r.returncode, summary, txt, True
    if r.returncode == 2 and name in ENV_DEPENDENT:
        # rc=2 是既定的「环境未就绪」码，不是失败 —— 别写成 NONZERO 吓人
        return r.returncode, "环境未就绪（需先启服务）", txt, False
    return (r.returncode,
            "OK" if r.returncode == 0 else "NONZERO(rc=%d)" % r.returncode,
            txt, False)


def run_exe_e2e(timeout: int = 600):
    """跑打包链路验证（真启动 exe 走 12 步）。"""
    if not Path(EXE_PATH).exists():
        return 2, "SKIP：未找到 %s（先跑 build_desktop.py）" % EXE_PATH, 0.0, ""
    t0 = time.time()
    try:
        r = subprocess.run([PY, EXE_E2E],
                           cwd=str(ROOT), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT(%ds)" % timeout, time.time() - t0, ""
    except Exception as e:  # noqa: BLE001
        return -2, "ERROR %s" % e, time.time() - t0, ""

    txt = _decode(r.stdout + r.stderr)
    m = re.findall(r"exe 端到端：(\d+)\s*通过\s*/\s*(\d+)\s*失败", txt)
    if m:
        summary = "exe 端到端 %s 通过 / %s 失败" % (m[-1][0], m[-1][1])
    elif r.returncode == 2:
        summary = "环境未就绪（缺 exe 或依赖）"
    else:
        summary = "OK" if r.returncode == 0 else "NONZERO(rc=%d)" % r.returncode
    return r.returncode, summary, time.time() - t0, txt


def main():
    ap = argparse.ArgumentParser(description="智论助手全量回归")
    ap.add_argument("--fast", action="store_true", help="跳过 exe 链路验证（快）")
    ap.add_argument("--only", choices=["unit", "exe"], help="只跑其中一类")
    args = ap.parse_args()

    do_unit = args.only != "exe"
    do_exe = (not args.fast) and (args.only != "unit")

    out = []
    failed = []
    env_not_ready = []
    logs = {}          # name -> 原始输出（失败时落盘，方便直接看失败详情）
    t_start = time.time()

    if do_unit:
        out.append("=" * 74)
        out.append("单元测试（%d 套件）" % len(TESTS))
        out.append("=" * 74)
        for t in TESTS:
            rc, summary, secs, txt = run_suite(t)
            mark = "  "
            if rc == 0:
                pass
            elif rc == 2 and t in ENV_DEPENDENT:
                mark = "~ "      # 环境依赖，不算失败
                env_not_ready.append(t)
            else:
                mark = "!! "
                failed.append(t)
            if rc not in (0, 2):
                logs[t] = txt
            out.append("%s%-26s rc=%-4d %-26s %5.1fs" % (mark, t, rc, summary, secs))
            print("%s%-26s rc=%-4d %s" % (mark, t, rc, summary))
        if env_not_ready:
            out.append("")
            out.append("(~ = 环境未就绪 rc=2，非失败：%s)" % ", ".join(env_not_ready))

    if do_exe:
        out.append("")
        out.append("=" * 74)
        out.append("打包链路验证（真启动 exe 走 12 步）")
        out.append("=" * 74)
        rc, summary, secs, txt = run_exe_e2e()
        mark = "  " if rc == 0 else ("~ " if rc == 2 else "!! ")
        if rc not in (0, 2):
            failed.append(EXE_E2E)
            logs[EXE_E2E] = txt
        elif rc == 2:
            env_not_ready.append(EXE_E2E)
        out.append("%s%-26s rc=%-4d %-26s %5.1fs" % (mark, EXE_E2E, rc, summary, secs))
        print("%s%-26s rc=%-4d %s" % (mark, EXE_E2E, rc, summary))

    elapsed = time.time() - t_start
    out.append("")
    out.append("=" * 74)
    if failed:
        out.append("结果：%d 套件失败 → %s" % (len(failed), ", ".join(failed)))
    elif env_not_ready:
        out.append("结果：全部通过（%d 项为环境未就绪，已忽略）· 耗时 %.0fs"
                   % (len(env_not_ready), elapsed))
    else:
        out.append("结果：全部通过 · 耗时 %.0fs" % elapsed)
    out.append("=" * 74)

    io.open(SUMMARY, "w", encoding="utf-8").write("\n".join(out))

    # 失败套件的原始输出单独存一份 —— 汇总表只给结论，详情在这里
    if failed:
        detail_lines = ["", "失败套件原始输出已写入："]
        for f in failed:
            log_name = str(ROOT / ("_regress_%s.log" % f))
            io.open(log_name, "w", encoding="utf-8").write(logs.get(f, ""))
            detail_lines.append("    %s" % log_name)
        io.open(SUMMARY, "a", encoding="utf-8").write("\n".join(detail_lines))
        print("\n".join(detail_lines))

    print("\n" + "\n".join(out[-3:]))
    print("(明细见 %s)" % SUMMARY)

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
