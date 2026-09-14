# -*- coding: utf-8 -*-
"""对打包好的 exe 做端到端功能验证（不依赖 .venv，只用标准库）。

流程：
    启动 exe → 等 /health → 上传示例数据 → 数据体检 → 跑一次 T 检验
    → 论文核查（含表格交叉核查）→ 生成清洗副本 → 答辩准备包 → 关闭

任何一步非 200 或返回 ok=false 都算失败。用法：
    python scripts/exe_e2e.py

⚠️ 路径基准是**仓库根**而不是本文件所在目录：
    本脚本在 `scripts/` 下，但 `dist/`、`examples/` 都在仓库根。
    早期版本用 `Path(__file__).parent` 找 `dist/智论助手.exe`，
    文件从根目录挪进 `scripts/` 之后就会永远找不到 exe（静默 SKIP，看起来"通过"）。
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 向上找到含 dist/ 或 examples/ 的那一层，作为仓库根
ROOT = HERE if (HERE / "examples").exists() else HERE.parent
EXE = ROOT / "dist" / "智论助手.exe"
BASE = "http://127.0.0.1:5000"

# =============================================================================
# 目录定位自检 —— 防止脚本被移动后"静默 SKIP 却报通过"
# =============================================================================
if not (ROOT / "examples").exists():
    print("  [FAIL] 找不到 examples/（仓库根判定错误）ROOT=%s" % ROOT)
    sys.exit(2)
if not EXE.exists():
    print("  [FAIL] 未找到 %s —— 先跑 `python build_desktop.py`" % EXE)
    sys.exit(2)

results = []


def log(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


def http(path, method="GET", payload=None, raw=None, ctype="application/json"):
    url = BASE + path
    data = None
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def multipart(fields, files):
    """手工拼 multipart/form-data（标准库没有现成的）。"""
    boundary = "----zhilunE2E" + str(int(time.time() * 1000))
    lines = []
    for k, v in fields.items():
        lines.append(("--" + boundary).encode())
        lines.append(('Content-Disposition: form-data; name="%s"' % k).encode())
        lines.append(b"")
        lines.append(str(v).encode("utf-8"))
    for k, (fname, content) in files.items():
        lines.append(("--" + boundary).encode())
        lines.append(
            ('Content-Disposition: form-data; name="%s"; filename="%s"' % (k, fname)).encode("utf-8"))
        lines.append(b"Content-Type: application/octet-stream")
        lines.append(b"")
        lines.append(content)
    lines.append(("--" + boundary + "--").encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, "multipart/form-data; boundary=" + boundary


def wait_ready(proc, seconds=45):
    for _ in range(int(seconds * 2)):
        time.sleep(0.5)
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
    return False


def main():
    if not EXE.exists():
        print("[SKIP] 未找到 exe：" + str(EXE))
        return 2

    csv = (ROOT / "examples" / "rm_anova_data.csv").read_bytes()
    print("启动 exe ...")
    proc = subprocess.Popen([str(EXE)], cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_ready(proc):
            log("exe 启动并 /health 就绪", False, "超时或进程已退出")
            return 1
        log("exe 启动并 /health 就绪", True)

        # 1) 首页
        st, body = http("/")
        log("GET / 返回首页", st == 200 and len(body) > 50000, "status=%s len=%d" % (st, len(body)))

        # 2) 上传
        mp, ct = multipart({}, {"file": ("rm_anova_data.csv", csv)})
        st, body = http("/api/upload", "POST", raw=mp, ctype=ct)
        up = json.loads(body.decode("utf-8")) if st == 200 else {}
        fid = up.get("file_id")
        log("POST /api/upload 上传成功", st == 200 and bool(fid), "status=%s file_id=%s" % (st, fid))
        if not fid:
            return 1

        # 3) 数据体检（datacheck 模块）
        st, body = http("/api/datacheck", "POST", {"file_id": fid})
        dc = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/datacheck 数据体检", st == 200 and dc.get("ok") is True,
            "status=%s issues=%s" % (st, len(dc.get("report", {}).get("issues", []) or [])
                                     if isinstance(dc.get("report"), dict) else "n/a"))

        # 4) 清洗副本（propose_fix）
        st, body = http("/api/datacheck/fix", "POST", {"file_id": fid})
        fx = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/datacheck/fix 清洗副本", st == 200 and fx.get("ok") is True, "status=%s" % st)

        # 5) 统计计算（methods_registry + app 内 run_*）
        st, body = http("/api/analyze", "POST", {
            "file_id": fid, "method": "independent_t",
            "group_col": "group", "value_col": "前测",
        })
        an = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/analyze 独立样本 T 检验",
            st == 200 and an.get("ok") is True,
            "status=%s keys=%s" % (st, list(an.keys())[:5]))

        # 6) 方法知识图谱（methods_graph）
        st, body = http("/api/methods_graph")
        mg = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("GET /api/methods_graph 知识图谱", st == 200, "status=%s" % st)

        # 7) 论文核查（audit + table_check + grimmer 全链路）
        paper_md = (
            "# 测试论文\n\n"
            "本研究共回收有效问卷 30 份（n=30）。实验组均值 3.47，标准差 0.52。\n\n"
            "| 变量 | 均值 | 标准差 |\n"
            "| --- | --- | --- |\n"
            "| 前测 | 60.90 | 12.34 |\n\n"
            "差异检验显示 p = 0.03，达到显著水平。\n"
        ).encode("utf-8")
        mp, ct = multipart({}, {
            "paper": ("paper.md", paper_md),
            "data": ("rm_anova_data.csv", csv),
        })
        st, body = http("/api/check_paper", "POST", raw=mp, ctype=ct)
        cp = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/check_paper 论文核查（含表格交叉核查）",
            st == 200 and cp.get("ok") is True,
            "status=%s keys=%s" % (st, list(cp.keys())[:6]))

        # 8) 流水线阶段（pipeline）
        st, body = http("/api/copilot/phases")
        if st != 200:
            st, body = http("/api/copilot/phases", "POST", {})
        log("copilot/phases 流水线阶段", st == 200, "status=%s" % st)

        # 9) 答辩准备包（defense_pack）
        st, body = http("/api/defense_pack", "POST", {
            "file_id": fid,
            "runs": [{"method": "independent_t", "group_col": "group", "value_col": "前测"}],
        })
        dp = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/defense_pack 答辩准备包",
            st == 200 and dp.get("ok") is True,
            "status=%s keys=%s" % (st, list(dp.keys())[:5]))

        # 10) 示例文件静态服务
        st, body = http("/api/sample/rm_anova_data.csv")
        log("GET /api/sample/* 示例数据", st == 200 and len(body) > 100, "status=%s" % st)

        # 11) 门禁默认关闭（未设 ACCESS_CODE 时应零干扰）
        st, body = http("/logout")
        log("GET /logout 门禁关闭时正常", st in (200, 302), "status=%s" % st)

    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 52)
    print("exe 端到端：%d 通过 / %d 失败" % (passed, total - passed))
    print("=" * 52)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
