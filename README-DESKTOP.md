# 智论助手 · 桌面端（内测版）

> 单文件 exe，双击即用，**无需安装 Python**。仅本机内测使用。

## 一、快速开始

1. 双击 `dist/智论助手.exe`
2. 控制台窗口出现后，浏览器会自动打开 `http://127.0.0.1:5000`
3. 用完关闭控制台窗口即可（服务随之停止）

**注意**：不要关闭那个黑色控制台窗口，它是后端服务本体。

## 二、它做了什么

启动器（`desktop_launcher.py`）在同一个进程里：

1. 检测 5000 端口
   - 如果已有本应用在跑 → 直接打开浏览器复用，不重复启动
   - 如果被别的程序占用 → 自动顺延到 5001、5002… 并提示实际地址
2. 拉起 Flask 后端（`app.py`）
3. 延迟 2 秒自动打开浏览器

两个 Tab 功能都在：

- **数据分析**：**12 种统计方法**（T 检验 / ANOVA / 相关 / 卡方 / 配对 T / Mann-Whitney / Wilcoxon / 线性回归 / Logistic 回归 / **Cronbach's α** / **双因素 ANOVA** / **重复测量 ANOVA**）+ 自动图表 + Word 导出
- **论文排查**：上传论文 + 数据 → 识别方法/统计量/变量 → 规则化建议 → 可导出 Word

## 三、重新打包

```bash
# 在项目根目录，用项目自带的 .venv
.venv/Scripts/python.exe build_desktop.py
```

流程：**预检清单** → 生成图标 → PyInstaller 打包 → 校验产物，产物落在 `dist/智论助手.exe`。

| 文件 | 作用 |
| --- | --- |
| `build_desktop.py` | 一键打包入口（含打包前预检） |
| `desktop.spec` | PyInstaller 配置（资源清单 + 隐藏导入） |
| `desktop_launcher.py` | 桌面启动器（端口检测 + 拉起 Flask + 开浏览器） |
| `make_icon.py` | 生成 `assets/icon.ico` / `icon.png` |
| `_exe_e2e.py` | **打包后**的功能验证（启动 exe 跑完整链路） |

### ⚠️ 打包前预检（必须理解这一条）

**新增任何模块后，必须回 `desktop.spec` 的 `datas` 列表补一行。**

漏掉的后果不是"少个功能"，而是**双击直接闪退** —— ImportError 发生在 Flask 启动之前，
用户只看到窗口一闪，自己完全查不出原因。

> **真实事故**：2026-09-11 那次打包漏了 `datacheck` / `table_check` / `grimmer` /
> `access_guard` / `paper_writer` / `defense_pack` / `security_guard` / `cross_platform` /
> `methods_registry` / `tone_guide` 等十余个模块，产出的 93.9MB exe 是**完全不可用的**。
> 当时没有人跑过一次，所以没人发现。

所以 `build_desktop.py` 现在会**先做静态预检**：解析入口可达的全部本地 import
（含函数内的延迟 import），与 spec 的 `datas` 求差集，缺任何一项就**退出码 2 并列出清单**，
不浪费 2–3 分钟做一次注定失败的构建。

```bash
# 预检失败的样子
[预检失败] desktop.spec 的 datas 清单缺少以下本地模块：
    - datacheck                   (被 audit.py 引用)
  修法：打开 desktop.spec，在 datas 列表里补上对应条目
```

紧急情况下可用 `--no-preflight` 跳过（不推荐）。

### 打包后请跑一次功能验证

构建成功 ≠ 功能可用（预检只保证"模块都进去了"）。`_exe_e2e.py` 会**真的启动 exe**，
走完 12 步完整链路再关掉：

```bash
.venv/Scripts/python.exe _exe_e2e.py
```

覆盖：启动就绪 → 首页 → 上传 → 数据体检 → 清洗副本 → T 检验 → 方法知识图谱
→ 论文核查（含表格交叉核查）→ 流水线阶段 → 答辩准备包 → 示例数据 → 门禁零干扰。

全绿（`12 通过 / 0 失败`）才建议发给同学。


## 四、LLM 功能说明

数据分析的「AI 深度解读」需要 API Key（环境变量）：

```
ZHIPU_API_KEY / DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / SILICONFLOW_API_KEY
```

没有 Key 时会**静默降级**为规则化输出，其余功能不受影响。
（结果缓存命中时甚至完全不需要 Key。）

> 桌面版目前读取系统环境变量；如需在 exe 内配置 Key，可在启动器里补一个设置界面（后续迭代）。

## 五、为什么不用 Tauri

最初计划用 Tauri（包体 5–10 MB）。但 Tauri 需要 MSVC 编译工具链，本机
Visual Studio Build Tools 安装失败，Rust 的 GNU 工具链又缺 `dlltool`，
导致构建卡住。

PyInstaller 方案包体 94.6 MB，但**零额外依赖、构建稳定**，对「本机内测」场景
完全够用。

## 六、当前版本状态（v2.14）

| 项 | 值 |
| --- | --- |
| 产物 | `dist/智论助手.exe` |
| 体积 | **94.6 MB**（单文件，免装 Python） |
| 构建校验 | `python build_desktop.py` → 预检通过 + rc=0 |
| **功能验证** | **`python _exe_e2e.py` → 12 通过 / 0 失败** |
| 打包模块 | 24 个本地模块全部就位（见 `build/desktop/Analysis-00.toc`） |

exe 已验证可用路由：`/` · `/health` · `/api/upload` · `/api/datacheck` ·
`/api/datacheck/fix` · `/api/analyze` · `/api/methods_graph` · `/api/check_paper` ·
`/api/copilot/phases` · `/api/defense_pack` · `/api/sample/*` · `/unlock` · `/logout`

## 七、已知问题

- 首次启动解压到临时目录，约需 5–10 秒才打开浏览器，属正常
- 未做代码签名，Windows SmartScreen 可能提示「未知发布者」，选择「仍要运行」
- 端口 5000 若被占用会自动顺延，请以控制台打印的地址为准
- **exe 是 Windows 专用的**：同学得用 Windows。Mac / Linux 用户请走
  `python app.py` 本地跑，或直接用网页版链接

## 八、发给同学怎么发

exe 单文件 94.6 MB，微信/QQ 传文件都行（超过 100MB 才需要压缩）。

**建议一并说明**：
1. 双击后**不要关黑色控制台窗口**，它就是服务本体
2. 若 Windows 弹「未知发布者」→ 点「更多信息」→「仍要运行」
3. 首次打开慢 5–10 秒是正常的（在解压）
4. 数据不出本机 —— 不上传服务器，分析完全地跑在这台电脑上

> 嫌 94.6 MB 太大？网页版反而更轻：一个链接搞定，还不用升级。
> 两条腿走路的取舍见 `部署上手指南.md` §12。

