# 智论助手 · MVP（最小可内测版）

一个本地运行的数据分析辅助 Web 应用，目标是：
1. 从 Excel/CSV 直接产出可放进论文的 T 检验结果。
2. 上传论文初稿 → 自动识别论文里的统计方法 → 用真实数据反向核查。

---

## 一、当前功能（v0.9）

| 模块 | 状态 |
| --- | --- |
| **【Tab 1：数据分析】** | |
| 拖拽 / 点击上传 .xlsx / .xls / .csv | ✅ |
| 自动读取列、识别连续 / 分类变量 | ✅ |
| **分步引导向导** + 手动切换方法（7 个方法） | ✅ v0.6 / v0.7 |
| **独立样本 T 检验**（方差齐性、Welch 校正、Cohen's d、95% CI） | ✅ |
| **单因素方差分析 ANOVA**（Levene、η²、Bonferroni 事后多重比较） | ✅ v0.3 |
| **Pearson 相关分析**（r、95% CI、t、Cohen 效应量解释） | ✅ v0.3 |
| **卡方检验**（χ²、Cramér's V、期望频数警告） | ✅ v0.3 |
| **配对样本 T 检验**（Cohen's d_z、Shapiro 正态性提示） | ✅ v0.7 |
| **Mann-Whitney U 检验**（非参数 2 组、效应量 r、中位数 + IQR） | ✅ v0.7 |
| **Wilcoxon 符号秩检验**（非参数配对、零差值剔除、效应量 r） | ✅ v0.7 |
| **多元线性回归**（OLS、R²、F 检验、系数 t 检验 + 95% CI） | ✅ v1.0 |
| **二元 Logistic 回归**（IRLS、Wald z、OR 值、McFadden 伪 R²） | ✅ v1.0 |
| **自动生成图表**：7 种方法对应不同图（柱图/散点/箱线/连线） | ✅ v0.5 / v0.7 |
| **流式渲染**：SSE 事件流（阶段消息 + 进度条 + 先文后图），stream=0 保留兼容 | ✅ v0.8 |
| **导出 Word**：标题/表格/列表自动排版 + 图表嵌入 + 封面元信息，宋体+Times New Roman | ✅ v0.9 |
| 输出 Markdown 学术解读（含"可直接引用进论文"的结论段） | ✅ |
| 复制 Markdown 到剪贴板 | ✅ |
| 内置示例数据（30 行学生 + 焦虑前后测 + 反应时） | ✅ |
| **【Tab 2：论文排查】** | |
| 同时上传论文 (.docx / .txt / .md) + 数据 | ✅ |
| 自动识别论文里的统计方法（v0.7 加 Mann-Whitney / Wilcoxon / Kruskal-Wallis）、统计量、变量名 | ✅ |
| 论文变量 ↔ 数据列 自动匹配（含同义词映射） | ✅ |
| 用真实数据重跑一遍，对比论文声称 vs 实际值 | ✅ |
| 规则化生成改进建议（含 7 种方法各自前提假设 + 替代建议） | ✅ |
| 自然语言指令过滤（"只看 T 检验 / p<0.05 / 男组 / 成绩"） | ✅ v0.4 |
| 简单接口限速（每 IP 每分钟 30 次） | ✅ |
| 重复测量 ANOVA / Logistic 回归 / Cronbach's α | ⏸ 后续版本 |
| 付费弹窗 / 人味润色 | ⏸ 后续版本 |

---

## 二、快速开始

> 💡 不想装 Python？用打包好的免安装桌面版，见 [README-DESKTOP.md](README-DESKTOP.md)。

### 1. 安装依赖

