# 贡献指南

感谢你愿意改进智论助手。本文档说明**怎么改、怎么验、有哪些坑**。

---

## 一、开发环境

```bash
git clone <repo-url>
cd 论文排版辅助agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                    # http://127.0.0.1:5000
```

**不需要 Node.js / 构建步骤**——前端是原生 HTML/JS，改完刷新即可。

环境变量对测试很重要：

```bash
export NO_PROXY=127.0.0.1,localhost   # 否则本机请求可能被代理拦截
```

---

## 二、动手前先读

| 你要改的东西 | 先读 |
| --- | --- |
| 任何代码 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 加/改统计方法 | 上文的「新增一个方法要同步改 8 处」 |
| 涉及 LLM / prompt | 上文的「缓存与版本纪律」 |
| 涉及红线 / 语气 | [`README.md` 第九节](README.md#九合规边界重要) |
| 部署 / H5 上线 | [`docs/DEPLOY_H5.md`](docs/DEPLOY_H5.md) |
| 产品方向 | [`ROADMAP.md`](ROADMAP.md) / [`专属桌面端开发总纲.md`](专属桌面端开发总纲.md) |

---

## 三、几条硬性纪律

### 1. 冻结 prompt 改动必须 bump `PROMPT_VERSION`

`llm_enhance.py` / `llm_audit.py` / `audit_chat.py` 的 `FROZEN_SYSTEM` 参与缓存键计算。
改了措辞却不 bump 版本，旧缓存会被错误复用。**bump 版本号即可，不要改缓存键算法。**

### 2. 不要用 `str.__hash__()` 当排序键

Python 字符串 hash 受 `PYTHONHASHSEED` 随机化影响，跨进程结果不同。
本项目曾因此让同一篇论文的报告行序每次运行都变。**按位置/序号排序。**

### 3. 断言要测契约，不要冻结快照

反例（都真实发生过误报）：

```python
# ❌ 新增平台后立刻误报
assert set(PROVIDER_REGISTRY) == {"sf", "zhipu", "deepseek", "dashscope", "maas"}
assert PROMPT_VERSION == "v2"
assert len(STATE_TO_MODEL["paper_check"]) == 4
```

```python
# ✅ 测契约
assert {"sf", "zhipu", "deepseek", "dashscope", "maas"} <= set(PROVIDER_REGISTRY)
assert re.fullmatch(r"v\d+", PROMPT_VERSION) and int(PROMPT_VERSION[1:]) >= 2
```

### 4. 隔离 Key 时从注册表派生清单

```python
from agents.openai_compat import PROVIDER_REGISTRY
ALL_KEY_ENVS = tuple(sorted({
    e.strip()
    for cfg in PROVIDER_REGISTRY.values()
    for e in (getattr(cfg, "env_var", "") or "").split(",")
    if e.strip()
}))
```

硬编码清单会在新增平台时漏掉，导致「无 Key 降级」被真调顶掉而假失败。

### 5. 测试只能用合成 Key

真实凭据绝不进代码、不进测试、不进提交。报错出口已由 `secrets_guard` 脱敏，
但不要把真实 Key 送进去测试它。

### 6. 统计量不交给 LLM 计算

LLM 只做「把已算好的结果讲成人话」。新增 AI 功能时，输入要用**白名单构造**，
不要用 `**obj` 展开（可能把 DataFrame / 原始数据带进 prompt）。

### 7. `RATE_LIMIT_DISABLE` 只能在需要它的脚本内部设置

这条**坑过本项目两次**，务必记住。

`security_guard_test.py` 测的就是限流器本身。如果你在批处理里**全局** `export
RATE_LIMIT_DISABLE=1` 然后再跑它，它会自己把自己的被测对象关掉，于是出现 4 个
莫名其妙的失败（实际是 52/4，单跑是 56/0）。

```bash
# ❌ 全局 export，会毒害后续所有脚本
export RATE_LIMIT_DISABLE=1
for t in *_test.py; do python "$t"; done

# ✅ 让需要它的脚本自己设（脚本内部 os.environ.setdefault）
for t in *_test.py; do python "$t"; done
```

需要关闭限流的脚本，应在**自身文件内**设置：

```python
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")   # 放在 import app 之前
```

不要用 `os.environ[...] = "1"`（会覆盖外部显式设置，破坏「默认安全」的可见性）。

---

## 四、验证流程

```bash
# 1. 语法
python -m compileall -q app.py cli.py agents

# 2. 前端内联 JS 语法（改过 templates/index.html 时必做）
node --check <(sed -n '/<script>/,/<\/script>/p' templates/index.html)

# 3. 相关测试
python registry_test.py      # 改了方法清单
python cli_test.py           # 改了 cli.py
python security_guard_test.py

# 4. 有服务依赖的（先另开终端跑 python app.py）
python smoke_test.py
python paper_check_test.py
```

前端**逻辑**正确性（不是语法）需要另写 DOM mock 探针，放在 `_syntaxcheck/` 下。
`node --check` 查不出「选条优先级算错」这类问题。

> ⚠️ `_syntaxcheck/` 是多人共用的探针目录，**不要整体删除**。

### 起服务供测试的坑

```python
# ✅ 必须
app.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
```

- Flask 的 **reloader 子进程会随父 shell 退出被回收**，表现为「curl 通了，
  下一秒 WinError 10061」。用 `use_reloader=False`。
- 不要手工设 `WERKZEUG_RUN_MAIN=true`（会触发 `KeyError: 'WERKZEUG_SERVER_FD'`）。
- **Windows 允许多个进程绑定同一端口**，操作系统会在它们之间**轮询负载均衡**。
  于是同一个 URL 时好时坏（本项目曾出现 `/favicon.ico` 间歇 404，根因是上一轮
  会话遗留的旧服务进程仍在监听 5000，旧进程没有那条路由）。
  排查与清理：

```bash
netstat -ano | grep ':5000'          # 拿到 LISTENING 的 PID
# 确认真的是本项目遗留进程后再杀：
#   在 PowerShell 里 Get-CimInstance Win32_Process -Filter "ProcessId=<PID>"
```

```powershell
Stop-Process -Id <PID> -Force
```

> 改完 `app.py` 路由后若见「新增路由 404」，先查端口是不是被旧进程占着，
> **不要**急着怀疑路由写错。用 `test_client` 能 200、真实 HTTP 却 404，
> 基本就是这个问题。

---

## 五、提交

- 提交信息说明**为什么改**，而不只是改了什么。
- 涉及行为变更的，在 [`CHANGELOG.md`](CHANGELOG.md) 相应版本下补一行。
- 新增能力后跑一遍全量回归：

```bash
export NO_PROXY=127.0.0.1,localhost   # 只设这个；不要全局设 RATE_LIMIT_DISABLE
for t in *_test.py; do
  echo "── $t"
  python -u "$t" || echo "FAILED: $t"
done
```

以退出码为准，不要用输出里的符号判断成败（有些脚本本身会打印 ❌ 表情）。

### 退出码约定（重要）

本项目统一使用三种退出码，批量回归时要分清：

| 码 | 含义 | 处理 |
| --- | --- | --- |
| `0` | 通过（含"无 Key 自动 SKIP"、"未指定 --live 跳过"） | 正常 |
| `1` | **真的测试失败**（代码或断言有问题） | 必须修 |
| `2` | **环境未就绪**（没起服务等），不是代码缺陷 | 起服务后重跑 |

4 个走真实 HTTP 的套件（`smoke` / `methods` / `paper_check` / `regression_audit`）
在服务未启动时会打印明确的「未检测到本地服务」并返回 **2**。
看到 2 就去起服务，**不要**当成 bug 去改代码。

- **需要真实网络的脚本不属于回归集**，不要放进 CI 批跑：
  `kimi_mimo_live_test.py`（需 `--live`）、`llm_cache_test`、`prefix_cache_test`、
  `zhipu_cache_test` 等会真调 LLM（慢、要 Key、要额度），且**在批处理里容易被
  超时杀掉**，看起来像失败其实是 SIGTERM。单独给足时间运行。
- 有 4 个脚本**必须先起服务**（即上表那 4 个）。其余走 `test_client`，无需网络。
- `wsgi_test.py` 会**自己起一个 waitress 子进程**（本机随机端口），
  不依赖外部服务，所以它应该留在 CI 里 —— 它守护的是 H5 部署契约。

---

## 六、不要做的事

- ❌ 不要实现「规避 AI 检测 / 查重」类功能（见 README 第九节，这是合规红线）。
- ❌ 不要为了通过测试而放松断言（先从业务上判断是代码错还是测试错，
  本项目两种情况都出现过）。
- ❌ 不要引入需要构建步骤的前端框架（保持开箱即跑）。
- ❌ 不要把 `模板论文/`、`.env`、真实数据提交进仓库（已在 `.gitignore`）。
- ❌ 不要因为复跑通过就认为问题解决——保留原始退出码与完整日志，
  分清 PASS / SKIP / FAIL。

---

## 七、环境说明

- 主开发环境：Windows + Git Bash + `.venv/Scripts/python.exe`。
- 测试矩阵：Python 3.10 / 3.12 × Linux / Windows（见 `.github/workflows/tests.yml`）。
- 若某测试在 CI 间歇失败，**先确认是不是依赖了本机状态**（端口、临时目录、Key），
  再判断是否真 flaky。
