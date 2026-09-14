# 架构说明 · 智论助手

本文档面向要读代码 / 改代码的人。产品定位与使用方式见根目录 `README.md`。

---

## 一、总体结构：一个 Flask 进程，三个 Tab

```
浏览器（templates/index.html 单页，原生 JS，无构建步骤）
        │  fetch / SSE
        ▼
   app.py（Flask，唯一入口）
        │
        ├── 纯函数计算层 ── methods_registry.py → run_*.py（numpy/scipy）
        ├── 规则层 ──────── audit.py / datacheck.py / extract_paper.py / tone_guide.py
        ├── LLM 层 ──────── agents/router.py → agents/openai_compat.py（多平台容灾）
        └── 安全层 ──────── security_guard.py（限流）/ secrets_guard.py（脱敏）
```

设计原则：**能不用 LLM 的绝不交给 LLM**。统计量全部由 numpy/scipy 计算，LLM 只负责把
已算好的结果翻译成人话；任何 LLM 环节失败都静默降级为规则模板，功能不中断。

---

## 二、计算层：方法与注册表（单一真源）

### `methods_registry.py` —— 方法名唯一真源

`MethodSpec(key, fn, alias, label, picker_label, fields, error_hint)` 描述一个方法；
`call_method(key, df, payload)` 负责「查表 → 用 `_resolve` 解析字段 → 调纯函数」。

每个 `run_*` 都是**可独立调用的纯函数**：

```python
run_independent_t(df, group_col, value_col) -> {
    "method": "independent_t",
    "summary": {...},        # 结构化统计量（机器可读）
    "markdown": "...",       # 学术解读（人可读）
    "variables": {...},
    "chart_spec": {...},     # 可选，前端画图用
}
```

### ⚠️ 新增一个方法要同步改 8 处

注册表不是「加一行就完事」。同一份方法清单散落在 8 个位置，漏一处就会出现
「前端能选但后端不认」这类不一致：

| # | 位置 | 作用 |
| --- | --- | --- |
| 1 | `methods_registry.py` | 注册 `MethodSpec`（真源） |
| 2 | 前端 `templates/index.html` 的 `<optgroup>` | 下拉框可见 |
| 3 | `extract_paper.py` 的 `_METHOD_PATTERNS` | 论文里的方法名识别 |
| 4 | `audit.py` 的 `_METHOD_ALIASES` | 论文措辞 → 方法 key |
| 5 | `audit.py` 的 `_METHOD_PRIORITY` | 多方法命中时的重跑优先级 |
| 6 | `paper_writer.py` 的 `_method_label` | 报告正文里的方法中文名 |
| 7 | `app.py` 的 `_dispatch_analysis` | JSON / SSE / copilot 三路共用入口 |
| 8 | `methods_graph.py` | 知识图谱节点与决策路径 |

`registry_test.py` 会校验这些清单是否一致——改完记得跑它。

### `methods_graph.py` —— 决策图谱是 DAG，不是树

`q_ngroups`（分组数）节点有**两个父节点**，所以高亮路径必须用 **BFS 最短路径**从根推进，
不能用「单父节点回溯」，否则会漏掉一条合法路径。

---

## 三、规则层

### `extract_paper.py` —— 论文文本 → 结构化信息

`read_paper_text(FileStorage)` 支持 `.docx / .pdf / .txt / .md`；PDF 走 pypdf，
并额外处理知网/万方下载的 AES「限制编辑」加密（空密码可解）。扫描件无法提取时明确报错
而不是返回空串。

三个抽取器：`extract_methods` / `extract_quantities` / `extract_variables`。

> ⚠️ **不要用 `str.__hash__()` 当排序键**。Python 的字符串 hash 受 `PYTHONHASHSEED`
> 随机化影响，跨进程结果不同。`extract_quantities` 曾因此让同一篇论文的报告行序
> 每次运行都变（对「可复现核查」是致命的）。现在按**匹配位置**排序。

### `audit.py` —— 论文声称 vs 实际重跑

流程：抽取声称值 → 匹配论文变量到数据列 → 用真实数据重跑 → 逐项比对 → 生成建议。

