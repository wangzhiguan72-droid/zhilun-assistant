# 智论助手 · ZhiLun Assistant

**本地运行的毕业论文「数据体检 + 统计核查 + 写作副驾」。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Tests](https://img.shields.io/badge/tests-57%20suites-brightgreen)](#七测试)
[![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE)
[![No Build](https://img.shields.io/badge/build-none%20required-success)](#二快速开始)

从 Excel/CSV 到可写进论文的统计结果，再到用真实数据反向核查论文里的统计量是否对得上——
一条**可追踪、不越界**的流水线。

> 核心统计全部由 Python 计算，**不配任何 API Key 也能完整跑通**。AI 能力是可选的「翻译层」，
> 只把已算好的结果讲成人话；它挂了，功能不中断。

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、快速开始](#二快速开始)
- [三、六种用法](#三六种用法)
- [四、产品主线：八站流程](#四产品主线八站流程)
- [五、设计原则](#五设计原则)
- [六、项目结构](#六项目结构)
- [七、测试](#七测试)
- [八、扩展点（设计已留好接口）](#八扩展点设计已留好接口)
- [九、合规边界（重要）](#九合规边界重要)
- [十、文档](#十文档)
- [十一、限制](#十一限制)
- [十二、许可证](#十二许可证)

---

## 一、它能做什么

### 十二个统计方法（全部纯 Python 实现，不依赖 statsmodels）

| 方法 | 关键产出 |
| --- | --- |
| 独立样本 T 检验 | 方差齐性 / Welch 校正、Cohen's d、95% CI |
| 配对样本 T 检验 | Cohen's d_z、Shapiro 正态性提示 |
| 单因素 ANOVA | Levene、η²、Bonferroni 事后多重比较 |
| 双因素 ANOVA | Type III 平方和、主效应 + 交互效应 |
| 重复测量 ANOVA | Mauchly 球形度检验、Greenhouse-Geisser 校正、Bonferroni 事后比较 |
| Pearson 相关 | r、95% CI、t、效应量解释 |
| 卡方检验 | χ²、Cramér's V、期望频数警告 |
| Mann-Whitney U | 非参数两组、效应量 r、中位数 + IQR |
| Wilcoxon 符号秩 | 非参数配对、零差值剔除、效应量 r |
| 多元线性回归 | OLS、R²/调整 R²、F 检验、系数 t 检验 + 95% CI、VIF |
| 二元 Logistic 回归 | IRLS、Wald z、OR 值、McFadden 伪 R² |
| Cronbach's α | α、删项后 α、CITC、信度等级建议 |

每个方法都自动产出：描述统计表格、前提条件检验、**可直接引用进论文的结论段**、
以及对应的图表（柱图 / 散点 / 箱线 / 连线）。结果可一键导出 Word
（标题/表格/列表自动排版 + 图表嵌入，宋体 + Times New Roman）。

### 数据体检（产品入口）

上传即体检，**纯本地规则、零 LLM**，检出 12 类数据问题：

合计≠分项 · 取值越界 · 计数列出现小数 · 重复行 · 前后测差值规律 ·
常数列 · 反向题漏反向计分 · 直线作答 · **作答时长过快** · 缺失模式异常 ·
**本福特分布不符** · **末位数字偏好**

还可一键生成**清洗副本**：只自动修「语义无歧义」的两类（重复行、反向题反向计分），
合计≠分项这类无法判断谁错的**只标记不改**，且**绝不触碰你的原文件**。

### 学术级取证（v2.13 · 纯 numpy，零 LLM）

数据体检里最后两类是**学术取证**，用来回答"这批数字像不像人编的 / 人工读的"：

| 检验 | 看什么 | 典型场景 |
| --- | --- | --- |
| **GRIM**（均值） | `n × 均值` 必须是粒度的整数倍 | 论文写 M=3.47、n=30，但 30×3.47=104.1 —— 这份 n 根本产生不出这个均值 |
| **GRIMMER**（均值+标准差） | 即便均值可能，SD 仍可能落在整数数据**不可达**的区间 | 均值自洽、标准差却无解 |
| **本福特分布** | 首位数字应遵循 `log10(1+1/d)` | 人随手编数时首位是**均匀**想的，分布会明显偏平 |
| **末位偏好** | 末位数字应均匀（各 10%） | 血压全记成 120/130 而非 123 —— 人工取整会压缩方差 |

**误报是这四把刀的生命线**，所以设了重重闸门：

- 本福特用 **MAD**（审计实务刻度，**与样本量无关**）判定，不用卡方——
  卡方在 n 大时几乎必然显著，那是台误报机器；列名含编号/年月/量表/百分比/金额、
  样本量 < 100、跨不到 2 个数量级、唯一值 < 50 的列**一律不判**。
- 末位偏好要求**双门槛同时成立**：卡方 p < 0.001 **且**某个末位占比 ≥ 25%。
  只显著但偏离很小（12% vs 10%）不报。
- 四者级别**最高只到「可疑」**，文案恒含"这只是线索，不代表造假"。

论文侧的 GRIM / GRIMMER 交叉核查走 `audit.py`（**样本量取你的数据实算行数，不采信论文写的 n**）。

### 论文统计核查

上传论文 + 数据 → 识别论文声称的统计方法与统计量 → 用你的真实数据重跑一遍 →
逐项对比「论文写的」vs「实算的」，不一致的地方给出**具体排查方向**
（样本范围 / 缺失值处理 / 是否用了别的统计量），而不是一句空话。

支持对任意一条比对**追问**（审计对话），回答附带「依据：论文 x / 实算 y」回执。

**AI 痕迹自查**（无需数据）：只传论文，纯本地规则检查高频套话密度、句长均匀性、被动句、拔高词无数据、AI 对话残留、数模高风险模型未说明等 8 类 AI 风格特征，给出分项扣分与修改建议。只评估风格风险，不判定作者身份。

论文是 **.docx** 时，还会把里面的**描述统计表**（组别 | n | M | SD）逐格和你的数据核对——分组对得上才核对、对不上不硬猜，表格数字与数据不符会点名到行。

### 答辩准备包（v2.10 · 八站流程最后一站）

本页每成功跑一次分析就记一笔；点「🎓 答辩准备」一键生成**高频 Q&A 预演**
（为什么用这个方法 / 前提满足吗 / 效应量多大 / 样本量够吗 / 数据清洗过吗），
每条答题要点都引用**后端重新实算**的统计量——不是抄屏幕上的数字。
再把会话内所有统计图**一键打包 zip** 下载（内存打包，不落盘）。纯规则零 LLM。

### 可插拔方法市场（v2.14）

一个统计方法 = **一个独立 .py 文件**，丢进 `plugins/` 重启即可用 —— 不改 app、不改前端、不动注册表。

```python
# plugins/run_my_method.py
SCHEMA = {"key": "my_method", "label": "我的方法",
          "needs": ["group_col", "value_col"],
          "params": [{"name": "alpha", "default": 0.05}]}

def run(df, group_col, value_col, alpha=0.05, **kw) -> dict: ...
```

三条护栏：**沙箱执行**（子进程跑，10 秒超时即杀，插件死循环只废这一次调用）、
**结果合理性校验**（p∈[0,1]、df>0、n≥2，不通过绝不进报告）、
**契约校验**（单个坏插件不拖垮其它插件，失败原因在 `/api/plugins` 里可见）。

内置示例插件 **TOST 等价性检验** —— 纠正"p > 0.05 就是两组没差异"这个最常见的误用：
不显著只代表"没检出"，要论证等价必须做等价性检验，并**事先说明等价边界 Δ 的依据**。

> 插件**不会**并进内置方法注册表：那张表是内置方法的真源，前端下拉、副驾驶、
> 方法图谱都依赖它的完整性。插件走"注册表不认识 → 才查插件市场"的兜底分发。

### 模拟数据生成器（v2.17）

没有数据也能完整走一遍流程：选方法 + 效应量 + 样本量 + 种子 → 一键生成
**可复现**的演示数据，直接载入分析流程（与真实上传同池，同样触发「上传即体检」）。
用来备课、做 demo、或者反过来**验证工具自己算得对不对**。

```bash
python cli.py simulate two_way_anova --n 30 --seed 42 --verbose
```

每次生成都附一份 `truth`（这份数据**应得**的统计量）。`expected_*` 可与 `run_*`
精确比对 —— 测试对 12 个方法 × 3 个 seed 逐位验过，对不上就是 bug。

> ⚠️ 生成的是**模拟数据**，只用于演示 / 备课 / 验证算法，
> **不能当真实研究数据写进论文**。文件名、界面、接口返回值三处都做了标注。

### 协作审阅（v2.16）

核查报告一键变成**可发给导师 / 同门的链接**（`/s/<token>`）：对方打开即可阅读
并追加批注，不必装 Python、不必有账号。

```bash
# 默认就落在项目内 .review_share/，零配置；想彻底关掉：
export REVIEW_SHARE_DIR=off
```

四条底线：

| 底线 | 怎么保证的 |
| --- | --- |
| **报告只读、批注另存** | 批注只追加/删除，**绝不改** `markdown`/`comparisons`；测试逐字节比对过 |
| **链接不可猜** | 96 bit 随机 token，**不是自增 ID**；非法 token 一律 404（不是 500） |
| **无 XSS** | 页面**不含任何 JS**，CSP 收到 `default-src 'none'`，所有内容过 `html.escape` |
| **本地优先** | 只写 `.review_share/`，**不上传任何云端**；删掉目录即彻底销毁，默认 30 天过期 |

> 报告是证据，意见是意见 —— 两者物理分开，所以"协作审阅"不会变成
> 「改完再说是导师意见」的新造假通道。

两个入口都能分享：**论文排查报告**（Tab2）与**数据体检报告**（Tab1，产品入口
第一站，发导师看"这份数据有没有问题"更早也更有用）。体检报告的 `markdown` 由
后端 `datacheck.render_markdown()` 统一渲染，与页面、CLI `check-data` 三处一种说法。

管理接口 `GET /api/review/list`（仅元信息，不含正文）与 `POST /api/review/gc`
（只清过期）**在访问门禁内**——分享页是给没口令的人看的，分享列表不是。

### 论文副驾驶

六阶段流水线：资料调研 → 文献综述 → 研究设计 → 数据分析 → 论文核查 → 论文撰写。

带**证据约束写作引擎**：claim 台账（来源 / 基线 / 置信度）+ 写作闸门自检
——没有来源的论断不许进正文、非高置信度不许进摘要、大词必须有数字支撑。
LaTeX 与中文学位论文双模板。可选 LLM 润色层（丢锚点 / 改数字则静默回退原稿）。

---

## 二、快速开始

不需要 Node.js、不需要构建步骤，`git clone` 之后两步即可。

```bash
# 0. 拉代码（公开仓库，无需登录）
git clone https://github.com/tiandaozongsi/zhilun-assistant.git
cd zhilun-assistant

# 1. 安装依赖
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 启动
python app.py
```

浏览器打开 <http://127.0.0.1:5000>（默认只监听本机，不对外暴露）。

> 💡 不想装 Python？见 [README-DESKTOP.md](README-DESKTOP.md)（免安装桌面版）。
> 也可以用 Docker：`docker compose up`。
> 想分享给同学用（公网 H5）：照着 [部署上手指南.md](部署上手指南.md) 敲命令即可。

### 给别人用之前：先加一道访问口令（一行环境变量）

默认**不设口令**（本地自己用零干扰）。一旦要挂到公网，**务必**设上，
否则网址被转发一次就等于对外开放：

```bash
# Linux / macOS / 容器
export ACCESS_CODE='你们内部约定的那串口令'

# Windows PowerShell
$env:ACCESS_CODE = '你们内部约定的那串口令'
```

设了之后，未登录访客只会看到一条口令输入页；输对一次，30 天内免再输。
改口令会让所有旧登录态立即失效。**只防"网址被转发"，不防有备而来的攻击**——
安全边界详见 [部署上手指南.md](部署上手指南.md) §10。

### 配置 API Key（完全可选）

**不配也能用**——统计分析、图表、数据体检、Word 导出都是纯 Python。

要启用 AI 深度解读 / 论文审计，复制 `.env.example` 为 `.env` 并填入 Key：

```bash
cp .env.example .env
```

`.env` 已在 `.gitignore` 中，**绝不会入库**。

**档位**：`LLM_TIER=free`（默认）只调用零成本模型，付费模型自动跳过，免费用户不产生任何
API 费用；设为 `pro` 才启用付费优先链。

**多平台容灾**：支持 7 家平台（智谱 / 硅基流动 / 百炼 / DeepSeek / MaaS / Kimi / MiMo），
任何一个失败自动试用下一个；可自带 Key（BYOK），填了 Key 的平台自动提到链首。

**网页端 BYOK**：页面右上角「⚙️ 模型设置」填自己的 Key（六家都支持），
Key 只存在你自己的浏览器（localStorage），请求时直接交给本机服务。
新用户第一次打开会弹 30 秒上手引导，「📖 使用指南」随时可看——都是大白话。

**Key 安全**：服务端持有的 Key 不会出现在任何返回给浏览器的内容里——前端可见的
`/api/llm_stats` 只显示模型名，报错文案统一经 `secrets_guard` 擦除。

---

## 三、六种用法

### 网页版（推荐）

功能最全：数据体检、12 个统计方法、图表、论文核查、图表 AI 核查、副驾驶、Word 导出。

### 图表 AI 核查（多模态）

论文里那张图，配得上你写的那句结论吗？上传图表截图 + 对应结论句，
多模态模型会**读图**并回答：坐标轴是什么、有没有误差棒、图和结论对不对得上。

**设计上刻意的"不讨好"**：如果结论说"显著差异"但图里没有误差棒 / 星号 / p 值，
模型会返回「信息不足，无法判断」，**而不是顺从地替你确认"显著"**——
统计工具宁可说"看不出来"，也不能给你一个虚假的确认。

图片**不落盘**（只在本请求内以 base64 送到模型），走智谱免费多模态档
`glm-4.6v-flash`（0 元）。无 API Key 时降级为"请手动核对"，不会白屏。

### 安装成桌面应用（PWA，零成本）

网页版本身就是 **PWA**：用 Chrome / Edge 打开后，地址栏右侧会出现「安装」图标，
装完就是一个独立窗口的应用（有图标、有开始菜单项、无浏览器地址栏），**不需要额外打包**。

- 应用图标：`static/icons/` 下的 192/512 双尺寸（含 `maskable` 安全区版本）
- 安装后带三个快捷方式：数据分析 / 论文排查 / 论文副驾驶
- 断网时显示离线页，并明确声明**不会展示缓存的历史统计结果**
- 服务端 worker 显式**永不拦截 `/api/*`** —— 统计工具宁可报错，也不能让用户看到陈旧数字

### 命令行（CLI）

同一套后端，命令行外壳。**结果与网页端逐字节一致**：

```bash
python cli.py methods                                     # 列出全部方法
python cli.py check-data data.csv                         # 数据体检（可选 --format json）
python cli.py analyze data.csv -m independent_t -g gender -v score
python cli.py analyze data.csv -m independent_t -g gender -v score -f json
python cli.py audit paper.pdf data.csv --directive "只看 T 检验"
python cli.py simulate independent_t --seed 42 --n 30     # 生成模拟数据
```

护栏：CLI **默认不写文件、只打印**（`--output` 才落盘），数据全内存不落盘，
学术红线自检同样生效。

`check-data` 可作为 CI 门控：发现高优先级数据问题时返回退出码 1，加 `--exit-zero` 可强制返回 0。

### 部署成 H5（分享链接给同学用）

后端零改动，换个生产 WSGI 服务器即可对外提供 H5 服务：

```bash
# Linux / 容器 / 云服务器
gunicorn --config gunicorn.conf.py wsgi:application

# Windows 服务器
python wsgi.py
```

`wsgi.py` 会按平台自动挑 gunicorn / waitress。关键是 `HOST=0.0.0.0`
（绑 127.0.0.1 时映射出去的端口永远连不上）。

完整步骤（Render / Railway / Docker / Nginx 反代）与上线安全清单见
[docs/DEPLOY_H5.md](docs/DEPLOY_H5.md)。

> ⚠️ **不能纯静态托管**：统计计算在 Python 后端（pandas/numpy/scipy），
> Vercel/Netlify 只能放前端壳子，必须同时有个能跑 Python 的后端。

### 接入小程序 / Uni-app

后端**零改动**即可被微信小程序调用 —— 本后端不使用 cookie 会话
（会话靠服务端 `file_id`），而 `wx.request` 也不走浏览器的同源策略。
接口参考见 [docs/API.md](docs/API.md)，接入步骤与阻塞项（AppID / 备案 / 开发者工具）
见 [docs/CROSS_PLATFORM.md](docs/CROSS_PLATFORM.md)。

### 免安装桌面版

见 [README-DESKTOP.md](README-DESKTOP.md)。

---

## 四、产品主线：八站流程

```
上传 → ①数据体检 → ②清洗建议 → ③方法推荐 → ④统计计算
     → ⑤人话解读 → ⑥论文核查 → ⑦写作/Word → ⑧答辩准备
```

① ② 是产品入口，③–⑦ 是已有能力。**换一个入口，就换了一个产品**。

---

## 五、设计原则

1. **能不用 LLM 的绝不交给 LLM。** 统计量全部由 numpy/scipy 计算；LLM 只把结果翻译成人话。
   任何 LLM 环节失败都静默降级为规则模板，功能不中断。
2. **可复现优先。** 同一输入必须产出同一结果（曾因此修掉一个 `str.__hash__()` 随机化
   导致的报告行序不稳定问题）。
3. **不越界。** 明确区分「辅助写作」与「学术不端」，见[第九节](#九合规边界重要)。
4. **零构建。** 原生 HTML/JS，`pip install` 之后就能跑，便于分发给不懂技术的同学。
5. **不引 statsmodels。** 所有统计手写实现，换取打包体积、部署依赖与公式可控性；
   代价是必须自己保证数值正确性——所以每个方法都配了对照测试。

---

## 六、项目结构

```
论文排版辅助agent/
├── app.py                    # Flask 入口 + 12 个 run_* 统计纯函数
│                             #   （三条主路径：数据分析 / 论文排查 / 论文副驾驶）
├── cli.py                    # 命令行外壳（analyze / audit / simulate / check-data / methods）
├── wsgi.py                   # 生产/H5 部署入口（按平台自动选 gunicorn / waitress）
├── gunicorn.conf.py          # gunicorn 配置（worker 数、超时、日志）
├── Procfile                  # PaaS 自动识别（Render / Railway）
│
├── methods_registry.py       # 统计方法注册表（方法名唯一真源）
├── methods_graph.py          # 方法知识图谱（DAG + 决策路径）
├── datacheck.py              # 数据体检 12 检测器 + 一键清洗副本（纯本地规则）
│                             #   （含本福特 / 末位偏好取证；GRIM 查均值也在此）
├── grimmer.py                # GRIMMER 检验（查标准差，GRIMMER 的唯一真源）
├── plugin_registry.py        # 可插拔方法市场：扫描 plugins/ + 沙箱执行 + 结果校验
├── plugin_worker.py          #   插件沙箱的子进程入口（python -m plugin_worker）
├── plugins/                  #   插件目录：一个方法 = 一个 .py（内置 TOST 示例）
├── audit.py                  # 论文核查（声称值 vs 实算值）+ 红线引擎
├── audit_chat.py             # 审计对话（LLM 只解释，不计算）
├── multimodal_agent.py       # 图表 AI 核查（多模态读图，图片不落盘）
├── extract_paper.py          # 论文文本提取与结构化解析
├── pipeline.py               # 副驾驶六阶段流水线编排
├── paper_writer.py           # 证据约束写作引擎
├── paper_polisher.py         # 可选 LLM 润色层（污染稿静默回退）
├── export_docx.py            # Markdown → docx
│
├── agents/                   # LLM 层
│   ├── router.py             # 状态 → 模型路由 + 多平台容灾链
│   ├── openai_compat.py      # 各平台 OpenAI 兼容适配器
│   ├── prompts.py            # 冻结前缀 prompt 契约
│   ├── secrets_guard.py      # 错误出口脱敏
│   └── base.py               # Agent 基类
├── security_guard.py         # 应用层限流（内存有界 + 防 XFF 伪造）
├── cross_platform.py         # 跨端适配层（CORS / 预检，默认关闭）
├── access_guard.py           # 访问门禁（ACCESS_CODE 一行环境变量，默认关闭）
├── tone_guide.py             # 语气规约（规则化，只报不改）
│
├── templates/index.html      # 单页应用（原生 JS，无构建）
├── static/                   # PWA 资源
│   ├── manifest.json         #   应用清单（standalone + 快捷方式）
│   ├── sw.js                 #   service worker（永不拦截 /api/*）
│   ├── offline.html          #   离线页
│   ├── favicon.ico           #   站点图标
│   └── icons/                #   192/512 图标（含 maskable 版本）
├── review_share.py           # v2.16 协作审阅：报告 → 分享链接 + 批注（本地落盘）
├── .review_share/            # 分享落盘目录（默认位置，已 gitignore）
├── make_icon.py              # 生成 favicon 与全套 PWA 图标
├── examples/                 # 8 份示例数据与论文
├── docs/ARCHITECTURE.md      # 架构说明（改代码前先读这个）
├── tools/                    # 开发期诊断脚本（需真实 Key，不参与 CI）
│
├── desktop_launcher.py       # 桌面端入口（端口检测 + 拉起 Flask + 开浏览器）
├── desktop.spec              # PyInstaller 配置（★ 新增模块必须回来补一行）
├── build_desktop.py          # 一键打包（含打包前预检，漏模块 → rc=2）
├── deploy/                   # 部署辅助（Nginx 配置模板等）
│
├── scripts/
│   ├── regress_run.py        # 全量回归入口（单元测试 + exe 打包链路验证）
│   └── exe_e2e.py            # 打包链路验证（真启动 exe 走 12 步）
├── *_test.py                 # 24 个回归测试套件
├── requirements.txt
├── .env.example
└── Dockerfile / docker-compose.yml
```

### `examples/` 内置示例

| 文件 | 内容 |
| --- | --- |
| `student_scores.csv` | 性别 + 成绩 + 学习时长 + 焦虑前后测 + 反应时（30 行） |
| `sample_paper.md` | 声称做了独立样本 T 检验的小论文片段 |
| `questionnaire_data.csv` / `questionnaire_paper.md` | 5 题 Likert 量表，报告 Cronbach's α |
| `two_way_data.csv` / `two_way_paper.md` | 2×3 双因素实验（含显著交互） |
| `rm_anova_data.csv` / `rm_anova_paper.md` | 30 人 × 4 时间点重复测量 |
| `sample_regression_data.csv` / `sample_regression_paper.md` | 回归分析示例 |

---

## 七、测试

24 个测试套件（部分需真实 API Key 或 `--live`，默认跳过），覆盖计算正确性、契约一致性、安全边界与前端逻辑。

### 一条命令跑全量回归（推荐）

```bash
.venv/Scripts/python.exe scripts/regress_run.py            # 单元测试 + exe 打包链路验证
.venv/Scripts/python.exe scripts/regress_run.py --fast     # 只跑单元测试（跳过 exe）
.venv/Scripts/python.exe scripts/regress_run.py --only exe # 只验打包链路
```

会汇总到 `_tmp_reg.txt`；有失败时另写 `_regress_<套件名>.log` 存该套件的完整原始输出
（不然你只知道"cli_test 有一项失败"，还得手工重跑一遍才能看到详情）。
退出码 0 全通过 / 1 有失败 / 2 环境未就绪。

**为什么回归里要带上 exe 验证**：单元测试全绿 ≠ exe 能跑。exe 缺模块是"构建期"问题，
任何单测都发现不了 —— 2026-09-11 那次 93.9MB 的包双击闪退，当时单测是全绿的。
所以 `regress_run.py` 默认会真启动 `dist/智论助手.exe` 走完 12 步链路。

**`cli_test` 的一条"只在回归里失败"的假象（已修）**：`cli_test` §9 用子进程交叉验证
报告 md5 的可复现性。原实现起 4 个子进程但**不传 `env`**，于是子进程继承父进程环境：
从回归 runner 里跑时父进程若已设 `PYTHONHASHSEED` 为某个具体值，这 4 个子进程全会拿到同一个值
→ 这段"跨子进程一致"是**假绿**。现改为额外起 4 个**显式抽掉 `PYTHONHASHSEED`** 的子进程（共 8 个），
逼它们用随机 hash，才真正验证了这个断言声称要守的性质。

```bash
# 也可以单独跑某几个套件
export NO_PROXY=127.0.0.1,localhost   # 防代理截获本机请求
python registry_test.py
python security_guard_test.py

# 少数套件需要先启动本地服务（否则返回 rc=2 = 环境未就绪，不是失败）
python app.py                         # 另开一个终端也行
python paper_check_test.py
python methods_test.py
```

几类测试的规模：`simulate`(244) · `review_share`(181) · `datacheck`(115) · `table_check`(111) ·
`registry`(104) · `copilot`(103) · `forensics`(101) · `export_docx`(97) · `plugin_registry`(93) ·
`access_guard`(92) · `rm_anova`(88) · `cli`(69) · `grimmer`(56) · `red_line`(66) · `audit_chat`(56) ·
`security_guard`(56) · `pwa`(57) · `tone_guide`(53) · `desktop_launcher`(48) · `build_desktop`(42) ·
`regression`(39) · `cronbach`(38) · `two_way`(30) · `plugin_worker`(28) · `explain`(27) ·
`datacheck_ui`(25) · `grim`(32) · `secrets_guard`(19)

前端内联 JS 另有 Node 探针（`node _syntaxcheck/*_probe.js`）：
`defense_export`(82) · `image_audit`(47) · `datacheck`(25)。
⚠️ `_syntaxcheck/` 是各会话共享目录，**不要整体清理** —— 2026-09-14 曾被整目录清掉，
`simulate` / `review_share` / `plugin_methods` 三个探针就此丢失（只能重写）。
`node --check` 只验语法，探针用 DOM mock 把 IIFE 抽出来**真跑一遍**，验的是契约
（该发什么请求、守门有没有生效、失败后按钮有没有复原、后端文本有没有转义）。

> 统计方法都配了**独立 oracle 交叉验证**（如重复测量 ANOVA 同时用定义式与 OLS 两条路径
> 算同一组数，互相印证）。

---

## 八、扩展点（设计已留好接口）

每个分析函数都设计成 **纯函数（输入 DataFrame，返回 dict）**，方便测试和接力。

### ⚠️ 新增一个统计方法要同步改多处

方法清单散落在多个位置，**漏一处就会出现「前端能选但后端不认」**。
完整清单与说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#-新增一个方法要同步改-8-处)，
`registry_test.py` 会校验一致性——改完记得跑它。

| # | 文件 | 改什么 |
| --- | --- | --- |
| 1 | `methods_registry.py` | 注册 `MethodSpec`（**真源**） |
| 2 | `templates/index.html` | 方法下拉框加 `<optgroup>`；`validateMethod` / `methodDesc` 同步 |
| 3 | `extract_paper.py` | `_METHOD_PATTERNS`（论文方法名识别） |
| 4 | `audit.py` | `_METHOD_ALIASES`（论文措辞 → 方法 key） |
| 5 | `audit.py` | `_METHOD_PRIORITY`（重跑优先级） |
| 6 | `paper_writer.py` | `_method_label`（报告正文中文名） |
| 7 | `app.py` | `_dispatch_analysis`（JSON / SSE / copilot 三路共用入口） |
| 8 | `methods_graph.py` | 知识图谱节点与决策路径 |

### 其它任务的落点

| 任务 | 在哪改 |
| --- | --- |
| 加图表 | `app.py` 新增 `/api/chart`；前端 `<img>` 渲染 |
| 加流式输出 | `api_analyze` 改成 SSE，前端用 `EventSource` 接（已实装 v0.8） |
| 加 Word 导出 | 新增 `/api/export`，用 python-docx（已实装 v0.9） |
| 论文变量同义词扩展 | `audit.py` 的 `_VAR_SYNONYMS` |
| 改进建议模板扩展 | `audit.py` 的 `_generate_suggestions` |
| 指令过滤扩展 | `audit.py` 的 `_METHOD_ALIASES` / `_VAR_FILTER_ALIASES` |
| 接新的 LLM 平台 | `agents/` 加子智能体类（`complete(prompt) -> str`），`router.py` 的 `STATE_TO_MODEL` 加候选 |

---

## 九、合规边界（重要）

本工具**辅助**学术写作，**不参与**学术不端。内置红线引擎会在指令层面拦截：

| 类别 | 处理 |
| --- | --- |
| 代写 / 枪手、买卖论文、规避查重或 AI 检测、伪造篡改数据 | **无条件拦截，不受任何豁免** |
| 润色你已写好的文字（表达、结构、语病） | 允许 |

**本工具能做的**：用你自己的真实数据跑统计；核查论文里的统计量是否对得上；
指出方法误用与报告缺项；润色你已经写好的文字。

**本工具不会做的**：代写论文、买卖论文、规避查重或 AI 检测、伪造篡改数据。

正确用法：先在「数据分析」上传你的数据跑出结果，再用「论文副驾驶」基于这些真实结果
生成初稿，最后由你自己改写、补充并署名。

> 这不是「AI 检测规避工具」。语气规约（`tone_guide.py`）的目标是让人读着顺，
> 不是规避检测——代码里有测试专门断言这一点。

### 数据安全

- ✅ 默认只监听 `127.0.0.1`，不上公网就能避免被刷。
- ✅ 上传的文件**只存在内存**，分析完不落盘、不持久化。
- ✅ 应用层限流：普通接口与 AI 接口分桶计数，内存有界；仅在可信代理后才采信
  `X-Forwarded-For`（默认关，防伪造绕过）。
- ✅ 错误出口统一 JSON，不外漏堆栈信息。

---

## 十、文档

| 文档 | 内容 |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、方法注册表的同步点、LLM 缓存纪律、测试策略 |
| [CHANGELOG.md](CHANGELOG.md) | 版本更新日志 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南（开发环境、硬性纪律、验证流程） |
| [README-DESKTOP.md](README-DESKTOP.md) | 桌面版打包与使用 |
| [docs/DEPLOY_H5.md](docs/DEPLOY_H5.md) | H5 部署指南（生产服务器、环境变量、上线安全清单） |
| [docs/API.md](docs/API.md) | HTTP API 参考（24 个路由、错误码、调用示例） |
| [docs/CROSS_PLATFORM.md](docs/CROSS_PLATFORM.md) | 小程序 / Uni-app 接入指南（含阻塞项） |
| [tools/README.md](tools/README.md) | 开发期诊断脚本说明 |

> 产品规划（ROADMAP）与产品主线总纲属内部文档，未随仓库公开。

---

## 十一、限制

- **仅本地内测**：默认只监听 `127.0.0.1`。若要公网部署，请先读
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 的安全层章节（限流、代理信任、HTTPS）。
- **扫描件 PDF 不支持**：图片型 PDF 无法提取文本（未做 OCR），会明确报错。
- **论文排查依赖规则引擎**识别统计方法，生僻表述可能识别不到。
- **编码识别**：CSV 优先 utf-8 / utf-8-sig / gbk；其它编码可能报错。
- **上限 20MB**（`MAX_UPLOAD_MB`）。
- **浏览器需支持 ES6 与 Fetch**（Chrome / Edge / Firefox 现代版本均可）。
- **双因素 ANOVA 不做事后多重比较**，交互显著时在报告中提示改做简单效应分析。
- **重复测量 ANOVA 只支持单因素被试内设计**（同一批被试多时间点）；含组间因素的设计
  （如实验组 vs 对照组 × 时间）需改用混合设计 ANOVA，暂未实装。
- **重复测量 ANOVA 要求完整案例**：任一时间点缺失的被试会被整行剔除，报告中的 n
  为剔除后的样本量。
- **AI 功能依赖外部平台**：模型可用性与价格是时效信息，配 Key 前请以平台公示为准。
- **不做 3D / 视频 / 图像生成**：本工具聚焦统计与学术写作。
- 跑依赖服务器的测试脚本时，需先启动本地服务，并确保
  `NO_PROXY=127.0.0.1,localhost`（环境里若设了 `HTTP_PROXY` 会拦截本机请求）。

---

## 十二、许可证

本项目采用 [MIT License](LICENSE) 开源，可自由使用、修改、分发。
