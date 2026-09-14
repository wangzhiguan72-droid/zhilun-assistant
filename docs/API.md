# HTTP API 参考

面向要对接本后端的调用方（微信小程序 / Uni-app / 第三方网页 / 自建前端）。

> 本文件由实际路由表整理，请以代码为准。改接口后请同步更新此处。

## 通用约定

- **基地址**：本地 `http://127.0.0.1:5000`；对外需 HTTPS（微信小程序强制要求）。
- **响应格式**：统一 JSON。**成功**为 `{"ok": true, ...}`；
  **失败**为 `{"ok": false, "error": "<人话说明>"}`，并带合适的 HTTP 状态码。
  （404 / 405 / 413 / 500 也统一返回 JSON，不会吐 HTML 堆栈。）
- **会话模型**：**不使用 cookie**。上传返回 `file_id`，后续接口带着它调用。
  服务端在内存字典 `_SESSION[file_id]` 里持有 DataFrame。
  - 好处：小程序 / H5 / CLI / 桌面共用同一套调用方式，无需各自鉴权适配。
  - ⚠️ 代价：`file_id` 无鉴权，**拿到即可读取该份数据**。不要跨用户分享。
  - 服务重启后 `file_id` 全部失效，需重新上传。
- **限流**：默认普通接口 60 次/分、LLM 接口 8 次/分（按客户端 IP）。
  超限返回 `429` + `Retry-After` 头。**`OPTIONS` 预检不计入限流。**
- **数据不落盘**：原始数据全程内存处理。
  `POST /api/audit_image` 上传的**图片同样不落盘**（仅请求内以 base64 送模型）。

---

## 上传与数据

### `POST /api/upload`

接收数据文件（Excel / CSV），返回列概览与推荐方法。

- **表单字段**：`file`（必需，注意字段名就是 `file`）
- 小程序用 `wx.uploadFile({ name: 'file', ... })`，**返回值需 `JSON.parse`**
- 返回：`file_id`、列概览（类型 / 缺失 / 唯一值 / 描述统计）

### `POST /api/datacheck`

数据体检（产品入口第一站）—— 「数据里有没有错」。

- **JSON**：`{"file_id": "..."}`
- 返回：`issues`（问题清单，带优先级）、`summary`

### `POST /api/datacheck/fix`

生成「清洗后副本」（第二站）。**绝不修改原数据**。

- **JSON**：`{"file_id": "..."}`
- 返回：`actions`（改了什么）、`changes`、`stats`

### `GET /api/sample/<name>` · `GET /api/sample_paper/<name>`

下载内置示例数据 / 示例论文，方便免登录体验。

---

## 统计分析

### `POST /api/analyze`

按方法 + 列跑统计，返回 Markdown（可选带图表与 LLM 解读）。

- **JSON**：
  - `file_id`（必需）
  - `method`（必需，方法名见 `python cli.py methods`）
  - `group_col` / `value_col` / `value_col2`（按方法需要）
    例：T 检验用 `group_col` + `value_col`；相关/卡方/配对用 `value_col` + `value_col2`
  - `stream: 1` → 改为 SSE 流式返回（`stage` / `markdown` / `chart` 事件）
  - `use_llm: 1` → 附带 LLM 深度解读（可选功能，失败静默降级）
  - BYOK：可带用户自带 Key 相关字段
- 返回：`summary`（统计量）、`groups`、`markdown`、可选 `chart`

> `summary` 的字段就是统计量本身（`p` / `t` / `df` / `ci_low` / `d` 等）。
> **同一份数据同一方法，此处结果与 `cli.py analyze` 必须逐字段相同** ——
> 这条契约由 `cli_test.py` 与 `wsgi_test.py` 共同守护。

### `POST /api/chart`

根据已上传数据 + 方法 + 列生成图表 PNG（base64）。

### `POST /api/export`

把分析结果 / 论文排查报告导出为 `.docx`。

- 导出前会做学术红线自检（见下），未通过则拒绝。

---

## 论文排查与副驾驶

### `POST /api/check_paper`

同时接收论文 + 数据，核对论文里声称的统计量与实算值是否对得上。

- **表单字段**：`paper`（论文）、`data`（数据）
- 返回：`comparisons`（status / 论文值 / 实算值）、`suggestions`、`explanations`

### `POST /api/red_line_scan`

学术红线自检（前端在提交指令 / 导出前实时校验）。

- **JSON**：`{"text": "..."}`
- 返回：`blocked`（是否拦截）、`hits`（**字符串数组**）、`correct_usage`

### `POST /api/audit_chat`

对**一条**统计比对追问，LLM 只解释不计算。

- **JSON**：`{"summary": {...}, "question": "...", "force": false}`
- 返回：`answer`（Markdown 或 `null`）、`cited`（引用回执）、`llm_model`、
  `llm_cached`、`llm_error`。LLM 不可用时 `answer` 回退到规则引擎文案。

### `POST /api/audit_image`

多模态**图表核查**：上传一张统计图表 + 论文里对应的一句结论，判断图是否支持该结论。