- `_compare_quantity` 产出 `status ∈ {ok, minor_diff, mismatch, no_real, unknown}`。
- `_attach_comparison_summaries` 给每条比对挂 `summary`（含零 LLM 的 `verdict` 人话结论
  与 `hint` 排查方向），供前端「审计对话」使用。
- `suggestions` 是 `list[str]`（兼容旧前端），`explanations` 是并行的结构化卡片（XAI）。
- 解释规则**不做过度推断**：列名不是研究设计证据，小样本也不等于「数据非正态」。

### `plugin_registry.py` / `plugin_worker.py` —— 可插拔方法市场（v2.14）

一个方法 = `plugins/` 里的一个 .py（暴露 `SCHEMA` + `run(df, **kw)`）。三道护栏：
**契约校验**（坏插件只记原因，不拖垮市场）→ **沙箱执行**（子进程、超时即杀）
→ **结果合理性校验**（p∈[0,1]、df>0、n≥2，不通过绝不进报告；**会递归进 `summary`**，
因为本项目结果约定是 `{"summary": {"p": ...}}`）。

- **插件绝不并进 `methods_registry`**。那张表是**内置方法**真源，registry_test 断言
  前端下拉 / 副驾驶 / `extract_paper` 识别层 / `audit` 别名 / 方法图谱全部覆盖它
  ——插件一进来实测崩 12 项。插件走 `app._run_plugin_method` 的兜底分发
  （注册表抛 MissingField 后才查市场），因此**冒名插件永远劫持不了内置方法**。
- **沙箱用 `python -m plugin_worker` 而非 `multiprocessing`**：后者在 Windows 上是 spawn，
  会**重新 import `__main__`**；本项目测试脚本是模块级直接跑断言的风格，
  子进程重跑主模块会递归派生进程。独立解释器起 worker 不碰 `__main__`。
- 冻结环境（PyInstaller）下 `sys.executable` 不是解释器 → 降级为进程内执行，
  并通过 `/api/plugins` 的 `sandbox` 字段**如实标注**，不静默。
- **v2.20：stdout 是协议的，不是插件的**。父进程 `pickle.loads(proc.stdout)`，
  插件往 stdout 写的任何东西都会混进 pickle 流。不 flush 的小 `print` 会**侥幸**
  通过（文本层缓冲到进程退出才 flush，落在 pickle 之后被 `loads` 忽略），但
  `print(..., flush=True)` / 输出超过 8KB 缓冲区 / `PYTHONUNBUFFERED=1` 必炸，
  报的还是"无法解析的结果"，而 stderr 为空 —— 用户看不出是自己多打了一行调试 print。
  故插件执行期间 `sys.stdout` 改道 `sys.stderr`，结束后还原；结果先落内存缓冲
  再整体写出（不可 pickle 的结果不会留下半截 pickle）；插件同目录加入
  `sys.path`（**append 不 insert**，免得插件自带的 `numpy.py` 顶掉标准库）。

### `datacheck.py` —— 数据体检（产品入口）

**12 个检测器** + `run_datacheck()` + `grim_check()`，纯本地规则、零 LLM。
`propose_fix(df, report)` 生成清洗副本，**绝不修改传入的 df**，且只自动修「语义无歧义」
的两类（重复行、反向题漏反向计分）；合计≠分项这类无法判断谁错的只标记不改。

最后两个检测器是**学术级取证**（v2.13），误报是它们的生命线，改之前务必先读
`forensics_test.py` 里的双边断言：

| 检测器 | 判定 | 防误报的关键闸门 |
| --- | --- | --- |
| `check_benford` | **MAD > 0.015**（不用卡方：n 大时卡方几乎必然显著，是误报机器） | 列名含编号/年月/量表/百分比/价格→跳过；全正；n ≥ 100；跨 ≥ 2 个数量级；唯一值 ≥ 50 |
| `check_terminal_digits` | 卡方 p < 0.001 **且**某末位占比 ≥ 25%（双门槛缺一不可） | 量程 ≥ 10；唯一值 ≥ 5；编号类列跳过 |

> **GRIMMER 不在本模块**。唯一真源是 `grimmer.py`（rsprite2 口径、`Fraction` 精确算术）；
> `datacheck` 只保留查均值的 `grim_check()`。两处各写一份会导致口径分裂——
> 本模块曾有一份重复实现，已删除。

