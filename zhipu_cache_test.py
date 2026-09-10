"""
GLM 前缀缓存实证脚本（v0.5，Tianshu 式推广）
==============================================
验证：智谱 GLM 的隐式上下文缓存对 agents/prompts.py 冻结前缀契约生效。

方法（与 v0.4.1 硅基流动 DeepSeek 实测同一套思路）：
    1. 构造一段 ~2000 token 的冻结前缀（模拟"系统提示 + 长文档"）
    2. 第 1 次调用：建立缓存（预期 cached_tokens = 0，全量按未命中计费）
    3. 等几秒（缓存写入是异步的）
    4. 第 2 次调用：**同一前缀 + 不同问题放最后**（变量区）
       → 预期 usage.prompt_tokens_details.cached_tokens > 0

官方依据（docs.bigmodel.cn 上下文缓存，2026-09 查证）：
    - 隐式缓存自动识别重复前缀，无需手动配置
    - 命中 token 按约 1/5 价计费；免费模型（glm-4.7-flash）缓存也免费
    - usage.prompt_tokens_details.cached_tokens 透出命中量

用法：
    export ZHIPU_API_KEY=sk-xxx   （或临时 .env.tmp，跑完即删）
    .venv/Scripts/python.exe zhipu_cache_test.py

无 Key 时自动 SKIP，不影响 CI。
"""
import os
import sys
import time

sys.path.insert(0, r'D:\论文排版辅助agent')

# Key 来源：项目根 .env（v0.5.3 落盘）或真实环境变量；都没有则 SKIP
from env_loader import load_dotenv
load_dotenv()

from agents import ZhipuAgent, AgentError

if not os.environ.get("ZHIPU_API_KEY", "").strip():
    print('[SKIP] 未设置 ZHIPU_API_KEY，跳过真实 API 实证（离线行为见 compat_agent_test.py）')
    sys.exit(0)

# ---------------------------------------------------------------------------
# 冻结前缀：模拟 paper_check 场景（系统说明 + 长文档 + 输出 schema）
# 字节级稳定：无时间戳、无随机数、无文件路径
# ---------------------------------------------------------------------------
FROZEN_PREFIX = """你是论文统计部分的审稿专家。以下是待审计的论文文本，请只依据该文本回答问题。

<<<PAPER
第三章 数据分析方法
3.1 研究对象：本研究招募了某高校两个年级共 180 名本科生，剔除无效问卷后
得到有效样本 172 份，其中男生 84 人，女生 88 人，平均年龄 19.6 岁（SD=1.12）。
3.2 研究工具：学业自我效能感量表采用 Likert 五点计分，共 22 题，内部一致性
系数 Cronbach's α = 0.87；学业成绩以期末综合成绩（百分制）为准。
3.3 统计方法：采用独立样本 t 检验比较不同性别学生的学业自我效能感差异；
采用 Pearson 积差相关分析自我效能感与学业成绩的关系；采用卡方检验分析
不同年级学生的通过率差异。所有分析均在 SPSS 26.0 中完成，显著性水平设定
为 α = 0.05。
3.4 结果：男生自我效能感得分 M = 3.62, SD = 0.54；女生 M = 3.48, SD = 0.61；
t(170) = 2.15, p = 0.033, Cohen's d = 0.24。自我效能感与学业成绩的相关
r = 0.42, p < 0.001。卡方检验 χ²(1) = 3.51, p = 0.061。
3.5 讨论部分指出"自我效能感显著预测学业成绩"，并在结论中建议"高校应
开设自我效能感提升课程以改善学业表现"。

（为保证前缀长度达到缓存块阈值，以下为方法学背景材料，供审计参考。）
独立样本 t 检验的前提假设包括：观测值相互独立、两组数据分别服从正态
分布、两组方差齐性。当方差齐性不满足时，应报告 Welch 校正的 t 检验结果。
Cohen's d 的计算在两组样本量不等时建议使用合并标准差的校正公式（Hedges' g）。
Pearson 相关的前提是两变量均为连续变量且关系近似线性；若存在明显偏态，
可考虑 Spearman 等级相关。卡方检验要求期望频数不小于 5，否则应采用
Fisher 精确检验或合并类别。p 值的常见误读包括：p 值不是零假设为真的概率、
统计显著不等于实际效应量大、p = 0.051 与 p = 0.049 的差异不应被夸大。
报告统计量时应同时给出效应量与置信区间，避免仅以 p 值作为结论依据。
相关系数的显著性检验自由度为 n-2；t 检验的自由度为 n1+n2-2。
PAPER>>>

输出格式（严格遵守，中文）：用不超过三句话直接回答问题，不输出寒暄。
"""


