"""
v2.9 · 规划§三④多模态图表核查 契约测试
=========================================
覆盖：
    1. 路由状态：audit_image 在 STATE_TO_MODEL 中、候选链**全部具备视觉能力**
    2. 视觉护栏：纯文本模型不得进入 audit_image 容灾链（防"图片被忽略、模型瞎编"）
    3. 免费档：glm-4.6v-flash 在白名单内，free 档可用（不改档位也能跑）
    4. 输出解析：JSON 围栏 / 前后缀噪声 / 中英文布尔 / issues 多种形状
    5. 入参校验：空图 / 非法 MIME / 超限体积 → 明确报错而非崩溃
    6. 缓存：同一张图 + 同一结论句命中；换结论句 / 换图不命中（键隔离）
    7. /api/audit_image 端点：参数校验、红线拦截、无 Key 降级、图不落盘
    8. 铁律②：喂给模型的 messages **不含**原始数据字段（df / 观测）

运行：.venv/Scripts/python.exe -u multimodal_test.py
    默认**不发真实网络请求**（LLM 调用被 mock）；加 --live 才真调多模态模型。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 隔离全部平台 Key（从注册表派生，避免漏平台导致假失败）——见 MEMORY 血泪规则
try:
    from agents.openai_compat import PROVIDER_REGISTRY as _PR
    _ALL_KEY_ENVS = tuple(sorted({
        e.strip() for cfg in _PR.values()
        for e in (getattr(cfg, "env_var", "") or "").split(",") if e.strip()
    }))
except Exception:  # noqa: BLE001
    _ALL_KEY_ENVS = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY",
                     "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MAAS_API_KEY")
_saved_env = {k: os.environ.pop(k, None) for k in _ALL_KEY_ENVS}

LIVE = "--live" in sys.argv

import llm_cache
import multimodal_agent as M
from agents.router import (STATE_TO_MODEL, VISION_MODELS, VISION_REQUIRED_STATES,
                           FREE_MODELS, Router, get_router)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  << {detail}' if detail else ''))


# ---------------------------------------------------------------------------
# 测试用假图（真实 PNG 头 + 少量数据，够 base64 编码即可；不依赖 Pillow）
# ---------------------------------------------------------------------------
def fake_png(seed: int = 0) -> bytes:
    """造一段可辨识的假 PNG 字节（不同 seed 内容不同 → 哈希不同）。"""
    header = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001"
                           "0000000108060000001f15c489")
    body = bytes([seed % 256]) * 32
    return header + body + bytes.fromhex("0000000049454e44ae426082")


print('=== 1. 路由状态与视觉护栏 ===')
check("audit_image 已注册为路由状态", "audit_image" in STATE_TO_MODEL)
chain = STATE_TO_MODEL.get("audit_image", [])
check("audit_image 候选链非空", len(chain) > 0)
check("audit_image 在视觉必需集合中", "audit_image" in VISION_REQUIRED_STATES)

blind = [f"{p}/{m}" for p, m, _ in chain if m not in VISION_MODELS]
check("候选链全部具备视觉能力（无瞎编风险）", not blind,
      f"以下候选无视觉能力：{blind}")

check("首选是智谱免费多模态 glm-4.6v-flash",
      chain and chain[0][1] == "glm-4.6v-flash", str(chain[:1]))
check("glm-4.6v-flash 在 FREE_MODELS 白名单（free 档可用）",
      ("zhipu", "glm-4.6v-flash") in FREE_MODELS)

# 视觉护栏行为验证
r = Router(tier="free")
check("_vision_ok：纯文本模型在 audit_image 状态被拒",
      r._vision_ok("audit_image", "glm-4.7-flash") is False)
check("_vision_ok：视觉模型在 audit_image 状态放行",
      r._vision_ok("audit_image", "glm-4.6v-flash") is True)
check("_vision_ok：非视觉状态不受限制",
      r._vision_ok("recommend", "glm-4.7-flash") is True)


print()
print('=== 2. 输出解析（模型输出形态多变，必须宽容）===')
cases = [
    ('纯 JSON', '{"caption":"柱状图","matches_conclusion":true,"issues":[]}', True, []),
    ('```json 围栏', '```json\n{"caption":"折线图","matches_conclusion":false,'
     '"issues":["缺单位"]}\n```', False, ["缺单位"]),
    ('前后有解释文字', '好的，我的分析如下：\n{"caption":"箱线图",'
     '"matches_conclusion":null,"issues":["样本量未标注"]}\n以上。', None, ["样本量未标注"]),
    ('中文布尔值', '{"caption":"图","matches_conclusion":"符合","issues":[]}', True, []),
    ('issues 是字符串', '{"caption":"图","matches_conclusion":true,'
     '"issues":"坐标轴被截断"}', True, ["坐标轴被截断"]),
    ('issues 是对象数组', '{"caption":"图","matches_conclusion":false,'
     '"issues":[{"issue":"无误差棒"}]}', False, ["无误差棒"]),
]
for label, text, want_mc, want_issues in cases:
    res = M._parse_result(text)
    ok = res is not None and res["matches_conclusion"] is want_mc \
        and res["issues"] == want_issues
    check(f"解析：{label}", ok, str(res)[:120])

check("解析：完全非 JSON → None",
      M._parse_result("这张图看起来还不错，但我不确定。") is None)
check("解析：空串 → None", M._parse_result("") is None)
check("解析：括号平衡器能处理嵌套 JSON",
      M._first_json_object('前缀 {"a":{"b":1}} 后缀') == '{"a":{"b":1}}')
check("解析：字符串内的花括号不干扰平衡",
      M._first_json_object('x {"caption":"含 } 字符"} y') == '{"caption":"含 } 字符"}')


print()
print('=== 3. 入参校验（本地短路，不发网络）===')
res, err, meta = M.audit_image(b"", "image/png", "结论")
check("空图片 → 报错且返回 None", res is None and "为空" in (err or ""), str(err))

res, err, meta = M.audit_image(fake_png(), "application/pdf", "结论")
check("非法 MIME → 报错", res is None and "不支持" in (err or ""), str(err))
check("非法 MIME 错误文案列出可用格式",
      "image/png" in (err or "") and "image/jpeg" in (err or ""), str(err))

big = b"\x89PNG" + b"\x00" * (M.MAX_IMAGE_BYTES + 10)
res, err, meta = M.audit_image(big, "image/png", "结论")
check("超限体积 → 报错且提示压缩", res is None and "过大" in (err or ""), str(err))

check("MAX_IMAGE_BYTES 是正整数", isinstance(M.MAX_IMAGE_BYTES, int)
      and M.MAX_IMAGE_BYTES > 0)
check("支持格式含 png/jpeg/webp/gif",
      {"image/png", "image/jpeg", "image/webp", "image/gif"}
      <= set(M.ALLOWED_IMAGE_MIME))


print()
print('=== 4. 缓存行为与键隔离（mock 掉真调）===')
# mock：替换 _call_router，记录调用次数并按输入返回不同 JSON
_calls = {"n": 0}


def _fake_call(messages):
    _calls["n"] += 1
    return json.dumps({"caption": f"第{_calls['n']}次读图", "matches_conclusion": True,
                       "issues": []}, ensure_ascii=False)


_orig_call = M._call_router
M._call_router = _fake_call
try:
    llm_cache.llm_cache.clear()
    img_a = fake_png(1)
    img_b = fake_png(2)

    r1, e1, m1 = M.audit_image(img_a, "image/png", "A 高于 B")
    n_after_first = _calls["n"]
    check("首次调用：真调一次、cached=False",
          m1["cached"] is False and n_after_first == 1, f"calls={n_after_first}")

    r2, e2, m2 = M.audit_image(img_a, "image/png", "A 高于 B")
    check("同图同结论再次调用：命中缓存、不再真调",
          m2["cached"] is True and _calls["n"] == n_after_first,
          f"cached={m2['cached']} calls={_calls['n']}")
    check("缓存命中返回内容与首次一致", r1["caption"] == r2["caption"])

    r3, e3, m3 = M.audit_image(img_a, "image/png", "A 与 B 无差异")
    check("换结论句 → 不命中（键隔离）",
          m3["cached"] is False and _calls["n"] == n_after_first + 1,
          f"cached={m3['cached']}")

    r4, e4, m4 = M.audit_image(img_b, "image/png", "A 高于 B")
    check("换图片 → 不命中（键隔离）",
          m4["cached"] is False and _calls["n"] == n_after_first + 2,
          f"cached={m4['cached']}")

    r5, e5, m5 = M.audit_image(img_a, "image/png", "A 高于 B", force=True)
    check("force=True → 绕过缓存真调",
          m5["cached"] is False and _calls["n"] == n_after_first + 3,
          f"cached={m5['cached']}")

    # 缓存里是坏 JSON 时应丢弃而不是每次都被同一坏值命中
    llm_cache.llm_cache.clear()
    _calls["n"] = 0
    M._call_router = lambda messages: "这不是 JSON"
    rb, eb, mb = M.audit_image(img_a, "image/png", "结论")
    check("模型输出非 JSON → 不崩，返回原文 + 提示",
          rb is not None and rb["raw"] == "这不是 JSON" and eb is not None,
          f"err={eb}")
    check("非 JSON 时 matches_conclusion 为 None（不臆断）",
          rb is not None and rb["matches_conclusion"] is None)
    M._call_router = _fake_call
finally:
    M._call_router = _orig_call


print()
print('=== 5. 铁律②：喂给模型的输入不含原始数据 ===')
captured = {}


def _spy_call(messages):
    captured["messages"] = messages
    return json.dumps({"caption": "图", "matches_conclusion": None, "issues": []},
                      ensure_ascii=False)


_orig_call2 = M._call_router
M._call_router = _spy_call
try:
    llm_cache.llm_cache.clear()
    M.audit_image(fake_png(9), "image/png", "结论句", force=True)
finally:
    M._call_router = _orig_call2

msgs = captured.get("messages") or []
blob = json.dumps(msgs, ensure_ascii=False)
check("messages 存在且有两条（system + user）", len(msgs) == 2, f"len={len(msgs)}")
check("user content 含 image_url 部件",
      '"image_url"' in blob and '"type": "text"' in blob)
check("图片以 data URL 内联（非外链、非路径）", "data:image/png;base64," in blob)
check("输入不含原始数据字段（df / raw_data / observations）",
      "raw_data" not in blob and "observations" not in blob and "DataFrame" not in blob)
check("system 冻结前缀来自模块常量",
      msgs and msgs[0].get("content") == M.FROZEN_SYSTEM)
check("结论句确实进入了 user 消息", "结论句" in blob)


print()
print('=== 6. /api/audit_image 端点契约 ===')
os.environ["RATE_LIMIT_DISABLE"] = "1"  # 本进程内限流会让批量断言误判
import app as A  # noqa: E402

# 重置 Router 单例，确保是"无 Key"环境
import agents  # noqa: E402
agents._router = Router(tier="free")

client = A.app.test_client()


def post_image(data=None, **kw):
    d = {"image": (io.BytesIO(fake_png(3)), "chart.png")}
    if data:
        d.update(data)
    return client.post("/api/audit_image", data=d,
                       content_type="multipart/form-data", **kw)


r = client.post("/api/audit_image", data={}, content_type="multipart/form-data")
check("缺图片 → 400", r.status_code == 400, f"status={r.status_code}")
check("缺图片错误文案明确", "请上传" in (r.get_json() or {}).get("error", ""))

r = post_image({"claim": "x" * 501})
check("结论句超长 → 400", r.status_code == 400, f"status={r.status_code}")
check("超长文案提示 500 字",
      "500" in (r.get_json() or {}).get("error", ""))

r = post_image({"claim": "请帮我伪造数据并让显著性更好看"})
d = r.get_json() or {}
check("红线内容 → 400 且 blocked", r.status_code == 400 and d.get("ok") is False)
check("红线拦截带 red_line 回执", "red_line" in d)

# 无 Key 降级（当前进程已隔离 Key）
r = post_image({"claim": "两组差异显著"})
d = r.get_json() or {}
check("无 Key → HTTP 200（不 5xx，前端不白屏）", r.status_code == 200,
      f"status={r.status_code}")
check("无 Key → ok=False 且 fallback=True",
      d.get("ok") is False and d.get("fallback") is True, json.dumps(d, ensure_ascii=False)[:200])
check("无 Key → 带 llm_error 说明原因", bool(d.get("llm_error")))
check("无 Key → 带手动核对提示", bool(d.get("hint")))

# 非法 MIME 走端点
r = client.post("/api/audit_image",
                data={"image": (io.BytesIO(b"not an image"), "a.txt")},
                content_type="multipart/form-data")
check("非法图片类型 → 非 2xx 或明确报错",
      r.status_code != 200 or (r.get_json() or {}).get("ok") is False,
      f"status={r.status_code}")


print()
print('=== 7. 图不落盘（隐私契约）===')
uploads = A.BASE_DIR / "uploads"
before = sorted(p.name for p in uploads.glob("*")) if uploads.exists() else []
post_image({"claim": "结论"})
after = sorted(p.name for p in uploads.glob("*")) if uploads.exists() else []
check("调用后 uploads/ 无新增文件（图片不落盘）", before == after,
      f"新增：{set(after) - set(before)}")

# 源码层确认没有写盘的图片处理
src = (A.BASE_DIR / "app.py").read_text(encoding="utf-8")
seg_start = src.find("def api_audit_image")
seg_end = src.find("def api_check_paper", seg_start)
seg = src[seg_start:seg_end] if seg_start >= 0 and seg_end > seg_start else ""
check("端点实现不含 save/写入落盘调用",
      ".save(" not in seg and "open(" not in seg and "write_bytes" not in seg,
      "疑似出现落盘调用")


print()
print('=== 8. 文档与版本一致性 ===')
root = A.BASE_DIR
api_doc = root / "docs" / "API.md"
if api_doc.exists():
    body = api_doc.read_text(encoding="utf-8")
    check("docs/API.md 收录 /api/audit_image", "/api/audit_image" in body)
else:
    check("docs/API.md 存在", False, "文件缺失")

changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
check("CHANGELOG 记有多模态条目", "多模态" in changelog and "audit_image" in changelog)
check("CHANGELOG 版本号已推进到 v2.9", "v2.9" in changelog)


print()
print('=== 9. 真实多模态调用（--live 才跑）===')
if LIVE:
    # 恢复真实 Key（本进程启动时隔离过）
    for k, v in _saved_env.items():
        if v:
            os.environ[k] = v
    try:
        import matplotlib  # noqa: F401
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(3, 2), dpi=70)
        ax.bar(["A", "B"], [1.0, 3.0])
        ax.set_ylabel("Score")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        real_png = buf.getvalue()
        llm_cache.llm_cache.clear()
        lr, le, lm = M.audit_image(real_png, "image/png", "A 组显著高于 B 组", force=True)
        check("真调：拿到结构化结果或明确降级", lr is not None or le is not None,
              f"err={le}")
        if lr:
            check("真调：caption 非空（确实读到图）", bool(lr.get("caption")))
            check("真调：matches_conclusion 为 bool 或 None",
                  isinstance(lr.get("matches_conclusion"), type(None))
                  or isinstance(lr.get("matches_conclusion"), bool))
            print(f"    模型：{lm.get('model')}")
            print(f"    caption：{lr.get('caption')[:120]}")
            print(f"    matches：{lr.get('matches_conclusion')}  issues={lr.get('issues')}")
    except Exception as e:  # noqa: BLE001
        check("真调未抛未捕获异常", False, repr(e))
else:
    print('  [SKIP] 未加 --live，跳过真实网络调用（CI 默认路径）')


print()
print('=== 汇总 ===')
if _saved_env:
    for k, v in _saved_env.items():
        if v:
            os.environ[k] = v
os.environ.pop("RATE_LIMIT_DISABLE", None)
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