> **别把"唯一值数量"当末位偏好的高门槛**：取整本身就会压低唯一值数量，
> 用它当门槛正好把要检测的 heaping 现象过滤掉（历史 bug，已改为量程 + 下限 5）。

### 红线引擎（`audit._red_line_scan`）

- `core` 类（代写 / 买卖 / 规避查重 / 伪造数据）**无条件拦截**，不受任何白名单豁免。
- `writing` 类在用户有明确润色意图时可豁免。
- 后端在 `/api/check_paper` 拦 `directive`，前端在 `/api/red_line_scan` 提示；
  `/api/audit_chat` 的**提问本身**、`/api/audit_image` 的**结论句**也各过一遍，
  防止借其它入口绕过。

> 这是**合规边界**，不是「AI 检测规避工具」。改这块前先读 `tone_guide.py` 的模块文档。

### `multimodal_agent.py` —— 图表 AI 核查（v2.9）

上传统计图表 + 论文里的一句结论，多模态模型读图并判断「图配得上结论吗」。

- **视觉护栏（本模块最重要的防御）**：`agents/router.py` 用
  `VISION_REQUIRED_STATES` + `VISION_MODELS` 声明「哪些状态必须看图、哪些模型能看图」。
  容灾链上的纯文本模型会被**直接跳过**——因为「图片被静默忽略、模型凭空描述图表」
  比直接报错**危险得多**：用户会以为模型真的看过图了。
- **图片不落盘**：以 base64 一次性内联送进请求体，不写 `uploads/`、不进日志；
  缓存 key 用**图片内容的 sha256**，缓存里**只存模型输出的文本**。
- **判定三态**：`matches_conclusion ∈ {true, false, null}`。`null` 表示「图信息不足」，
  不是失败。prompt 明确要求「看不出显著性 ≠ 显著」——图中无误差棒 / 星号 / p 值时
  不得宣称显著（**已用真实 Key 实测**：模型对无误差棒的图返回 `null`，未顺从结论）。
