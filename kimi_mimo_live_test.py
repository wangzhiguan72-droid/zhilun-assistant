"""
Kimi + 小米 MiMo 双平台实测脚本（v1.8，真实 API，最小 token 消耗）
========================================================================
验证 v1.8 新接入的两个平台（用户提供的测试 Key，已落盘 .env）：

    ① /models 列表拉取（免费）→ 确认平台现役模型 ID，避免瞎猜烧钱
    ② 薄壳 Agent 直连：complete() 正常出话 + usage 采集
    ③ 冻结前缀缓存观测：同前缀两次调用，看 cached_tokens 是否命中
       （判定标准与 zhipu_cache_test.py 一致：cached ≥64 且 ≥50%。
        Kimi 的缓存是"显式缓存"模式还是自动隐式，本脚本如实测量，
        不命中也如实报告，绝不假报省钱）
    ④ Router 集成：BYOK 注入 → 容灾链把用户平台排到链首并成功调用

省钱纪律（用户明确要求）：
    - 每次调用 max_tokens=64，前缀控制在 ~800 token 以内
    - Kimi 只用 k2.5 / k2.6 档（赠金额度有限，绝不碰 k3 旗舰）
    - 全程每平台最多 3 次补全调用

用法：
    .venv/Scripts/python.exe kimi_mimo_live_test.py --live
    （缺少 --live 或未设置对应 Key 时自动 SKIP 并以 0 退出，安全通过 CI；
      本脚本会真调外部 API，**不要**放进 `for t in *_test.py` 批处理扫全量，
      否则会被超时杀掉、还会因上游额度/限流产生与代码无关的失败。
      离线契约行为见 `kimi_mimo_offline_test.py`）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from env_loader import load_dotenv
load_dotenv()

from agents import AgentError, KimiAgent, MimoAgent, Router
from agents.secrets_guard import redact

# 真调开关：必须显式 --live 才发起网络请求。
# 这样无论谁把它扫进全量回归，都不会误报失败，也不会烧额度。
LIVE = "--live" in sys.argv
if not LIVE:
    print("  [SKIP] 未指定 --live：本脚本会真调 Kimi / MiMo 外部 API，已跳过。")
    print("  [SKIP] 如需实测请运行：python kimi_mimo_live_test.py --live")
    sys.exit(0)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def head(text, n=60):
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


# 冻结前缀：~800 token 的方法学材料（模拟 paper_check 场景，字节级稳定）
FROZEN_PREFIX = """你是论文统计部分的审稿专家。只依据下面的材料回答最后的问题。

<<<材料
某研究招募某高校两个年级共 180 名本科生，剔除无效问卷后得到有效样本 172 份，
其中男生 84 人、女生 88 人，平均年龄 19.6 岁（SD=1.12）。学业自我效能感量表
采用 Likert 五点计分，共 22 题，Cronbach's α = 0.87；学业成绩以期末综合成绩
（百分制）为准。统计方法：独立样本 t 检验比较性别差异；Pearson 相关分析
自我效能感与学业成绩的关系；卡方检验分析不同年级通过率。显著性水平 α=0.05。
结果：男生自我效能感 M=3.62, SD=0.54；女生 M=3.48, SD=0.61；t(170)=2.15,
p=0.033, Cohen's d=0.24。自我效能感与学业成绩相关 r=0.42, p<0.001。卡方
χ²(1)=3.51, p=0.061。所有分析在 SPSS 26.0 完成。
（以下为方法学背景：t 检验适用于两组均值比较，需满足正态性与方差齐性；
方差齐性不满足时可用 Welch 校正；效应量 d=0.2 小、0.5 中、0.8 大；相关
系数 r 的平方决定效度；p 值只说明极端程度，不说明效应大小；样本量影响
统计功效，G*Power 常用于先验功效分析；问卷反向题需反向计分后再求总分。）
材料>>>