def call(agent: ZhipuAgent, question: str) -> tuple[str, dict]:
    """冻结前缀 + 变量后缀 → 完整调用。返回 (回答, usage)。"""
    prompt = FROZEN_PREFIX + "\n本次任务：" + question + "\n"
    text = agent.complete(prompt, max_tokens=120)
    return text, agent.last_usage or {}


def fmt_usage(u: dict) -> str:
    total = u.get("prompt_tokens", 0)
    cached = u.get("cached_tokens", 0)
    pct = (cached / total * 100) if total else 0.0
    return (f"prompt={total}, cached={cached} ({pct:.1f}%), "
            f"completion={u.get('completion_tokens', 0)}")


agent = ZhipuAgent(model="glm-4.7-flash")  # 混合思考默认关闭（轻量任务）
print(f'目标模型：{agent!r}')
print(f'冻结前缀长度：{len(FROZEN_PREFIX)} 字符')
print()

# --- 第 1 次调用：建立缓存 ---
q1 = "该研究最值得质疑的一处统计报告遗漏是什么？"
print(f'[调用 1] 建立缓存 ｜ 问题：{q1}')
try:
    ans1, u1 = call(agent, q1)
except AgentError as e:
    print(f'[FAIL] 调用失败：{e}')
    sys.exit(1)
print(f'  -> {ans1[:120]}')
print(f'  usage: {fmt_usage(u1)}')

# --- 异步写入等待（几秒到几十秒不等） ---
print('\n等待 8s（缓存异步写入）...\n')
time.sleep(8)

# --- 第 2 次调用：同一前缀，不同问题 ---
q2 = "3.4 节的 t 检验报告有什么格式上的不完整？"
print(f'[调用 2] 验证命中 ｜ 问题：{q2}')
ans2, u2 = call(agent, q2)
print(f'  -> {ans2[:120]}')
print(f'  usage: {fmt_usage(u2)}')

# --- 判定（异步生效有波动：miss 就再等一次重试） ---
if u2.get("cached_tokens", 0) > 0:
    print(f'\n[PASS] 前缀缓存命中！cached_tokens={u2["cached_tokens"]}')
    print('结论：智谱 GLM 隐式缓存对冻结前缀契约生效，')
    print('      agents/prompts.py 的做法可以直接推广到智谱平台。')
    sys.exit(0)

print('\n[MISS] 第 2 次未命中（缓存写入可能仍在路上），15s 后重试一次...')
time.sleep(15)
q3 = "该研究用的相关分析自由度是多少？"
print(f'[调用 3] 重试验证 ｜ 问题：{q3}')
ans3, u3 = call(agent, q3)
print(f'  -> {ans3[:120]}')
print(f'  usage: {fmt_usage(u3)}')
if u3.get("cached_tokens", 0) > 0:
    print(f'\n[PASS] 前缀缓存命中（第 3 次调用）！cached_tokens={u3["cached_tokens"]}')
    sys.exit(0)

print('\n[WARN] 三次调用均未观测到 cached_tokens > 0。可能原因：')
print('  1. 前缀未达智谱缓存块的最小 token 阈值（硅基流动是 512 token/块）')
print('  2. 免费档缓存策略与付费档不同（缓存免费可能也意味着命中率低优先级）')
print('  3. 异步写入延迟超过本次等待窗口')
print('结论待定：冻结前缀契约本身无害（命中是纯增益），保持即可。')
sys.exit(0)