- **宽容解析 + 不臆断**：模型输出常带 ```json 围栏或前后解释文字；解析失败时
  `matches_conclusion` 置 `null`（绝不当成「符合」），附原文供人工复核。

---

## 四、LLM 层

### `agents/router.py` —— 多平台容灾

`STATE_TO_MODEL: {状态 → [(provider, 模型友好名, 温度), ...]}`，列表顺序即优先级，
第一个失败自动试用下一个。平台共 7 家：`sf / zhipu / deepseek / dashscope / maas / kimi / mimo`。

- `free` / `pro` 两档；`FREE_MODELS` 是独立的免费白名单，默认 `free`。
- Key 从 `PROVIDER_REGISTRY[platform].env_var` 取，**不要自拼变量名**。
- BYOK：`set_user_key()` 注入构造时参数，不改全局状态。
- `maas` 是私有百炼端点的尾位兜底；`kimi` / `mimo` 是 v1.8 新增的**付费 BYOK 尾部备胎**
  （故意排在免费档之后，免费用户零感知）。

### 缓存与版本纪律

```python
PROMPT_VERSION = "v3"   # 冻结前缀改动必须 bump，否则旧缓存会被误用
cache_key = hash(model + PROMPT_VERSION + method + summary_canonical_json)
```

- `_sanitize` 把浮点截到 4 位、极小值转 `"<0.0001"`，保证同一结果缓存键稳定。
- 结果 LRU + provider 前缀缓存；**按实际成功模型记名**（容灾切换后不能记成原模型）。
- 输入最小化：只送必要字段，**绝不用 `**summary` 展开**（防止把 df / 原始数据带进 prompt）。

---

## 五、安全层

### `security_guard.py`（应用层限流）

- 滑动窗口 + **内存有界**（空闲 TTL 回收 + `max_ips` 淘汰），避免慢性内存泄漏。
- `client_ip()` **仅在 `TRUST_PROXY=1` 时采信 `X-Forwarded-For`**（默认关，防伪造换 IP）。
- LLM 接口（真花钱）与普通接口**分桶计数**，阈值分别由
  `RATE_LIMIT_LLM_PER_MIN`(8) / `RATE_LIMIT_PER_MIN`(60) 控制。
- 非法值 / 0 / 负数一律回退默认（0 会让服务不可用）。
- 限流器自身异常时**放行**：宁可漏限，不可挂站。
- 本地测试可用 `RATE_LIMIT_DISABLE=1` 关闭——**只在需要的脚本内设**。

### `secrets_guard.py`

在 `AgentError` 与浏览器错误出口统一脱敏，测试只能使用合成 Key。

### `cross_platform.py`（跨端适配，默认关闭）

手写 CORS，不引 `flask-cors`（保持零构建、开箱即跑）。设计要点：

- **默认全关**：不配 `CORS_ALLOW_ORIGINS` 时**不发任何 CORS 头**，
  浏览器同源行为与启用前完全一致。这是「不为了新端把老端搞坏」的底线，
  由 `cross_platform_test.py` 锁住。
- **白名单制**，且必带 `Vary: Origin` —— 否则 CDN/代理可能把 A 站的响应
  缓存后发给 B 站（跨站数据串味）。
- **预检显式拒绝**：非白名单来源返回 `403`，而不是静默 `200` 却不带 CORS 头。
  后者前端只能看到一个语焉不详的 CORS 报错，排查成本极高。
- **`*` + 凭据**是危险组合：回显具体来源而非 `*`（浏览器规范要求），
  并在启动时打印告警。
- **预检不计入限流**（`security_guard.is_preflight`）。浏览器在每个跨域真实请求
  前都要先发预检；若预检也计数，用户会被自己的预检耗光额度，
  症状是「跨域调用时好时坏」——本项目实际踩到并修复过。

> **小程序为什么用不到 CORS**：`wx.request / wx.uploadFile` 不是浏览器请求，
> 不走同源策略、不发预检。小程序能直接调本后端的真正原因是
> **后端不使用 cookie 会话**（会话由服务端 `file_id` 承载），
> 因此无 cookie 的调用方天然可用。详见 [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md)。

---

## 五之二、部署层（外壳，不改内核）

本项目刻意把「应用」与「服务器外壳」分开，兑现「后端一次写、外壳 N 次包」：

| 外壳 | 入口 | 服务器 | 说明 |
| --- | --- | --- | --- |
| 本地内测 | `python app.py` | Flask 开发服务器 | 单进程单线程，方便看错误，**仅限本机** |
| CLI | `python cli.py` | 无 | 直接调纯函数，不走 HTTP |
| H5 / 容器 | `gunicorn --config gunicorn.conf.py wsgi:application` | gunicorn | Linux/macOS/容器 |
| H5（Windows 服务器） | `python wsgi.py` | waitress | gunicorn 不支持 Windows |
| 桌面 | PyInstaller 产物 | 同上（内嵌） | 见 `README-DESKTOP.md` |

关键设计点：

- **`wsgi.py` import 时不加载 `.env`**（与 `app.py` 一致）。凭据只从**真实环境变量**进来，
  这是 12-factor 做法，也避免把本机凭据打进镜像。
- **`HOST` 默认 `0.0.0.0`（仅在 `wsgi.py`）**：对外服务绑 `127.0.0.1`
  会让容器/PaaS 的端口映射彻底失效，是部署头号坑。`app.py` 本地直启仍默认
  `127.0.0.1`，避免无意中把内测服务暴露到局域网。
- **`gthread` worker 而非 `sync`**：`sync` 一个 worker 同时只能处理一个请求，
  而本项目有 LLM 解读这种长耗时 IO，会堵死其他用户。
- **超时设为 120s**：大样本 ANOVA 与 LLM 解读都可能跑过 gunicorn 默认的 30s。
- **`TRUST_PROXY` 与 `FORWARDED_ALLOW_IPS`**：两处都不无条件信任转发头，
  与 `security_guard` 的 XFF 防伪造策略一致——置身后端代理才开。
- **退出码约定**：`0` 通过 / `1` 真失败 / `2` 环境未就绪（如没起服务）。
  让批量回归能区分「代码坏了」与「环境没准备好」。

部署细节与上线安全清单见 [`DEPLOY_H5.md`](DEPLOY_H5.md)。

> ⚠️ 本项目**不能纯静态托管**：统计计算在 Python 端（pandas/numpy/scipy），
> Vercel/Netlify 之类只能放前端壳子，必须同时有一个能跑 Python 的后端。

---

## 五之二、`simulate.py` —— 模拟数据生成器（v2.17）

纯 numpy/scipy，12 个生成器，**不 import `app`**（`app` 需要 import 它 → 循环依赖
会直接炸）。每个 `generate_*` 返回 `(df, truth)`。

`truth` 是本模块的价值所在，分两类，别混用：

| 字段 | 性质 | 怎么用 |
| --- | --- | --- |
| `expected_*` | **可精确比对的 oracle** | 与 `run_*` 的输出逐位对得上（测试对 12 方法 × 3 seed 验过） |
| `true_beta`（logistic） | 生成参数 | 估计值只会落在它附近，**不是 oracle**，抽样变异决定偏差 |

三条不变量：

* **真值必须与被测量同口径** —— `expected_d` 曾因用 `(g2−g1)/pooled` 而与
  `run_independent_t` 的 `d` 反号，拿它当标准答案会误判工具算错符号；
* **真值不许美化** —— `cronbach_alpha` 曾 `np.clip(alpha, 0, 1)`，而 α 可以为负
  （题目间负相关，实测 −4.86），clip 后的"真值"会说 0；
* **不许产出 NaN** —— NaN 不在 JSON 标准里，`jsonify` 会把它序列化成非法 JSON。
  `generate()` 对 `n<3` / NaN / Inf 明确抛 `ValueError`，`/api/simulate` 另有
  `_clamp_num` 钳制，并把被改动的入参放进 `adjusted` 回报（静默改数最难排查）。

> 坑：`_add_noise` 按 `values.mean()` 缩放。带基数（50 分量表）的生成器必须把
> 噪声加在**离差**上再抬回基数 —— 否则 `noise=0.1` 就是 sd≈5.5，效应整个被淹没
> （`two_way_anova` 曾因此 20 个 seed 只中 5 个显著）。

---

## 五之三、`review_share.py` —— 协作审阅（v2.16）

核查报告 → `/s/<token>` 分享页 + 批注。三条不变量（测试逐条守着）：

* **报告只读**：批注只追加/删除，绝不改 `markdown`/`comparisons`——改了就等于
  提供「改完说是导师意见」的造假通道；
* **非法 token 一律 404 不是 500**：URL 是用户可编辑的，路由层已为此踩过一次；
* **无 XSS**：分享页不含 JS，CSP `default-src \'none\'`，内容一律 `html.escape`，
  正文进 `<pre>` 不做 markdown → HTML 转换。