问题："""


def probe_provider(cls, label, model_candidates, question, _call_gap=8.0):
    """单平台完整探测：models 列表 → 直连 → 前缀缓存双击。返回所用模型 ID。

    _call_gap：两次补全之间的间隔秒数。Kimi 赠金档限 RPM=3（平台实测
    报错文案），必须 ≥25s；MiMo 无明确限制，8s 稳妥。
    """
    print(f"\n===== {label} =====")
    agent = None
    chosen = None

    # ① 免费 /models：确认现役模型 ID（不猜、不烧钱）
    for cand in model_candidates:
        try:
            agent = cls(model=cand)
            break
        except AgentError as e:
            print(f"  [INFO] {cand} 实例化失败（缺 Key？）：{redact(str(e))}")
            return None
    if agent is None:
        return None
    try:
        ids = [m.id for m in agent.client.models.list()]
        print(f"  [INFO] 平台现役模型（前 12 个）：{ids[:12]}")
        # 按候选优先级挑一个平台上真实存在的 ID
        chosen = next((c for c in model_candidates if c in ids), None)
        if chosen is None:
            # 兜底：按关键词模糊挑（仍限非旗舰档）
            for kw in ("k2.6", "k2.5", "flash", "Flash"):
                chosen = next((i for i in ids if kw.lower() in i.lower()), None)
                if chosen:
                    break
        check(f"{label} /models 拉取并选定模型", bool(chosen),
              f"候选 {model_candidates} 都不在平台列表 {ids[:12]}")
        if not chosen:
            return None
        agent = cls(model=chosen)
        print(f"  [INFO] 选定模型：{chosen}")
    except AgentError as e:
        check(f"{label} /models 拉取", False, redact(str(e)))
        return None
    except Exception as e:  # noqa: BLE001
        check(f"{label} /models 拉取", False, f"{type(e).__name__}: {redact(str(e))}")
        return None

    # ② 直连补全（小 prompt；两家都是思考模型，max_tokens 给足 256，
    #    否则思考 token 就把小额度吃光、正文为空）
    try:
        out = agent.complete("用一句话说明什么是 p 值。", max_tokens=256)
        check(f"{label} 直连补全出话", bool(out.strip()), "返回空")
        print(f"  [INFO] 回答：{head(out)}")
        u = agent.last_usage or {}
        check(f"{label} usage 采集", u.get("total_tokens", 0) > 0, f"usage={u}")
        print(f"  [INFO] usage：prompt={u.get('prompt_tokens')} "
              f"completion={u.get('completion_tokens')} "
              f"cached={u.get('cached_tokens')}")
    except AgentError as e:
        check(f"{label} 直连补全", False, redact(str(e)))
        return chosen

    # ③ 冻结前缀双击（缓存观测；同一前缀 + 不同变量问题）
    try:
        time.sleep(_call_gap)
        q1 = question + "本研究的样本量是多少？只回答数字。"
        agent.complete(FROZEN_PREFIX + q1, max_tokens=256)
        time.sleep(_call_gap)  # 缓存写入通常是异步的；同时避开 RPM 限流
        out2 = agent.complete(FROZEN_PREFIX + question + "女生的自我效能感均值是多少？只回答数字。",
                              max_tokens=256)
        print(f"  [INFO] 二次回答：{head(out2)}")
        u2 = agent.last_usage or {}
        cached, total = u2.get("cached_tokens", 0), u2.get("prompt_tokens", 0)
        hit = cached >= 64 and total > 0 and (cached / total) >= 0.50
        if hit:
            ratio = cached / total * 100
            check(f"{label} 前缀缓存有效命中", True)
            print(f"  [INFO] 缓存命中：{cached}/{total} token（{ratio:.1f}%）——"
                  f"命中部分按折扣计费，重复审计能省不少")
        else:
            # 不假报省钱：如实记录该平台当前不自动命中
            check(f"{label} 前缀双击完成（是否自动缓存见备注）", True)
            print(f"  [NOTE] cached_tokens={cached}/{total}，未达有效命中阈值"
                  f"（≥64 且 ≥50%）。该平台可能需要显式缓存 API 或粒度不同，"
                  f"如实记录，不假报省钱。")
    except AgentError as e:
        check(f"{label} 前缀双击", False, redact(str(e)))
    return chosen


# ---------------------------------------------------------------------------
print("[1] Kimi（月之暗面）平台探测（只用 k2.5 / k2.6 档，不碰 k3）")
# ---------------------------------------------------------------------------
kimi_model = None
if os.environ.get("KIMI_API_KEY", "").strip():
    kimi_model = probe_provider(
        KimiAgent, "Kimi",
        ["kimi-k2.6", "kimi-k2.5", "kimi-k2.6-turbo", "kimi-latest"],
        "依据材料，", _call_gap=25.0)
else:
    print("  [SKIP] 未设置 KIMI_API_KEY")

# ---------------------------------------------------------------------------
print("\n[2] 小米 MiMo 平台探测")
# ---------------------------------------------------------------------------
mimo_model = None
if os.environ.get("MIMO_API_KEY", "").strip():
    mimo_model = probe_provider(
        MimoAgent, "小米 MiMo",
        ["mimo-v2.5", "mimo-v2.5-pro"],
        "依据材料，", _call_gap=25.0)
else:
    print("  [SKIP] 未设置 MIMO_API_KEY")

# ---------------------------------------------------------------------------
print("\n[3] Router BYOK 集成：用户 Key 平台排链首并成功出话")
# ---------------------------------------------------------------------------
if os.environ.get("KIMI_API_KEY", "").strip() and kimi_model:
    try:
        time.sleep(25)  # Kimi RPM=3：等上一轮调用的滑窗过去
        r = Router()  # 默认 free 档：验证 BYOK 放行规则
        r.set_user_key("kimi", os.environ["KIMI_API_KEY"])
        check("BYOK 后链首是 kimi", r._ordered_chain("paper_check")[0][0] == "kimi",
              f"实际={r._ordered_chain('paper_check')[0][0]}")
        out = r.complete("paper_check", "用一句话说明 t 检验的用途。", max_tokens=256)
        # 严格断言：必须真的路由到 Kimi 本尊，而不是容灾到别人
        check("Router 走 BYOK Kimi（free 档放行 + 链首 + 本尊调用）",
              isinstance(r._last_agent, KimiAgent),
              f"实际路由到 {type(r._last_agent).__name__}")
        print(f"  [INFO] 实际路由：{r._last_agent!r}")
        print(f"  [INFO] 回答：{head(out)}")
    except AgentError as e:
        check("Router 走 BYOK Kimi", False, redact(str(e)))
else:
    print("  [SKIP] 无 KIMI_API_KEY 或直连未选定模型")

if os.environ.get("MIMO_API_KEY", "").strip() and mimo_model:
    try:
        time.sleep(25)  # 新平台普遍限 RPM，等上一轮调用的滑窗过去
        r = Router()
        r.set_user_key("mimo", os.environ["MIMO_API_KEY"])
        check("BYOK 后链首是 mimo", r._ordered_chain("write_text")[0][0] == "mimo",
              f"实际={r._ordered_chain('write_text')[0][0]}")
        out = r.complete("write_text", "把『两组成绩有差别』润色成一句学术表述。",
                         max_tokens=256)
        if not isinstance(r._last_agent, MimoAgent):
            # 定位容灾原因：冷却表里有没有 mimo（429 等）
            print(f"  [INFO] mimo 未接住调用的原因排查：冷却表={r._cooldown_until}")
        check("Router 走 BYOK MiMo（free 档放行 + 链首 + 本尊调用）",
              isinstance(r._last_agent, MimoAgent),
              f"实际路由到 {type(r._last_agent).__name__}")
        print(f"  [INFO] 实际路由：{r._last_agent!r}")
        print(f"  [INFO] 回答：{head(out)}")
    except AgentError as e:
        check("Router 走 BYOK MiMo", False, redact(str(e)))
else:
    print("  [SKIP] 无 MIMO_API_KEY 或直连未选定模型")

# ---------------------------------------------------------------------------
print(f"\n{'=' * 46}")
print(f"Kimi + MiMo 实测：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 46}")
sys.exit(1 if FAIL else 0)
