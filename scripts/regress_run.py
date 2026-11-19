"""智论助手 · 全量回归 runner（单元测试 + 打包链路验证）

为什么要有这个文件：
    改完代码要跑二十多个测试套件，手工逐个跑既慢又容易漏。
    更重要的是 —— **单元测试全绿 ≠ exe 能跑**：
    exe 缺一个模块是"构建期"问题，任何单测都发现不了（2026-09-11 那次
    93.9MB 的包双击闪退，单测当时是全绿的）。所以把打包链路验证也收进来。

v2.62 起的两条硬规矩：
    1. **自己起服务**：9 个套件走真实 HTTP。以前没服务时它们 rc=2 或静默 SKIP，
       runner 记成「环境未就绪，已忽略」，最后照样打印「全部通过」——
       测试没跑 ≠ 测试通过。现在 runner 起一个干净的服务实例（无凭据、限流关），
       这些套件必须真的执行；起不来就明确报「未执行」并返回 2，绝不报绿。
    2. **声明式排除**：需要真 Key / --live 的段落默认不在范围内，但必须**写出来**，
       而不是混在「通过」里。加 --live 才把它们纳入范围，届时缺 Key 同样返回 2。

用法：
    .venv/Scripts/python.exe scripts/regress_run.py            # 全量（单元测试 + exe 验证）
    .venv/Scripts/python.exe scripts/regress_run.py --fast     # 只跑单元测试（跳过 exe 验证）
    .venv/Scripts/python.exe scripts/regress_run.py --only exe # 只跑 exe 验证
    .venv/Scripts/python.exe scripts/regress_run.py --live     # 把真 Key 段落也纳入范围
    .venv/Scripts/python.exe scripts/regress_run.py --no-server # 不起服务（那 9 个套件会记未执行）

退出码：
    0 = 范围内全部执行且通过
    1 = 有套件失败
    2 = 范围内有套件**没能执行**（服务起不来 / 缺 Key / 缺 exe）

产出：`_tmp_reg.txt`（Human-readable 汇总，**每跑完一个套件就增量写一次**，
      中途被掐断也看得到已完成部分）；有失败时另写 `_regress_<套件名>.log`。
"""
import argparse
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _find_root(start: Path) -> Path:
    """向上找到真正的仓库根：含 `*_test.py` 的那一级。

    为什么不用 `(HERE.parent / ".venv").exists()`：**从 git worktree 里跑会假绿**——
    worktree 没有 `.venv`，于是 ROOT 退化成 scripts/，`ROOT.glob("*_test.py")`
    一个都找不到，runner 却报「全部通过 · 0 套件」。测试没跑 ≠ 测试通过。
    """
    for d in (start, *start.parents):
        if any(d.glob("*_test.py")):
            return d
    return start.parent


ROOT = _find_root(HERE)

# 解释器：worktree 无 .venv 时用主仓库那份（路径由上面的根查找保证不越界）
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    _fallback = Path(sys.executable)
    if _fallback.exists():
        PY = str(_fallback)
EXE_E2E = str(ROOT / "scripts" / "exe_e2e.py")
EXE_PATH = str(Path(os.environ.get("ZHILUN_E2E_EXE") or ROOT / "dist" / "智论助手.exe").resolve())
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
#   3. HTTP 外部服务型**不再排除**：runner 自己起服务，它们必须真的跑。
# -----------------------------------------------------------------------------
TESTS = sorted(p.stem for p in ROOT.glob("*_test.py"))

# 真调外部 API / 需要真实 Key（与 CI 的 LIVE_SUITES 保持同一清单）
LIVE_SUITES = {"llm_cache_test", "prefix_cache_test",
               "zhipu_cache_test", "kimi_mimo_live_test"}

# 需要 Flask 服务才能跑的套件。runner 会起服务；起不来时这些记「未执行」而非「忽略」。
SERVER_SUITES = {"paper_check_test", "methods_test", "regression_test",
                 "smoke_test", "regression_audit_test", "ai_audit_test",
                 "defense_pack_test", "table_cross_test"}

# 套件内部有「需要真 Key / --live」的段落。默认声明排除（会写进汇总），
# --live 时纳入范围。既不是失败，也不许冒充通过。
KEY_GATED = {"llm_audit_test", "llm_enhance_test", "multimodal_test"}
LIVE_ARGS = {"multimodal_test": ["--live"]}
KEY_ENV = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY")

TESTS = [t for t in TESTS if t not in LIVE_SUITES]