落盘目录 `.review_share/`（`REVIEW_SHARE_DIR` 可改/可 `off`）。
`/s/` 对访问门禁放行（分享就是给没口令的人看），但批注走页面表单，
`/api/review/*` 仍在门禁内——包括管理接口 `GET /api/review/list`（只回元信息，
不含正文）与 `POST /api/review/gc`（只清过期）。

两个内容来源：**论文排查报告**（`kind='audit'`，带 `comparisons`）与
**数据体检报告**（`kind='datacheck'`，只带 `suggestions`）。体检的 markdown
由后端 `datacheck.render_markdown()` 统一渲染后随 `/api/datacheck` 返回，
前端不另写渲染器——页面 / CLI `check-data` / 分享页三处保持一种说法。

---

## 五之四、`export_docx.py` —— Word 导出（v2.18）

纯函数 `markdown_to_docx(markdown, *, chart_png, method_label, meta, title) -> bytes`：
报告 markdown → docx 字节流，被 `/api/export` 与论文排查报告导出共用。

三条不变量（测试逐条守着）：

* **绝不静默丢表格数据**：分隔行只在「紧邻表头 + 每格只含 `-:` + 至少一格 ≥3 字符」
  时成立，非首位的 `---` 一律当数据；列数取表头与所有数据行的**最大列数**，
  长行扩列、短行补空。docx 看着正常却少一行，是最坏的失败方式。
* **标题 `#`~`######` 全识别**：`##`→H1 / `###`→H2 是历史映射（**保留不动**，
  否则已有报告的观感会变），`####`→H3 … `######`→H4。
* **入参宽容**：`markdown` 接受 `None` / `bytes` / 非字符串，`title` / `meta` 同理 ——
  纯函数不该因为调用方传了个 `None` 就甩栈。

