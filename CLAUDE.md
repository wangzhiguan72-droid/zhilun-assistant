# CLAUDE.md · 并行智能体协作约定（必读）

> 本项目由**多个智能体会话并行开发**（ZCode / WorkBuddy / Claude Code 等），
> 2026-09-14 曾发生过三起并行交叉事故：重复端点、版本号三处漂移、临时文件互删。
> **任何会话动手前，先读完本文件。** 本文件是三方约定的唯一真源。

---

## 一、项目身份（30 秒）

**智论助手**：装在学生电脑里的「论文数据安检 + 写作副驾」。Flask 单进程 +
原生 HTML 前端（无构建步骤），统计计算全本地（numpy/scipy），LLM 只做可选增强。

四条永不退让的红线：
1. 计算本地跑，LLM 不碰原始数据（只喂统计量 dict）
2. 数据体检/核查只报「可疑，请核对」，**永不判定造假**
3. 用户数据全内存（例外：协作审阅的 `.review_share/` 是产品功能，落盘且已 gitignore）
4. 不做代写、不绕检测；AI 痕迹自查只评「风格风险」，不判作者身份

## 二、版本与单一真源（改前先看，别再造第二份）

| 真源 | 位置 | 谁引用它 |
| --- | --- | --- |
| 版本号 | `version.py` 的 `APP_VERSION` | 页面角标 / 启动器横幅 / 构建报告 |
| 方法名 | `methods_registry.py` | 前端 methodLabel 映射表（有探针锁定） |
| BYOK 字段 | `app.py _BYOK_FIELDS` | 前端 FIELD_MAP（有探针锁定，两端必须同步改） |
| 前端插件组 | `/api/plugins`（`plugin_registry.py`） | 方法下拉动态填充 |

改版本号 = 只改 `version.py` 一行 + CHANGELOG.md 加一段。**页面和启动器里
不许再出现写死的版本字符串**（CI 探针 `_syntaxcheck/method_label_probe.js`
会锁住方法映射，BYOK 探针锁住字段）。

## 三、目录归属与高冲突区

**高冲突公共区**（多方都要改，纪律最严）：
- `app.py`（39+ 路由）：改前 `git status` + 重读最新源码；加端点前先 grep 同名路由
- `templates/index.html`（4000+ 行单文件）：改动小步快走；改完必跑
  `node --check` 抽取校验 + `_syntaxcheck/*_probe.js`
- `desktop.spec`：**任何新增本地模块必须在 datas 补一行**，否则 exe 双击闪退
  （`build_desktop_test.py` 会红）；hiddenimports 记得第三方库

**模块归属**（owner 优先改自己的；动别人的先读其模块 docstring）：
- `agents/`（多平台 LLM 层）、`ai_audit.py`、`defense_pack.py`、
  `desktop_launcher.py` 托盘/拖拽、`/api/local_open`、Kimi/MiMo 接入 → ZCode 线
- 协作审阅 `review_share.py`、模拟数据 `simulate.py`、Word 导出、
  启动器 v2.19 加固、打包预检 → WorkBuddy 线
- 数据体检 `datacheck.py`、取证（GRIM/GRIMMER/本福特）、`table_check.py` → Claude 线

**临时产物**：会话级脚手架一律放 `_` 前缀文件（.gitignore 已排除）；用完即删。
可复用的验证脚本放 `scripts/`（已入库，改名需同步 `build_desktop.py` 与 README）。

## 四、测试与 CI 契约（改动前必知）

1. **新模块**：命名 `*_test.py` 放根目录（CI 自动发现）；必须同时在
   `desktop.spec` datas 补行——`build_desktop_test.py` 断言预检 rc=0，漏了就红。
2. **全量回归**：`.venv/Scripts/python.exe scripts/regress_run.py`
   （单元 + exe 链路）；快跑加 `--fast`。
3. **CI（.github/workflows/tests.yml）**：
   - 套件下限 `ran >= 30`（当前 57 个套件，排除 4 个真调 API 的 LIVE 白名单，
     ran≈53，余量充足；删套件前先想这条）
   - LIVE 白名单（真烧 API，CI 排除）：`llm_cache_test prefix_cache_test
     zhipu_cache_test kimi_mimo_live_test`——新写真调外部 API 的测试要么进
     白名单，要么自带 SKIP 门槛
   - **前端探针 `_syntaxcheck/*_probe.js`（≥3，自动发现）已入库**——它们用
     node 真跑前端函数契约（DOM mock），删目录会让 CI 红；新增前端契约时
     顺手加探针，别只靠肉眼看页面
4. **exe 验证**：`scripts/exe_e2e.py`（构建后必跑，12 步链路）。

## 五、提交与推送纪律

- **推送权在用户**：任何会话不得 `git push`。首推三行（等用户发话）：
  `git remote add origin https://github.com/tiandaozongsi/zhilun-assistant.git`
  `git branch -M main`
  `git push -u origin main`
- **commit 前置**：全量回归绿 + 看一眼 `git status`——并行会话的工作区经常有
  别人的半成品，`git add -p` 逐块确认，别把别人的中间态一起带走。
- commit 信息用中文 conventional 风格（`feat: v2.XX — 一句话`），与 CHANGELOG
  条目对应。

## 六、平台注意事项

- Windows + Git Bash；Python 一律用 `.venv/Scripts/python.exe`
- exe 控制台重定向下 print 是块缓冲：启动诊断必须 `flush=True`（托盘那课）
- HTTP 响应头只允许 latin-1：中文放头里 werkzeug 直接断连（X-Missing-Charts 那课）
- 中文与英文缩写相邻时 `\b` 词边界失效（"采用LSTM" 检测不到，用 lookaround）
- 思考型模型（Qwen3.7 / mimo-v2.5 / kimi-k2.6）小 max_tokens 会被思考吃光，
  正文为空——默认 4096 起

_本文件由三方共同维护：改约定先在 CHANGELOG 记一笔，让其它会话能感知。_
