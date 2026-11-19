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
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 向上找到含 dist/ 或 examples/ 的那一层，作为仓库根
ROOT = HERE if (HERE / "examples").exists() else HERE.parent
EXE = Path(os.environ.get("ZHILUN_E2E_EXE") or ROOT / "dist" / "智论助手.exe").resolve()
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

    # v2.50：启动前先看端口。PyInstaller onefile 的 bootstrapper 被 terminate
    # 后可能留下孤儿服务进程占着 5000——新 exe 连不上、或 /health 打到旧实例
    # 上撞旧限流计数，症状是无中生有的 429（2026-10-20 回归连踩两次，一次把
    # 整轮结果埋掉）。宁可明确失败，也不给假结果。
    import socket
    _s = socket.socket()
    try:
        _s.bind(("127.0.0.1", 5000))
    except OSError:
        print("  [FAIL] 127.0.0.1:5000 已被占用——先清理残留的智论助手服务进程：")
        print("         netstat -ano | findstr :5000  →  taskkill /PID <pid> /F")
        return 2
    finally:
        _s.close()

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
        # Version and new resources belong to this exact build, never an old R1 log.
        from runpy import run_path
        APP_VERSION = run_path(str(ROOT / 'version.py'))['APP_VERSION']
        st, health_body = http('/health')
        health = json.loads(health_body.decode('utf-8'))
        log("新构建版本一致", st == 200 and APP_VERSION in body.decode('utf-8'), APP_VERSION)
        homepage = body.decode('utf-8')
        log("AI 上下文与输出预算控件", all(item in homepage for item in (
            'id="auditContextMode"', 'id="auditOutputTokens"', 'id="auditResearchTools"',
            'value="full"', 'value="16384"')))
        for resource in ('task_guide.js', 'school_rules_ui.js', 'model_settings_ui.js', 'task_help.js', 'task_ui.css'):
            code, content = http('/static/' + resource)
            log('新前端资源 ' + resource, code == 200 and len(content) > 100)
        code, status_body = http('/api/model/status')
        status = json.loads(status_body.decode('utf-8'))
        log('实例模型状态端点', code == 200 and status.get('ok') is True and 'server' in status)
        raw_requirements, requirement_type = multipart({}, {'requirements': ('demo.txt', '开发自造规范：正文12pt。'.encode('utf-8'))})
        code, source_body = http('/api/school_rules/preview', 'POST', raw=raw_requirements, ctype=requirement_type)
        source = json.loads(source_body.decode('utf-8'))
        log('学校规范独立预览', code == 200 and source.get('ok') is True and len(source.get('sources', [])) == 1)

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

        # 5b) 流式分析（SSE）—— 打包后必须真能流出来。
        # 原先这个脚本完全没测流式，而流式恰恰是产品主路径：
        # 自设逐跳头那类缺陷只在真起服务器时才暴露（见 wsgi_test）。
        st, body = http("/api/analyze", "POST", {
            "file_id": fid, "method": "independent_t",
            "group_col": "group", "value_col": "前测", "stream": 1,
        })
        txt = body.decode("utf-8", "replace") if st == 200 else ""
        ev = [l[7:].strip() for l in txt.split("\n") if l.startswith("event: ")]
        log("POST /api/analyze stream=1 流式分析",
            st == 200 and "done" in ev and "markdown" in ev,
            "status=%s events=%s" % (st, ev[:6]))

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
        mp, ct = multipart({"use_llm": "0", "ai_research_tools": "1"}, {
            "paper": ("paper.md", paper_md),
            "data": ("rm_anova_data.csv", csv),
        })
        st, body = http("/api/check_paper", "POST", raw=mp, ctype=ct)
        cp = json.loads(body.decode("utf-8")) if st == 200 else {}
        log("POST /api/check_paper 论文核查（含表格交叉核查）",
            st == 200 and cp.get("ok") is True and not cp.get("llm_enhanced")
            and cp.get("llm_research") is None,
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

        # 11) v2.61 风格提示护栏：新模块必须真被打进包（函数内 import，漏 datas 只在运行时炸）
        #     夹具同时覆盖「豁免」与「保留」两条路径：封面段被本地门豁免，讨论段留下。
        #     夹具全部合成脱敏（姓名/校名/题名/正文均非真实论文），只保留门依赖的
        #     字段族、分行排版与讨论段的套话密度。
        cover = (
            "2025 届 硕 士 学 位 论 文\n合成视角下样例群体示例观念状况与改进路径考察\n"
            "作者姓名 测试甲\n指导教师 测试乙 教授\n学科专业 示例学\n研究方向 示例方向\n"
            "培养单位 示例学院\n学习年限 2021年9月至2024年6 月\n二〇二五年六月\n示例大学\n"
            "综上所述，本文进一步构建了模型。此外，与此同时，值得注意的是，可以看出模型效果很好。"
            "该模型显著提高了预测精度。结果表明本方法广泛应用于各个领域。由此可见，本方法具有重要价值。")
        discussion = (
            "本研究围绕示例平台的使用意愿展开讨论。结果显示，感知有用性对使用意愿具有正向影响，"
            "感知易用性的作用则相对有限。这一发现与既有技术接受模型的结论基本一致，但在本样本中，"
            "主观规范的调节作用并未达到显著水平，可能与样本集中于高年级学生有关。需要指出的是，"
            "本研究采用横断面设计，无法据此推断因果关系，后续研究可考虑追踪设计加以检验。\n"
            "综上所述，该模型显著提高了预测精度。此外，结果显著。与此同时，方法有效。由此可见，方案可行。")
        st, body = http("/api/ai_audit", "POST", {"text": cover + "\n\n" + discussion})
        audit = json.loads(body.decode("utf-8"))
        exempted = audit.get("passages_exempted") or []
        log("POST /api/ai_audit 本地豁免门已打包生效",
            st == 200 and audit.get("ok") is True and audit.get("gate_version") == "v3"
            and len(exempted) >= 1 and bool(exempted[0].get("gate", {}).get("reason"))
            and len(audit.get("passages") or []) >= 1,
            "status=%s gate=%s 保留=%d 豁免=%d" % (
                st, audit.get("gate_version"), len(audit.get("passages") or []), len(exempted)))

        # 关掉 AI 必须零调用，且不得谎称「模型输出无法解析」
        st, body = http("/api/style_triage", "POST",
                        {"text": cover + "\n\n" + discussion, "llm": False})
        tri = json.loads(body.decode("utf-8"))
        items = tri.get("items") or []
        log("POST /api/style_triage llm=false 零调用不丢条",
            st == 200 and tri.get("ok") is True and tri.get("model_calls") == 0
            and len(items) == len(audit.get("passages") or [])
            and all(i.get("verdict") in ("真", "假", "存疑") for i in items)
            and all(i.get("source") != "unparseable" for i in items)
            and "不判定" in (tri.get("disclaimer") or ""),
            "status=%s calls=%s items=%d sources=%s" % (
                st, tri.get("model_calls"), len(items), [i.get("source") for i in items]))

        st, _ = http("/api/style_triage", "POST", {"text": "", "llm": False})
        log("POST /api/style_triage 空文本 → 400", st == 400, "status=%s" % st)
        log("首页含护栏复核控件",
            'id="styleTriageBtn"' in homepage and 'id="styleTriageResult"' in homepage)

        # 12) 门禁默认关闭（未设 ACCESS_CODE 时应零干扰）
        st, body = http("/logout")
        log("GET /logout 门禁关闭时正常", st in (200, 302), "status=%s" % st)

    finally:
        try:
            if proc.poll() is None:
                # Windows onefile has a bootloader parent and a serving child.
                # Stop only the tree launched by this test, before the parent
                # exits and its child becomes an untraceable port-5000 orphan.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=15)
                else:
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