拆行不用 `strip('|')`（会连剥多个首尾管道符、吃掉行首/行尾空单元格），改为各剥一个。
图表嵌入失败降级成「（图表嵌入失败）」文字，不阻断导出。

---

## 五之五、`desktop_launcher.py` —— 桌面启动器（v2.19）

双击图标后的第一件事：判断 5000 端口上**是不是自己人**，是就复用并开浏览器，
否则顺延端口起新实例。三条要点：

* **回环请求强制不走代理**：`_http_get` 挂空 `ProxyHandler`。开了代理工具时
  `urlopen` 会把 127.0.0.1 也发给代理，"已有实例" 就永远检测不到 → 起了第二个
  （端口还不一样，用户以为刚才的数据丢了）。
* **身份判定用 JSON 不用子串**：`/health` 必须是合法 JSON、`ok` 为真，且
  `service` 标记（若有）等于 `zhilun-assistant`。**缺字段仍算自家（老版本），
  字段不对才算别人** —— 5000 是 Flask 默认端口，撞车概率不低。
* **报错路径不二次崩溃**：`_wait_for_enter()` 吞 `EOFError` / `OSError`
  （`--windowed` 打包 / CI 没有 stdin）；`app.run` 显式 `use_reloader=False`
  并捕获 `OSError`（检测端口与实际绑定之间有时间差，被抢了给可读提示而非 traceback）。

---

## 六、测试策略

57 个 `*_test.py`，分三类：

| 类型 | 例子 | 需要什么 |
| --- | --- | --- |
| 纯单测（多数） | `registry_test` / `security_guard_test` / `review_share_test` / `simulate_test` | 无，`test_client` 即可 |
| 需活服务 | `smoke_test` / `methods_test` / `paper_check_test` / `regression_audit_test` | 本机 5000 端口（未就绪时退出码 2） |
| 真调 LLM | `llm_cache_test` / `prefix_cache_test` / `zhipu_cache_test` | 真实 Key，较慢 |

> `multimodal_test.py` 默认 **mock 掉 LLM 不发网络**（CI 路径），
> 加 `--live` 才真调多模态模型；`kimi_mimo_live_test.py` 同样需 `--live`。

### 环境注意

```bash
export NO_PROXY=127.0.0.1,localhost   # 防代理截获本机请求
```

起服务供测试时必须 `debug=False, use_reloader=False`：Flask 的 reloader 子进程
会随父 shell 退出被回收，表现为「curl 通了，接着就 WinError 10061」。
也不要手工设 `WERKZEUG_RUN_MAIN=true`（会触发 `KeyError: 'WERKZEUG_SERVER_FD'`）。

### 改代码时的经验规则

1. **断言要测契约，不要冻结快照。** 「平台集合恰好是这 5 个」「PROMPT_VERSION == "v2"」
   这类断言会在功能正常演进时误报（已在平台 roster、PROMPT_VERSION、Key 隔离清单上
   各踩过一次）。
2. **隔离 Key 时从 `PROVIDER_REGISTRY` 派生清单**，不要硬编码——新增平台不会漏。
3. **判失败要保留原始退出码与完整日志**，分清 PASS / SKIP / FAIL，
   不能因为复跑通过就认定根因已解决。
4. 前端内联 JS 的语法用 `node --check`，**逻辑正确性**要另写 DOM mock 探针
   （见 `_syntaxcheck/`）。`_syntaxcheck/` 是各会话共有目录，**不要整体删除**。
5. **导出 / 下载路径的两条硬契约**（v2.23 用血泪换来的）：
   - 文件名必须优先取 RFC 5987 的 `filename*`。Werkzeug 对非 ASCII `download_name`
     会给两个参数，`filename=` 里中文被**整段删掉**，只剩 `_independent_t_123.docx`。
   - Blob 下载的 `<a>` 必须 `document.body.appendChild(a)` 再 `click()`，
     不挂到文档上的锚点在 Firefox 里点了没反应。

---

## 七、为什么不用 statsmodels

所有统计（含 Type III 平方和、Mauchly 球形度、Greenhouse-Geisser 校正、IRLS 逻辑回归）
都用 numpy/scipy 手写实现。原因：打包体积、部署依赖、以及对每个公式的可控性。
代价是必须自己保证数值正确性——所以每个方法都配了对照测试。