- **multipart/form-data**：

  | 字段 | 必填 | 说明 |
  | --- | --- | --- |
  | `image` | 是 | 图表截图，`.png` / `.jpg` / `.jpeg` / `.webp` / `.gif`，**≤ 5 MB** |
  | `claim` | 否 | 与这张图对应的结论句（≤ 500 字） |
  | `force` | 否 | `1` = 绕过缓存强制真调 |

- 返回字段：

  | 字段 | 类型 | 说明 |
  | --- | --- | --- |
  | `ok` | bool | 是否成功拿到读图结果 |
  | `matches_conclusion` | `true` / `false` / `null` | 图是否支持结论；**`null` = 图信息不足以判断** |
  | `issues` | string[] | 具体问题（如"无误差棒""坐标轴被截断"） |
  | `caption` | string | 模型对图表的客观描述（坐标轴 / 组别 / 趋势） |
  | `raw` | string | 模型原始输出，便于人工复核 |
  | `llm_model` / `llm_cached` / `llm_error` | - | 路由到的模型、是否命中缓存、错误说明 |
  | `fallback` | bool | `true` = 无 AI 结果，请手动核对 |

- **降级契约**：缺 Key / 模型不可用时返回 **HTTP 200** + `ok:false` + `fallback:true`
  + `hint`（"请手动核对"），**不返回 5xx**，前端不会白屏。
- **隐私**：图片只在本请求内以 base64 内联送到模型，**不落盘**（不写 `uploads/`、
  不进日志）；缓存 key 用图片内容的 sha256，缓存里**只存模型输出的文本**。
- **参考价**：走智谱免费多模态档 `glm-4.6v-flash`（0 元）。

### `GET /api/methods_graph`

方法学知识图谱：决策路径 + 每个方法的前提假设。**只读，无副作用。**

---

## 副驾驶流水线

六阶段：`collect → survey → plan → analyze → review → write`
（外加第 0 关 `datacheck` 门控）。

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/copilot/phases` | GET/POST | 阶段目录（渲染看板用），**只读无副作用** |
| `/api/copilot/status` | POST | 扫描状态：各阶段完成情况 + 下一个待执行阶段（**幂等只读**） |
| `/api/copilot/dispatch` | POST | 生成某阶段的上下文桥接串（只传摘要，不传全文） |
| `/api/copilot/validate` | POST | 校验某阶段产出文件是否齐备 |
| `/api/copilot/mark` | POST | 记录阶段状态（done / in_progress / blocked） |
| `/api/copilot/paper` | POST | 由分析结果生成证据约束的论文初稿（claim 台账 + 图表 + 正文） |
| `/api/copilot/datacheck` | POST | 第 0 关门控：跑体检 → 落盘报告 → 写状态 |

---

## 运维

### `GET /health`

健康检查（Docker healthcheck / 反代探测）。**不触碰任何用户数据。**

- 返回限流可观测统计（`tracked_ips` / `rejects_total`）
- 该路径**不限流**

### `GET /api/llm_stats`

LLM 缓存命中率观测（调试用）。只读统计，**不含缓存内容**。

### `GET /` · `GET /favicon.ico`

单页应用首页与站点图标。

---

## 错误码

| 码 | 含义 |
| --- | --- |
| `200` | 成功 |
| `204` | 预检通过（OPTIONS） |
| `400` | 参数问题（缺字段 / 方法不识别 / 会话过期） |
| `403` | 预检来源不在 CORS 白名单内 |
| `405` | 方法不允许（返回 JSON，不是 HTML） |
| `413` | 上传体积超限 |
| `429` | 触发限流，看 `Retry-After` |
| `500` | 服务内部错误（堆栈不外漏，统一 JSON） |

---

## 调用示例

### 小程序

```js
// 上传（注意 wx.uploadFile 返回字符串，需 JSON.parse）
wx.uploadFile({
  url: `${BASE}/api/upload`, filePath: tmp, name: 'file',
  success(res) {
    const up = JSON.parse(res.data);
    wx.request({
      url: `${BASE}/api/analyze`, method: 'POST',
      header: { 'Content-Type': 'application/json' },
      data: { file_id: up.file_id, method: 'independent_t',
              group_col: 'gender', value_col: 'score' },
      success(r) {
        const s = r.data.summary;
        console.log('p =', s.p, 't =', s.t, 'df =', s.df);
      }
    });
  }
});
```

### curl

```bash
# 上传
curl -s -X POST http://127.0.0.1:5000/api/upload \
  -F "file=@examples/student_scores.csv"
# 用返回的 file_id 分析
curl -s -X POST http://127.0.0.1:5000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"file_id":"<上一步的 file_id>","method":"independent_t",
       "group_col":"gender","value_col":"score"}'
```

### CLI（等价路径）

```bash
python cli.py analyze examples/student_scores.csv \
  -m independent_t -g gender -v score -f json
```

三者（网页端 / 小程序 / CLI）结果必须**逐字段一致**。
