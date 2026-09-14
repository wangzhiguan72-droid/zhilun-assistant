"""
前缀缓存真调实证（v0.5.1，Tianshu 式推广）
============================================
验证：冻结前缀不变 + 变量后缀不同时，provider 侧隐式缓存是否命中。

    1. 第 1 次调用：建立缓存（预期 cached_tokens = 0 或很小）
    2. 等几秒（缓存异步写入）
    3. 第 2 次调用：同一冻结前缀 + 不同问题（变量区在最后）
       → 预期 last_usage.cached_tokens > 0（命中率应接近冻结前缀占比）

支持平台（自动按可用 Key 选择，优先 DeepSeek 官方 → 智谱 → 百炼）：
    - DeepSeek 官方：上下文缓存自动生效，命中价约为未命中 3%（最低价差）
    - 智谱 GLM：隐式缓存，命中约 1/5 价（免费模型缓存也免费）
    - 阿里云百炼：Qwen 隐式缓存自动生效（命中约 20% 价）

用法：
    export DEEPSEEK_API_KEY=sk-xxx   （或写 .env.tmp，跑完删）
    .venv/Scripts/python.exe prefix_cache_test.py

无任何 Key 时 SKIP。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Key 来源：项目根 .env（v0.5.3 落盘）或真实环境变量；都没有则 SKIP
from env_loader import load_dotenv
load_dotenv()

from agents import AgentError

# 平台优先级：DeepSeek 官方（价差最大）→ 智谱 → 百炼
_CANDIDATES = [
    ("deepseek", "deepseek-flash",    "DEEPSEEK_API_KEY"),
    ("zhipu",    "glm-4.7-flash",     "ZHIPU_API_KEY"),
    ("dashscope","qwen3.7-flash",     "DASHSCOPE_API_KEY"),
]

# ---------------------------------------------------------------------------
# 冻结前缀：模拟 paper_check 场景（系统说明 + 长文档 + 输出 schema）
# 字节级稳定：无时间戳、无随机数、无文件路径；~2500 token，远超 512 块阈值
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

（方法学背景材料，供审计参考。）
独立样本 t 检验的前提假设包括：观测值相互独立、两组数据分别服从正态
分布、两组方差齐性。当方差齐性不满足时，应报告 Welch 校正的 t 检验结果。
Cohen's d 的计算在两组样本量不等时建议使用合并标准差的校正公式（Hedges' g）。
Pearson 相关的前提是两变量均为连续变量且关系近似线性；若存在明显偏态，
可考虑 Spearman 等级相关。卡方检验要求期望频数不小于 5，否则应采用
Fisher 精确检验或合并类别。p 值的常见误读包括：p 值不是零假设为真的概率、
统计显著不等于实际效应量大、p = 0.051 与 p = 0.049 的差异不应被夸大。
报告统计量时应同时给出效应量与置信区间，避免仅以 p 值作为结论依据。
相关系数的显著性检验自由度为 n-2；t 检验的自由度为 n1+n2-2。
重复测量方差分析适用于同一批被试在多个时间点的测量，需满足球形假设
（Mauchly 检验），不满足时用 Greenhouse-Geisser 校正。多元方差分析
（MANOVA）用于多个因变量同时检验，可控制第一类错误膨胀。效应量的
常用标准：d = 0.2 小效应，0.5 中效应，0.8 大效应；η² = 0.01 / 0.06 / 0.14
分别对应小 / 中 / 大。置信区间越窄说明估计越精确；样本量翻倍，置信
区间宽度约缩小为原来的 1/√2 倍。回归分析中 R² 表示因变量方差被自变量
解释的比例，调整 R² 对加入无关自变量有惩罚；标准化回归系数（Beta）
可用于比较不同量纲自变量的相对重要性。多重共线性用 VIF 诊断，VIF > 10
视为严重。Logistic 回归报告 OR 值及其 95% 置信区间，OR = 1 表示无关联。
PAPER>>>

输出格式（严格遵守，中文）：用不超过三句话直接回答问题，不输出寒暄。
"""


def fmt_usage(u) -> str:
    if not u:
        return "（无 usage）"
    total = u.get("prompt_tokens", 0)
    cached = u.get("cached_tokens", 0)
    pct = (cached / total * 100) if total else 0.0
    return (f"prompt={total}, cached={cached} ({pct:.1f}%), "
            f"completion={u.get('completion_tokens', 0)}")