```bash
python -m venv .venv

# Windows (PowerShell / Git Bash)
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 启动

```bash
python app.py
```

然后浏览器打开 <http://127.0.0.1:5000>（默认只监听本机）。

---

### 配置 API Key（可选，启用 AI 功能）

不配也能跑（统计分析、图表、Word 导出都是纯 Python）。要启用 AI 深度解读 / 论文审计，在项目根建一个 `.env`（已在 `.gitignore` 里，绝不入库）：

```dotenv
ZHIPU_API_KEY=你的智谱Key
DASHSCOPE_API_KEY=你的百炼Key
DEEPSEEK_API_KEY=你的DeepSeekKey
LLM_TIER=free          # free=免费档（默认）| pro=会员档
```

`python app.py` 直启时会自动加载 `.env`（只影响直启，跑测试不加载，测试环境保持"无 Key"）。

**档位（v0.5.3）**：`LLM_TIER=free`（默认）只调用零成本模型（智谱 `glm-4.7-flash`、百炼 `qwen3.7-flash`、硅基流动 `GLM-Z1-9B`），付费模型（`deepseek-*` / `V4-Pro`）自动跳过；设为 `pro` 才启用付费优先链。免费用户不产生任何 API 费用。详见 `agents/router.py` 模块说明。

## 三、怎么用

### Tab 1：数据分析
1. 打开首页 → 点击/拖拽上传数据文件，或先点底部"下载示例数据"。
2. 上传成功后，自动进入工作台。按左侧的**分步向导**操作：
   - **Step 1**：选「因变量 Y」（必须是连续列）
   - **Step 2**：选「分组 X」（必须是分类列）；不选 = 走相关分析
   - **Step 3**（仅在没选分组时出现）：选第二个数值列（相关分析）
   - **✓ 系统推荐方法**：每选完一步自动显示推荐方法（T/ANOVA/相关）
   - 想做卡方？点 Step 1 下方的「点这里切换到卡方」
3. 点"开始生成分析"，等待约 1-2 秒，结果以 Markdown 渲染，**下方会自动生成对应图表**。
4. 点"复制 Markdown"，可直接粘贴到你的论文初稿里；图表可右键保存到答辩 PPT。

### Tab 2：论文排查
1. 顶部切到「📋 论文排查」tab。
2. 分别上传论文（.docx / .txt / .md）和数据（.xlsx / .csv）。
3. **可选**：在「🎯 自然语言过滤」输入框里写指令，例如：
   - `只看 T 检验` —— 只保留独立样本 T 检验的核查
   - `只看 p<0.05` —— 只看显著结果
   - `只看男组` / `只看女组` —— 只保留某组变量相关的声称
   - `只看 T 检验 且 p<0.05` —— 组合过滤
   - `只看成绩` / `只看学习时长` —— 按变量过滤
4. 点「开始核查」，系统会：
   - 识别论文里写了哪些统计方法（T 检验 / ANOVA / 相关 / 卡方 等）
   - 提取论文里出现的 p 值、t 值、F 值、χ²、相关系数
   - 把论文里的变量名匹配到你的数据列
   - 用真实数据跑一遍 → 对比论文结论 vs 真实数据
   - 在 Markdown 顶部追加「🎯 已应用指令」说明
   - 给出一份 Markdown 格式的核查报告（含改进建议）
5. 点「复制报告」可粘贴到任何地方。

**自带测试用例**：
- 数据：`examples/student_scores.csv`（30 行，性别+成绩+学习时长）
- 论文：`examples/sample_paper.md`（一份声称做了 T 检验的小论文）

---

## 四、项目结构

```
论文排版辅助agent/
├── app.py                  # Flask 后端（两条主路径：数据分析 / 论文排查）
├── extract_paper.py        # 论文文本提取与结构化解析（独立可复用模块）
├── audit.py                # 论文核查与改进建议生成（独立可复用模块）
├── templates/
│   └── index.html          # 单页面应用（含 Tab 切换）
├── examples/
│   ├── student_scores.csv  # 示例数据：性别 + 成绩 + 学习时长
│   └── sample_paper.md     # 示例论文：声称做 T 检验的小论文片段
├── uploads/                # 上传临时目录（每次启动自动创建）
├── .venv/                  # 隔离 Python 环境
├── requirements.txt        # 依赖清单
├── smoke_test.py           # 数据分析端到端冒烟测试
├── paper_check_test.py     # 论文排查端到端冒烟测试
├── methods_test.py         # 7 个统计方法单元测试
├── directive_test.py       # v0.4 论文排查指令过滤测试
├── chart_test.py           # v0.5 图表生成端到端测试
├── wizard_test.py          # v0.6 分步向导契约测试
├── stream_test.py          # v0.8 流式 SSE 渲染测试
├── export_docx.py          # v0.9 markdown → docx 转换器（纯函数）
├── export_test.py          # v0.9 Word 导出端到端测试
└── README.md               # 本文件
```

---

## 五、后续可接力扩展点（设计已留好接口）

每个分析函数都设计成 **纯函数（输入 DataFrame，返回 dict）**，方便测试和接力。

| 任务 | 在哪改 |
| --- | --- |
| 实现配对 T 检验 / 双因素 ANOVA / 回归 | `app.py` 加 `run_*` 函数，`api_analyze` 加分支 |
| 实现非参数方法（Mann-Whitney / Kruskal-Wallis） | 同上套路 |
| 加图表（柱状图 / 散点图 / 折线图） | 后端用 matplotlib 生成 PNG，前端 `<img src="...">` 渲染 |
| 加 Word 导出 | 后端用 python-docx 把 Markdown 转 .docx，暴露新接口 `/api/export` |
| 加人味润色 | 新增 `/api/polish` 接口，前端在结果区加个"开启人味润色"开关 |
| 接入 LLM 写解读 | 在 `api_analyze` 末尾把统计量塞进 Prompt 调用模型 |
| 论文排查加「用户指令输入」 | `audit.py` 新增 `apply_user_directive(...)` 函数，根据用户文本过滤要核查的统计量 / 范围 |
| 论文里识别更多统计方法 | `extract_paper.py` 的 `_METHOD_PATTERNS` 列表加新正则 |
| 论文里识别更多统计量 | `extract_paper.py` 的 `_PATTERNS` 加新模式 |
| 论文变量同义词扩展 | `audit.py` 的 `_VAR_SYNONYMS` 加新映射 |
| 改进建议模板扩展 | `audit.py` 的 `_generate_suggestions` 加新规则 |
| 指令过滤扩展（新方法 / 新变量关键词） | `audit.py` 的 `_METHOD_ALIASES` / `_VAR_FILTER_ALIASES` 加映射 |
| 实现方式 | `audit.py.apply_user_directive(...)` 是纯函数，返回 `{methods, quantities, variables, comparisons, parsed}` |

---

## 六、防刷与数据安全（PRD 提到的）

- ✅ 默认只监听 `127.0.0.1`，不上公网就能避免被刷。
- ✅ 接口每 IP 每分钟最多 30 次（可在 `app.py` 改 `_RATE_LIMIT_PER_MIN`）。
- ✅ 上传的文件**只存在内存**，分析完不落盘、不持久化。

---

## 七、限制

- 论文排查依赖规则引擎识别统计方法，生僻表述可能识别不到。
- 编码识别：CSV 优先 utf-8 / utf-8-sig / gbk；其它编码可能报错。
- 上限 20MB（`MAX_UPLOAD_MB`）。
- 浏览器需支持 ES6 与 Fetch（Chrome / Edge / Firefox 现代版本均可）。

---

## 八、许可证

本项目采用 [MIT License](LICENSE) 开源，可自由使用、修改、分发。
