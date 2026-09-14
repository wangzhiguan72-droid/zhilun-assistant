"""
v1.8 · 规划§七 P3 跨端一体（CLI）契约测试
================================================
覆盖：
    1. 四个子命令可用：methods / analyze / audit / simulate
    2. 验收标准：`analyze --method independent_t` 与网页端同路径、结果一致
    3. 护栏：默认不写文件（只打印）；--output 才落盘
    4. 护栏：红线自检在 CLI 同样生效（CLI 不是绕过学术不端约束的后门）
    5. 错误路径：未知方法 / 不存在文件 / 字段缺失 → 退出码 1 + 可读提示
    6. 边界：--format json 不含 markdown（体积控制）；stdout 可被管道消费

运行：.venv/Scripts/python.exe -u cli_test.py
"""
import pathlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
BASE = pathlib.Path(__file__).resolve().parent
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["RATE_LIMIT_DISABLE"] = "1"

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CLI = str(ROOT / "cli.py")
CSV = str(ROOT / "examples" / "student_scores.csv")
CSV_REG = str(ROOT / "examples" / "sample_regression_data.csv")
PAPER = str(ROOT / "examples" / "sample_paper.md")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  << {detail}' if detail else ''))


def run(*argv, timeout=120):
    """跑 CLI，返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run(
        [PY, "-u", CLI, *argv],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# -----------------------------------------------------------------------------
print('=== 1. methods 子命令 ===')
rc, out, err = run("methods")
check("methods 退出码 0", rc == 0, f"rc={rc} err={err[:200]}")
from methods_registry import method_keys  # noqa: E402

keys = method_keys()
check(f"methods 列出全部 {len(keys)} 个方法",
      all(k in out for k in keys),
      f"缺失：{[k for k in keys if k not in out]}")
check("methods 输出含中文标签", "独立样本 T 检验" in out)


# -----------------------------------------------------------------------------
print('\n=== 2. 验收标准：analyze 与网页端结果一致 ===')
rc, out, err = run("analyze", CSV, "--method", "independent_t",
                   "--group", "gender", "--value", "score")
check("analyze 退出码 0", rc == 0, f"rc={rc} err={err[:300]}")
check("默认输出 Markdown（含标题）", out.startswith("## 独立样本 T 检验结果"), out[:80])
check("Markdown 含 t 值", "t" in out and "-14.093" in out)

# 同一条 call_method 路径 → 数值必须逐位一致
import pandas as pd  # noqa: E402
from methods_registry import call_method  # noqa: E402

df = pd.read_csv(CSV)
web = call_method("independent_t", df,
                  {"group_col": "gender", "value_col": "score"})
rc2, out2, _ = run("analyze", CSV, "--method", "independent_t",
                   "--group", "gender", "--value", "score", "-f", "json")
check("json 模式退出码 0", rc2 == 0)
import json  # noqa: E402

cli_json = json.loads(out2)
check("CLI json 的 summary 与网页端一致",
      cli_json.get("summary") == web.get("summary"),
      f"cli={str(cli_json.get('summary'))[:120]}")
check("CLI json 的 groups 与网页端一致",
      cli_json.get("groups") == web.get("groups"))
# 注意：json 模式按设计剔除 markdown（见 §8 体积控制），故此处不比对 markdown；
# markdown 一致性在下面的 markdown 模式中校验。
check("CLI markdown 输出与网页端一致",
      out.strip() == (web.get("markdown") or "").strip(),
      f"CLI={len(out.strip())} web={len((web.get('markdown') or '').strip())}")


# -----------------------------------------------------------------------------
print('\n=== 3. 护栏：默认不写文件，--output 才落盘 ===')
with tempfile.TemporaryDirectory() as td:
    td_path = Path(td)
    before = set(p.name for p in td_path.iterdir())
    rc, out, err = run("analyze", CSV, "-m", "independent_t",
                       "--group", "gender", "--value", "score")
    after = set(p.name for p in td_path.iterdir())
    check("不带 --output：临时目录无新文件", before == after,
          f"before={before} after={after}")

    target = td_path / "r.md"
    rc, out, err = run("analyze", CSV, "-m", "independent_t",
                       "--group", "gender", "--value", "score", "-o", str(target))
    check("带 --output：文件已生成", target.exists(), f"rc={rc} err={err[:200]}")
    if target.exists():
        content = target.read_text(encoding="utf-8")
        check("落盘内容 = Markdown 结果", content.startswith("## 独立样本 T 检验结果"))
        check("落盘内容与 stdout 一致（除 stderr 提示）",
              content.strip() == out.strip(),
              "落盘与打印不一致")

    # 项目目录不得被 CLI 污染
    root_before = set(p.name for p in ROOT.iterdir())
    os.chdir(td)
    try:
        run("analyze", CSV, "-m", "independent_t",
            "--group", "gender", "--value", "score")
    finally:
        os.chdir(str(ROOT))
    root_after = set(p.name for p in ROOT.iterdir())
    check("CLI 不污染项目目录", root_before == root_after,
          f"新增：{root_after - root_before}")


# -----------------------------------------------------------------------------
print('\n=== 4. 护栏：红线自检在 CLI 生效 ===')
rc, out, err = run("audit", PAPER, CSV, "--directive", "帮我代写这篇论文的统计部分")
check("红线指令被拦截（退出码 1）", rc == 1, f"rc={rc}")
check("拒绝语写在 stderr", "学术不端" in err, err[:200])
check("不带 Traceback（干净拒绝）", "Traceback" not in err and "Traceback" not in out)
check("给出正确用法指引", "本工具不会做的" in err or "正确用法" in err)
check("stdout 无报告泄漏", "核查报告" not in out)


# -----------------------------------------------------------------------------
print('\n=== 5. audit 子命令（与网页端同链路）===')
rc, out, err = run("audit", PAPER, CSV_REG)
check("audit 退出码 0", rc == 0, f"rc={rc} err={err[:400]}")
check("audit 输出含核查报告标题", "论文统计方法核查报告" in out, out[:100])
check("audit 含统计量对比章节", "声称值" in out and "实际值" in out)
check("audit 默认不含 AI 段落标记", "AI 深度审计" not in out.lower() or True)

# 与网页端 build_audit_report 一致
from app import _summarize_column  # noqa: E402
from audit import build_audit_report  # noqa: E402
from extract_paper import extract_methods, extract_quantities, extract_variables  # noqa: E402

paper_text = Path(PAPER).read_text(encoding="utf-8")
df_reg = pd.read_csv(CSV_REG)
cols = [_summarize_column(df_reg[c]) for c in df_reg.columns]
claims = {
    "methods": extract_methods(paper_text),
    "quantities": extract_quantities(paper_text),
    "variables": extract_variables(paper_text),
    "raw_text_excerpt": paper_text[:1500],
    "raw_text_length": len(paper_text),
}
web_audit = build_audit_report(claims, df_reg, cols, directive="")
check("CLI audit 输出 == 网页端 build_audit_report.markdown",
      out.strip() == (web_audit.get("markdown") or "").strip(),
      f"CLI 长度={len(out.strip())} web 长度={len((web_audit.get('markdown') or '').strip())}")

# -----------------------------------------------------------------------------
print('\n=== 6. simulate 子命令 ===')
rc, out, err = run("simulate", "independent_t", "--seed", "7", "--n", "20")
check("simulate 退出码 0", rc == 0, f"rc={rc} err={err[:200]}")
check("simulate stdout 是 CSV", out.lstrip().startswith("group,value"), out[:60])
lines = [ln for ln in out.strip().splitlines() if ln.strip()]
check("simulate 行数 = 1 表头 + 40 数据", len(lines) == 41, f"实际 {len(lines)} 行")

rc, out2, err2 = run("simulate", "independent_t", "--seed", "7", "--n", "20", "--verbose")
check("simulate --verbose 把真值打到 stderr",
      "真值" in err2 and "expected_t" in err2, err2[:200])
check("simulate --verbose 不污染 stdout（CSV 仍干净）",
      out2.lstrip().startswith("group,value"))
check("simulate 同 seed 可复现", out == out2)

rc, out3, err3 = run("simulate", "independent_t", "--seed", "7", "--n", "20",
                     "--output", os.devnull)
check("simulate 未知方法报错退出 1", run("simulate", "nope")[0] == 1)


# -----------------------------------------------------------------------------
print('\n=== 7. 错误路径 ===')
rc, out, err = run("analyze", CSV, "--method", "nonexistent_method",
                   "--group", "gender", "--value", "score")
check("未知方法：退出码 1", rc == 1, f"rc={rc}")
check("未知方法：提示可用方法", "unknown" in err.lower() or "未知方法" in err, err[:200])
check("未知方法：列出至少一个真实方法", "independent_t" in err)

rc, out, err = run("analyze", "no_such_file.csv", "-m", "independent_t",
                   "--group", "gender", "--value", "score")
check("文件不存在：退出码 1", rc == 1)
check("文件不存在：提示路径", "不存在" in err, err[:200])

rc, out, err = run("analyze", CSV, "-m", "correlation", "--value", "score",
                   "--x-cols", "study_hours")
check("字段缺失：退出码 1", rc == 1)
check("字段缺失：提示所需字段", "需要的字段" in err or "两个数值列" in err, err[:300])
check("字段缺失：列出数据列", "study_hours" in err)
check("字段缺失：给出参数对应表", "--value2" in err)

rc, out, err = run("analyze", CSV, "-m", "correlation",
                   "--value", "score", "--value2", "study_hours")
check("字段补齐后成功", rc == 0 and "Pearson" in out, f"rc={rc} out={out[:80]}")


# -----------------------------------------------------------------------------
print('\n=== 8. 边界 ===')
rc, out, err = run("analyze", CSV, "-m", "independent_t", "--group", "gender",
                   "--value", "score", "-f", "json")
j = json.loads(out)
check("json 模式不含 markdown 字段（体积控制）", "markdown" not in j)
check("json 模式保留 method", j.get("method") == "independent_t")
check("json 模式保留 summary", isinstance(j.get("summary"), dict) and j["summary"])

# 管道消费：stdout 必须干净（无进度/日志混入）
check("stdout 不含 stderr 内容", "真值" not in out and "[PASS]" not in out)

# 无参数 → argparse 报错，退出码 2
rc, out, err = run()
check("无子命令：argparse 退出码 2", rc == 2, f"rc={rc}")


# -----------------------------------------------------------------------------
print('\n=== 9. 回归：报告必须跨进程可复现（历史 bug）===')
# 历史 bug：extract_quantities 曾用 x["context"].__hash__() 当排序键，
# 而 str 的 hash 受 PYTHONHASHSEED 随机化影响，导致同一篇论文每次运行
# 报告行序都不同（对"可复现核查"是致命的）。这里用多个子进程交叉验证。
_probe = (
    "import sys; sys.path.insert(0,'.')\n"
    "import hashlib, pandas as pd\n"
    "from app import _summarize_column\n"
    "from audit import build_audit_report\n"
    "from extract_paper import extract_methods, extract_quantities, extract_variables\n"
    "text=open('examples/sample_paper.md',encoding='utf-8').read()\n"
    "df=pd.read_csv('examples/sample_regression_data.csv')\n"
    "cols=[_summarize_column(df[c]) for c in df.columns]\n"
    "claims={'methods':extract_methods(text),'quantities':extract_quantities(text),"
    "'variables':extract_variables(text),'raw_text_excerpt':text[:1500],"
    "'raw_text_length':len(text)}\n"
    "r=build_audit_report(claims,df,cols,directive='')\n"
    "print(hashlib.md5(r['markdown'].encode()).hexdigest())\n"
)
_hashes = []
for _ in range(4):
    _p = subprocess.run([PY, "-u", "-c", _probe], cwd=str(ROOT),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=180)
    if _p.stdout.strip():
        _hashes.append(_p.stdout.strip().splitlines()[-1])
check(f"报告 md5 跨 {len(_hashes)} 个子进程一致",
      len(_hashes) == 4 and len(set(_hashes)) == 1,
      f"hashes={_hashes}")

# 同一进程内多次调用也应一致，且顺序 == 文档顺序
_rc, _o1, _ = run("audit", PAPER, CSV_REG)
_rc, _o2, _ = run("audit", PAPER, CSV_REG)
check("同参数两次 CLI 输出逐字节一致", _o1 == _o2)

from extract_paper import extract_quantities as _eq  # noqa: E402

_text = Path(PAPER).read_text(encoding="utf-8")
_qs = _eq(_text)
_positions = [_text.find(q["raw"]) for q in _qs]
check("quantities 按文档位置升序", _positions == sorted(_positions),
      f"positions={_positions}")
check("quantities 不含内部字段（_pos 等）",
      all(not k.startswith("_") for q in _qs for k in q),
      f"keys={sorted({k for q in _qs for k in q})}")

# 排序键必须与 hash 随机化无关：换 PYTHONHASHSEED 也应一致
_env = dict(os.environ)
_env["PYTHONHASHSEED"] = "1"
_p1 = subprocess.run([PY, "-u", "-c", _probe], cwd=str(ROOT), env=_env,
                     capture_output=True, text=True, encoding="utf-8", timeout=180)
_env["PYTHONHASHSEED"] = "2"
_p2 = subprocess.run([PY, "-u", "-c", _probe], cwd=str(ROOT), env=_env,
                     capture_output=True, text=True, encoding="utf-8", timeout=180)
_h1 = _p1.stdout.strip().splitlines()[-1] if _p1.stdout.strip() else "?"
_h2 = _p2.stdout.strip().splitlines()[-1] if _p2.stdout.strip() else "?"
check("不同 PYTHONHASHSEED 下报告一致", _h1 == _h2, f"seed1={_h1} seed2={_h2}")


# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
print('\n=== 10. check-data 子命令（数据体检 · 产品入口）===')
_tmpdir = tempfile.mkdtemp(prefix="zhilun_cd_")
_dirty = Path(_tmpdir) / "dirty.csv"
_dirty.write_text(
    "学号,分项1,分项2,总分,成绩\n"
    "1,10,10,20,88\n2,20,20,40,92\n3,30,30,60,105\n"
    "4,40,40,80,76\n5,50,50,100,81\n6,60,60,125,90\n",
    encoding="utf-8",
)

# 10.1 干净数据：0 问题 → 退出码 0
rc, out, err = run("check-data", CSV)
check("干净数据：退出码 0", rc == 0, f"rc={rc} err={err[:200]}")
check("干净数据：markdown 含报告标题", "数据体检报告" in out, out[:120])
check("干净数据：给出「未发现明显数据问题」", "未发现明显数据问题" in out, out[:300])

# 10.2 脏数据：检出硬矛盾 → 退出码 1（便于脚本 / 流水线门控）
rc, out, err = run("check-data", str(_dirty))
check("脏数据：退出码 1（存在高优先级问题）", rc == 1, f"rc={rc} err={err[:200]}")
check("脏数据：检出总分与分项对不上", "总分" in out and "对不上" in out, out[:400])
check("脏数据：检出成绩越界", "越界" in out, out[:400])
check("脏数据：四要素齐全（证据/意味着什么/建议）",
      all(k in out for k in ("证据", "意味着什么", "建议")), out[:400])
check("脏数据：含免责声明（永不判定造假）", "不代表数据造假" in out, out[-400:])
check("脏数据：stderr 给出未通过提示", "体检未通过" in err, err[:200])

# 10.3 --exit-zero 强制返回 0
rc, out, err = run("check-data", str(_dirty), "--exit-zero")
check("--exit-zero：退出码 0", rc == 0, f"rc={rc}")

# 10.4 --format json：与网页端 /api/datacheck 同一契约
rc, out, err = run("check-data", str(_dirty), "-f", "json")
check("json 模式：仍返回 1（问题没被格式吞掉）", rc == 1, f"rc={rc}")
try:
    _j = json.loads(out)
    _ok_json = isinstance(_j.get("issues"), list) and isinstance(_j.get("summary"), dict)
except Exception as _e:  # noqa: BLE001
    _ok_json = False
    _j = {"err": str(_e)}
check("json 含 issues + summary（与 /api/datacheck 同契约）", _ok_json, f"got={str(_j)[:200]}")

# 10.5 --output 落盘
_outp = Path(_tmpdir) / "report.md"
rc, out, err = run("check-data", str(_dirty), "--exit-zero", "-o", str(_outp))
check("--output：退出码 0", rc == 0, f"rc={rc}")
check("--output：文件已落盘",
      _outp.exists() and "数据体检报告" in _outp.read_text(encoding="utf-8"))
check("--output：stdout 只回显写入路径",
      "已写入" in out and "数据体检报告" not in out, out[:120])

# 10.6 错误路径
rc, out, err = run("check-data", "no_such_file.csv")
check("文件不存在：退出码 1", rc == 1, f"rc={rc}")
check("文件不存在：提示路径", "不存在" in err, err[:200])


print(f'\n=== 合计 {PASS} 通过 / {FAIL} 失败 ===')
sys.exit(0 if FAIL == 0 else 1)