def main() -> int:
    # 选平台
    chosen = None
    for provider, model, env_var in _CANDIDATES:
        if os.environ.get(env_var, "").strip():
            chosen = (provider, model, env_var)
            break
    if chosen is None:
        print('[SKIP] 未设置任何平台 Key（DEEPSEEK / ZHIPU / DASHSCOPE），跳过真调实证')
        return 0
    provider, model, env_var = chosen
    print(f'[平台] {provider}/{model}（Key: {env_var}）')
    print(f'冻结前缀长度：{len(FROZEN_PREFIX)} 字符')
    print()

    # BYOK：Key 从 env 传给 Agent 构造（绝不写全局）
    from agents.router import get_router
    router = get_router()
    router.set_user_key(provider, os.environ[env_var].strip())

    def call(question: str):
        prompt = FROZEN_PREFIX + "\n本次任务：" + question + "\n"
        # v0.5.2：deepseek-flash / qwen3.7-flash 是推理模型，思考 token 计入
        # completion——max_tokens 给小会返回空 content（触发 Agent 的空内容保护）
        text = router.complete("paper_check", prompt, max_tokens=4096)
        agent = router._last_agent
        return text, getattr(agent, "last_usage", None)

    # 限流判定：429 / 限流 / 速率限制 / 访问量过大 均属外部配额问题
    # （连续跑多个真调测试后常见），不代表缓存契约失效 → SKIP 而非 FAIL。
    def _is_throttle(err) -> bool:
        s = str(err)
        return any(k in s for k in ("429", "限流", "速率限制", "访问量过大"))

    def _skip(err) -> int:
        print(f'[SKIP] 平台限流（{str(err)[:80]}…），跳过前缀缓存实证')
        print('       外部配额问题，非代码/契约问题；稍后重跑即可。')
        return 0

    def call_or_skip(question: str, tag: str):
        """调用；限流则返回 None 表示应跳过整轮实证。"""
        print(f'{tag} ｜ 问题：{question}')
        try:
            return call(question)
        except AgentError as e:
            if _is_throttle(e):
                _skip(e)
                return None
            print(f'[FAIL] 调用失败：{e}')
            return "FAIL"

    # 第 1 次：建立缓存
    q1 = "该研究最值得质疑的一处统计报告遗漏是什么？"
    r1 = call_or_skip(q1, '[调用 1] 建立缓存')
    if r1 is None:
        return 0
    if r1 == "FAIL":
        return 1
    ans1, u1 = r1
    print(f'  -> {ans1[:150]}')
    print(f'  usage: {fmt_usage(u1)}')

    print('\n等待 5s（缓存异步写入）...\n')
    time.sleep(5)

    # 判定阈值：cached_tokens > 0 不代表真命中。
    # 平台在未命中时也会返回极少量 cached（观测到 2–3 token 的噪声，
    # 疑似系统提示的固定前缀），若按 >0 判定会假报"命中"并错误宣称省钱。
    # 真正的前缀缓存命中应覆盖绝大部分 prompt（智谱实测 96.8%），
    # 这里要求 ≥ 50% 且绝对值 ≥ 64 token，才算生效。
    HIT_RATIO, HIT_MIN_ABS = 0.50, 64

    def _is_real_hit(usage) -> bool:
        u = usage or {}
        cached = u.get("cached_tokens", 0)
        total = u.get("prompt_tokens", 0) or 1
        return cached >= HIT_MIN_ABS and (cached / total) >= HIT_RATIO

    # 第 2 次：同前缀不同问题
    q2 = "3.4 节的 t 检验报告有什么格式上的不完整？"
    r2 = call_or_skip(q2, '[调用 2] 验证命中')
    if r2 is None:
        return 0
    if r2 == "FAIL":
        return 1
    ans2, u2 = r2
    print(f'  -> {ans2[:150]}')
    print(f'  usage: {fmt_usage(u2)}')

    if _is_real_hit(u2):
        cached = u2["cached_tokens"]
        total = u2.get("prompt_tokens", 1)
        print(f'\n[PASS] 前缀缓存命中！命中率 {cached / total * 100:.1f}%'
              f'（cached={cached} / prompt={total}）')
        print('结论：冻结前缀契约在该平台生效，多次审计同篇论文可省大量输入费用。')
        return 0

    print('\n[MISS] 第 2 次未达有效命中阈值（异步写入可能未完成），10s 后重试...')
    time.sleep(10)
    q3 = "该研究的相关分析自由度是多少？"
    r3 = call_or_skip(q3, '[调用 3] 重试验证')
    if r3 is None:
        return 0
    if r3 == "FAIL":
        return 1
    ans3, u3 = r3
    print(f'  -> {ans3[:150]}')
    print(f'  usage: {fmt_usage(u3)}')
    if _is_real_hit(u3):
        cached = u3["cached_tokens"]
        total = u3.get("prompt_tokens", 1)
        print(f'\n[PASS] 前缀缓存命中（第 3 次）！命中率 {cached / total * 100:.1f}%')
        return 0

    print('\n[WARN] 三次调用均未观测到**有效**命中（阈值 ≥50% 且 ≥64 token）。')
    print('       观测到的 cached 仅为个位数 token 噪声，不构成真实缓存收益。')
    print('       可能原因：该平台上该模型未启用前缀缓存 / 前缀未达缓存块阈值')
    print('       / 该 Key 档位不含缓存。冻结前缀契约本身无害，保持即可，')
    print('       但**不要**据此宣称已在该平台省钱。')
    return 0


if __name__ == "__main__":
    sys.exit(main())