# 打包链路验证脚本（慢：要真启动 exe，约 30–60 秒）—— 路径见上方 EXE_E2E / EXE_PATH


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _child_env() -> dict:
    """子进程环境：强制 UTF-8 输出。

    【为什么必须显式传 env】子进程的 stdout 是管道，Python 按系统 ANSI 代码页
    选编码——Windows 上是 GBK。而套件里到处在 print 中文和「⚠️」这类符号，
    写不进去就 UnicodeEncodeError 崩在 print 上，**测试一条没跑就退出**。
    更坏的是下面 _decode 一直按 UTF-8 读，等于写 GBK 读 UTF-8，本来就是错配。

    实测（v2.45）：`paper_check_test` / `smoke_test` / `regression_audit_test`
    三个套件在同一次 429 场景下全部 rc=1，崩点都在那一行 print("⚠️ …")。
    修在这里而不是逐个改套件：错配在启动器，一处修好四个套件。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    # 管道下 stdout 是块缓冲：套件跑 11 分钟日志还是 0 字节，看起来像卡死。
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    return env


def run_suite(name: str, timeout: int = 900, retry: bool = True, args=()):
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
    rc, summary, detail, complete = _run_once(name, timeout, args)
    retried = ""
    if retry and rc not in (0, 2) and not complete:
        rc, summary, detail, complete = _run_once(name, timeout, args)
        retried = "（重跑后仍不可完整执行）"
    return rc, summary + retried, time.time() - t0, detail


def _count_skips(txt: str) -> int:
    """数套件输出里的 `[SKIP]` 行。

    【为什么不能只靠「合计 N」】v2.46 实测：`table_cross_test` 有 3 项端到端
    用例包在 `if _d is not None:` 里，服务没起（或吃 429）时静默不执行、
    只打一行 `[SKIP]`，退出码照样 0。该套件**不打印「合计」**，于是
    `_run_once` 里那道 `tot and ...` 的校验直接短路，26 项反而被记成
    `complete=True`——测试没跑，汇总上却和 29 项全跑长得一模一样。

    这与 v2.43 修的「遗留报告骗过门控」是同一条纪律：**静默跳过必须浮到汇总**，
    而不是指望每个套件自觉去补「合计」。
    """
    return len(re.findall(r"^\s*\[SKIP\]", txt, re.M))


def _run_once(name: str, timeout: int, args=()):
    """跑一次套件，返回 (rc, summary, detail, complete)。

    complete=True 表示「通过数 + 失败数 == 总数」，即测试全部执行完毕；
    为 False 表示有用例没跑起来（典型：CLI 起不来 → 环境问题而非代码回归）。
    """
    try:
        r = subprocess.run([PY, "-u", name + ".py", *args],
                           cwd=str(ROOT), capture_output=True, timeout=timeout,
                           env=_child_env())
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT(%ds)" % timeout, "", False
    except Exception as e:  # noqa: BLE001
        return -2, "ERROR %s" % e, "", False

    txt = _decode(r.stdout + r.stderr)
    skips = _count_skips(txt)
    skip_note = "  ⚠ %d 项按环境 SKIP（未执行）" % skips if skips else ""
    m = re.findall(r"(\d+)\s*通过[^\d]{0,12}(\d+)\s*失败", txt)
    if m:
        passed, failed = int(m[-1][0]), int(m[-1][1])
        summary = "%s 通过 / %s 失败%s" % (passed, failed, skip_note)
        # 大多数套件会打印「合计 N 项」，据此判断用例是否跑全
        tot = re.findall(r"合计\s*(\d+)", txt)
        if tot and (passed + failed) != int(tot[-1]):
            summary += "  ⚠ 应有 %s 项，只跑了 %d 项（多半是服务未就绪）" % (
                tot[-1], passed + failed)
            return r.returncode, summary, txt, False
        # 没有「合计」的套件靠 SKIP 计数兜底：跳过 = 没跑，就不是 complete
        return r.returncode, summary, txt, not skips
    if r.returncode == 2:
        # rc=2 是既定的「环境未就绪」码。**不再当成「已忽略」**——
        # 服务由 runner 负责起，还 rc=2 就是范围内没跑成，必须浮到结果行。
        return r.returncode, "未执行（环境未就绪 rc=2）", txt, False
    return (r.returncode,
            ("OK" if r.returncode == 0 else "NONZERO(rc=%d)" % r.returncode) + skip_note,
            txt, False)


def run_exe_e2e(timeout: int = 600):
    """跑打包链路验证（真启动 exe 走 12 步）。"""
    if not Path(EXE_PATH).exists():
        return 2, "SKIP：未找到 %s（先跑 build_desktop.py）" % EXE_PATH, 0.0, ""
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-u", EXE_E2E],
                           cwd=str(ROOT), capture_output=True, timeout=timeout,
                           env=_child_env())
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
    # 父进程自己的汇总行也带 ⚠ —— Windows GBK 控制台编不出来直接崩（不是套件失败，
    # 是 runner 死在打印上，后面的套件一个都没跑）。子进程有 _child_env() 兜底，
    # 父进程这儿必须自己来。line_buffering 同样必须：重定向到文件时块缓冲会让
    # 日志 11 分钟停在 0 字节，看起来像卡死。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="智论助手全量回归")
    ap.add_argument("--fast", action="store_true", help="跳过 exe 链路验证（快）")
    ap.add_argument("--only", choices=["unit", "exe"], help="只跑其中一类")
    ap.add_argument("--live", action="store_true",
                    help="把需要真 Key / --live 的段落也纳入范围（缺 Key 则记未执行）")
    ap.add_argument("--no-server", action="store_true",
                    help="不起共享服务（依赖服务的套件会记未执行，不会报绿）")
    ap.add_argument("--port", type=int, default=5000, help="共享服务端口")
    args = ap.parse_args()

    do_unit = args.only != "exe"
    do_exe = (not args.fast) and (args.only != "unit")

    out = []
    failed = []
    not_run = []       # 范围内但没能执行 —— 绝不许冒充通过
    excluded = []      # 声明式排除（默认范围外的真 Key 段落）
    logs = {}          # name -> 原始输出（失败时落盘，方便直接看失败详情）
    t_start = time.time()

    def flush_summary():
        """每跑完一个套件就落盘：中途被掐断也看得到已完成部分。"""
        with io.open(SUMMARY, "w", encoding="utf-8") as f:
            f.write("\n".join(out))

    srv = None
    srv_error = ""
    if do_unit and not args.no_server:
        from test_server import TestServer
        srv = TestServer(port=args.port, root=ROOT)
        try:
            srv.start()
            out.append("共享服务：%s（无凭据、限流已关、日志 _test_server.log）" % srv.base)
        except RuntimeError as e:
            srv_error = str(e)
            srv = None
            out.append("共享服务起不来：%s" % srv_error)
        print(out[-1], flush=True)
        flush_summary()

    if do_unit:
        out.append("=" * 74)
        out.append("单元测试（%d 套件）" % len(TESTS))
        out.append("=" * 74)
        for t in TESTS:
            sargs = LIVE_ARGS.get(t, []) if args.live else []
            rc, summary, secs, txt = run_suite(t, args=sargs)
            has_skip = "[SKIP]" in txt
            if rc == 0 and has_skip and t in KEY_GATED and not args.live:
                mark = "- "      # 声明式排除：范围外，写出来但不算失败
                excluded.append(t)
            elif rc == 0 and not has_skip:
                mark = "  "
            elif rc == 2 or (rc == 0 and has_skip):
                mark = "~ "      # 范围内没跑成
                not_run.append(t)
            else:
                mark = "!! "
                failed.append(t)
            if rc not in (0, 2) or has_skip:
                logs[t] = txt
            line = "%s%-26s rc=%-4d %-30s %5.1fs" % (mark, t, rc, summary, secs)
            out.append(line)
            print(line, flush=True)
            flush_summary()
        if srv_error:
            out.append("")
            out.append("服务未就绪导致以下套件未执行：%s"
                       % ", ".join(sorted(SERVER_SUITES & set(TESTS))))
        if excluded:
            out.append("")
            out.append("(- = 声明排除，需真 Key/--live：%s；加 --live 可纳入范围)"
                       % ", ".join(excluded))
        if not_run:
            out.append("")
            out.append("(~ = 范围内未真正执行：%s)" % ", ".join(not_run))

    # exe 端到端自己占 5000，必须先让出端口
    if srv is not None:
        srv.stop()
        srv = None

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
            not_run.append(EXE_E2E)
        line = "%s%-26s rc=%-4d %-30s %5.1fs" % (mark, EXE_E2E, rc, summary, secs)
        out.append(line)
        print(line, flush=True)

    elapsed = time.time() - t_start
    out.append("")
    out.append("=" * 74)
    if failed:
        out.append("结果：%d 套件失败 → %s" % (len(failed), ", ".join(failed)))
    elif do_unit and not TESTS:
        # 一个套件都没发现 ≠ 全通过。宁可红，也不能给假绿。
        out.append("结果：未发现任何单元测试套件（ROOT=%s）—— 检查根目录查找是否跑偏" % ROOT)
        failed.append("<no-suites>")
    elif not_run:
        out.append("结果：%d 项未真正执行（不是通过）→ %s"
                   % (len(not_run), ", ".join(not_run)))
        out.append("      已执行的套件无失败 · 耗时 %.0fs" % elapsed)
    else:
        out.append("结果：范围内全部执行且通过（%d 套件）· 耗时 %.0fs"
                   % (len(TESTS) + (1 if do_exe else 0), elapsed))
    if excluded:
        out.append("范围外（已声明，未执行）：%s —— 需真 Key 或 --live"
                   % ", ".join(excluded))
    out.append("=" * 74)

    flush_summary()

    # 失败套件的原始输出单独存一份 —— 汇总表只给结论，详情在这里
    if failed or not_run:
        detail_lines = ["", "失败/未执行套件原始输出已写入："]
        for f in failed + not_run:
            # v2.49：套件名可能是绝对路径（exe_e2e），直接拼日志名在
            # Windows 上非法（`_regress_D:\…`）→ OSError 把全部结果埋掉。
            safe = f.replace(":", "").replace("\\", "_").replace("/", "_")
            log_name = str(ROOT / ("_regress_%s.log" % safe))
            with io.open(log_name, "w", encoding="utf-8") as fh:
                fh.write(logs.get(f, ""))
            detail_lines.append("    %s" % log_name)
        with io.open(SUMMARY, "a", encoding="utf-8") as fh:
            fh.write("\n".join(detail_lines))
        print("\n".join(detail_lines), flush=True)

    print("\n" + "\n".join(out[-4:]), flush=True)
    print("(明细见 %s)" % SUMMARY, flush=True)

    if failed:
        return 1
    if not_run:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
