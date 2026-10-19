# extract_quantities 崩溃修复方案（extract_paper.py:514 / 主仓 495）

状态：**已批准并实施（v2.49，2026-10-20）**。本文档为方案存档；实施结果见 CHANGELOG v2.49
与 `开源赛道/06-佐证材料/匿名汇总/D1b-修复后复验-说明.md`（W34/W69 修复后 200 且抽出 r2 数值）。

## 一、现象与证据

- 触发论文：W34（MIT 2006 硕士，SHA256 `49e8e761…`）、W69（MIT 2017 硕士，SHA256 见最小复现 JSON），两篇不同论文、同一行代码、同一 traceback，复现确认。
- 用户侧表现：`POST /api/check_paper_only` 返回 HTTP 500「服务器内部错误，请稍后重试」，重试永远失败。
- 服务器侧 traceback（原样在案：`06-佐证材料/原始证据/论文复验扩样-2026-10-19/跑批控制台-第二轮-退避重试与W34崩溃实录.log`、`…第三轮….log`）：

```
File "…/extract_paper.py", line 514, in extract_quantities
    val = float(re.search(r"=\s*(-?\d+\.?\d*)", raw).group(1))
AttributeError: 'NoneType' object has no attribute 'group'
```

- 最小复现（补丁抓取正则 None 时的原文，存 `…/论文复验扩样-2026-10-19/514bug-最小复现.json`）：
  - W34 触发片段：`R2= .95`
  - W69 触发片段：`R2=.037`

## 二、根因

两层叠加：

1. **守卫缺失**：`extract_quantities` 的各数值支路里，通用 `else` 支路有 `val_match else None` 守卫，mean/sd/n 支路有 `if mv is None: continue`，**唯独 r2/β/OR 支路直接 `.group(1)`**。
2. **正则盲区**：`=\s*(-?\d+\.?\d*)` 要求等号后以数字开头。APA/心理学惯用的**无前导零小数**（`.95`、`.037`）不匹配；Word 里常见的 **Unicode 负号 `−`**（U+2212，如 `β = −0.31`）同样不匹配。`re.search` 返回 None 后崩溃。

教育学、心理学、医学类论文大量使用这两种写法——这不是边角料，是目标用户的高频格式。

## 三、修复补丁（主仓 `extract_paper.py:493-495`，未应用）

```python
                elif kind in ("r2", "beta", "or"):
                    # v1.0 回归统计量：β / R² / OR
                    # v2.49：兼容 APA 无前导零小数（"R2 = .95"）与 Unicode 负号
                    # （U+2212，"β = −0.31"）；此前 re.search 返回 None 时未加
                    # 守卫直接 .group(1)，两篇真实论文（W34/W69）触发 500。
                    mv = re.search(r"=\s*([-−]?\d*\.?\d+)", raw)
                    if mv is None:
                        continue
                    val = float(mv.group(1).replace("−", "-"))
                    out.append({"kind": kind, "value": val, "raw": raw,
                                "context": _slice_context(text, m)})
                    positions.append(m.start())
```

说明：

- 正则从 `(-?\d+\.?\d*)` 改为 `([-−]?\d*\.?\d+)`：`\d*\.?\d+` 同时覆盖 `95`、`.95`、`0.95`、`−0.31`；`−` 为 U+2212，浮点前归一为 ASCII `-`。
- `if mv is None: continue` 与 mean/sd/n 支路同款守卫——即使未来正则再有盲区，也是**跳过该条**而不是整篇 500。
- 防御性加固（同一次提交，行为不变）：`kind == "r"` 支路（主仓约 484 行）与 `kind == "d"` 支路（约 488 行）的 `re.search(...).group()` 加同款 None 守卫。当前不可达，但属同类地雷。

## 四、回归与验收

1. 新增 `extract_paper_test.py`（仓库测试命名 `*_test.py`，纳入 `scripts/regress_run.py` 全量回归）：
   - `R2= .95` → kind=r2，value=0.95，无异常
   - `R2=.037` → kind=r2，value=0.037
   - `β = −0.31` → kind=beta，value=-0.31
   - `OR = 1.2` → kind=or，value=1.2
   - `R2 = ` （等号后无数字）→ 该条跳过，无异常（守卫路径）
2. 验收标准：全量回归 rc=0（发布门禁不变）；用 W34、W69 两篇真实 PDF 经 `POST /api/check_paper_only` 复跑，预期 2/2 返回 200 且 r2 统计量进入核对（0.95 / 0.037）。
3. 证据收编：复跑结果以追加页形式记入 D1b 说明（「修复后复验」小节），500 记录保留不改写——先失败后修复的过程本身就是证据。

## 五、版本与纪律

- v2.48 快照（`_工作/测试快照/v248-20261004/`）与交付 exe 不动；修复进主仓，随 v2.49 发版。
- 只改 `extract_paper.py` 与 `version.py`（号段 v2.49）；不涉及 `desktop.spec` datas。
- 提交信息只写工程事实；推送权在用户，任何会话不执行 push。
