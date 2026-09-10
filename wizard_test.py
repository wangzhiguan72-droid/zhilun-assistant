"""v0.6 分步向导 · 端到端契约测试

不直接测前端 JS（JS 跑在浏览器里），而是模拟前端 wizard 推导出的 payload，
验证后端 /api/analyze 对 4 种方法（含卡方模式）的契约仍然成立。

覆盖场景：
  1. T 检验：Step 1 选 score → Step 2 选 gender（2 分类）→ 推 independent_t
  2. ANOVA：Step 1 选 score → Step 2 选 study_intensity（3 分类）→ 推 anova
  3. 相关：Step 1 选 score → Step 2 不选 → Step 3 选 study_hours → 推 correlation
  4. 卡方模式：点击「卡方切换」→ Step 1 选 gender → Step 3 选 pass → 推 chi_square
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import app


# ---------------------------------------------------------------------------
# 模拟前端 wizard 推导逻辑（含 v0.7 手动 override）
# ---------------------------------------------------------------------------
def wizard_payload(columns: list[dict], v1: str | None, g: str | None,
                    v2: str | None, mode: str = 't_anova',
                    manual: str | None = None) -> dict:
    """复刻 updateWizard 的方法推断逻辑（含手动覆盖）。

    manual 非空 = 用户在 Step 4 下拉里手动选了方法，按方法走校验
    manual 为 None = 走自动推断

    返回 (payload, expected_method, error)。
    """
    if manual:
        # 手动模式：按用户选的方法走
        if manual == 'independent_t':
            if not g: return ({"ok": False, "error": "需要分组变量"}, None)
            return ({"file_id": "_", "method": "independent_t", "group_col": g, "value_col": v1}, "independent_t")
        if manual == 'anova':
            if not g: return ({"ok": False, "error": "需要分组变量"}, None)
            return ({"file_id": "_", "method": "anova", "group_col": g, "value_col": v1}, "anova")
        if manual == 'mann_whitney':
            if not g: return ({"ok": False, "error": "需要分组变量"}, None)
            return ({"file_id": "_", "method": "mann_whitney", "group_col": g, "value_col": v1}, "mann_whitney")
        if manual == 'correlation':
            if not v2 or v1 == v2: return ({"ok": False, "error": "需要第二个变量"}, None)
            return ({"file_id": "_", "method": "correlation", "group_col": None, "value_col": v1, "value_col2": v2}, "correlation")
        if manual == 'paired_t':
            if not v2 or v1 == v2: return ({"ok": False, "error": "需要第二个变量"}, None)
            return ({"file_id": "_", "method": "paired_t", "group_col": None, "value_col": v1, "value_col2": v2}, "paired_t")
        if manual == 'wilcoxon':
            if not v2 or v1 == v2: return ({"ok": False, "error": "需要第二个变量"}, None)
            return ({"file_id": "_", "method": "wilcoxon", "group_col": None, "value_col": v1, "value_col2": v2}, "wilcoxon")
        if manual == 'chi_square':
            if not v2 or v1 == v2: return ({"ok": False, "error": "需要第二个变量"}, None)
            return ({"file_id": "_", "method": "chi_square", "group_col": v1, "value_col": v2, "value_col2": v2}, "chi_square")
        return ({"ok": False, "error": f"不支持的方法 {manual}"}, None)

    if mode == 'chi_square':
        if not v1 or not v2:
            return ({"ok": False, "error": "卡方模式需要两个分类列"}, None)
        if v1 == v2:
            return ({"ok": False, "error": "两个变量不能相同"}, None)
        t1 = next((c['type'] for c in columns if c['name'] == v1), None)
        t2 = next((c['type'] for c in columns if c['name'] == v2), None)
        if t1 != 'categorical' or t2 != 'categorical':
            return ({"ok": False, "error": "卡方需要两个分类列"}, None)
        # v0.6：runBtn 把 valueCol→group_col、valueCol2→value_col（向后兼容 v0.3 API）
        return ({
            "file_id": "_", "method": "chi_square",
            "group_col": v1, "value_col": v2, "value_col2": v2,
        }, "chi_square")

    # t_anova / correlation 自动模式
    if not v1:
        return ({"ok": False, "error": "请先选因变量"}, None)
    if g:
        g_col = next((c for c in columns if c['name'] == g), None)
        n_groups = g_col['n_unique'] if g_col else 0
        if n_groups == 2:
            method = 'independent_t'
        elif n_groups >= 3:
            method = 'anova'
        else:
            return ({"ok": False, "error": "分组列至少 2 个水平"}, None)
        return ({
            "file_id": "_", "method": method,
            "group_col": g, "value_col": v1,
        }, method)
    else:
        # 不分组 → 相关
        if not v2:
            return ({"ok": False, "error": "相关模式需要第二个数值列"}, None)
        if v1 == v2:
            return ({"ok": False, "error": "两个变量不能相同"}, None)
        return ({
            "file_id": "_", "method": "correlation",
            "group_col": None, "value_col": v1, "value_col2": v2,
        }, "correlation")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
print("=" * 70)
print("v0.6 分步向导 · 契约测试（wizard → /api/analyze）")
print("=" * 70)

client = app.test_client()

# 1) 上传示例数据
with open(ROOT / "examples" / "student_scores.csv", "rb") as f:
    csv_bytes = f.read()
r = client.post("/api/upload", data={
    "file": (io.BytesIO(csv_bytes), "student_scores.csv"),
}, content_type="multipart/form-data")
up = r.get_json()
assert up["ok"], up
columns = up["columns"]
file_id = up["file_id"]
print(f"\n✓ 上传：{up['rows']} 行 / {len(columns)} 列")
print(f"  列: {[(c['name'], c['type']) for c in columns]}")

# 2) 7 种 wizard 路径（自动推断 + 手动 override 混合）
print()
print("--- 7 种 wizard 路径 ---")
scenarios = [
    # (v1, g, v2, mode, manual, expected_method, label)
    ('score', 'gender', None, 't_anova', None, 'independent_t', 'T 检验：score + gender（2 分类）'),
    ('score', 'study_intensity', None, 't_anova', None, 'anova', 'ANOVA：score + study_intensity（3 分类）'),
    ('score', None, 'study_hours', 't_anova', None, 'correlation', '相关：score + study_hours（不分组）'),
    ('gender', None, 'pass', 'chi_square', None, 'chi_square', '卡方模式：gender + pass'),
    # v0.7 三个新方法只能手动选（自动推断无法识别"配对"或"非参数"）
    ('anxiety_pre', None, 'anxiety_post', 't_anova', 'paired_t', 'paired_t', '配对 T：anxiety_pre + anxiety_post（手动）'),
    ('reaction_time_ms', 'gender', None, 't_anova', 'mann_whitney', 'mann_whitney', 'Mann-Whitney：reaction_time_ms + gender（手动）'),
    ('anxiety_pre', None, 'anxiety_post', 't_anova', 'wilcoxon', 'wilcoxon', 'Wilcoxon：anxiety_pre + anxiety_post（手动）'),
]

for v1, g, v2, mode, manual, exp_method, label in scenarios:
    payload, derived = wizard_payload(columns, v1, g, v2, mode, manual=manual)
    assert derived == exp_method, f"{label} 推断方法 {derived} ≠ 期望 {exp_method}"
    payload['file_id'] = file_id  # 注入真实 file_id
    r = client.post('/api/analyze', json=payload)
    d = r.get_json()
    assert d['ok'], f"{label}: {d.get('error')}"
    assert d['method'] == exp_method
    print(f"  ✓ {label:<48} → {d['method']}, p={d.get('summary', {}).get('p')}")

# 3) 错误路径
print()
print("--- wizard 错误路径（应被前端拦截，但契约上也兜底） ---")
err_cases = [
    ('score', None, None, 't_anova', '相关模式需要第二个数值列'),
    ('score', 'gender', None, 't_anova', 'OK'),  # 这种情况其实 OK，下面会单独验
    ('score', None, 'score', 't_anova', '两个变量不能相同'),
    ('score', 'pass', None, 't_anova', 'OK'),  # pass 是 2 分类，OK
    ('gender', None, 'score', 'chi_square', '卡方需要两个分类列'),  # score 是连续列
]
for v1, g, v2, mode, expected in err_cases:
    payload, derived = wizard_payload(columns, v1, g, v2, mode)
    if expected == 'OK':
        # 这些场景走通了，直接验证
        if derived:
            payload['file_id'] = file_id
            r = client.post('/api/analyze', json=payload)
            d = r.get_json()
            assert d['ok'], f"期望 OK 但失败: {d.get('error')}"
        print(f"  ✓ 正常路径: derived={derived}")
        continue
    # 错误路径
    if 'error' in payload:
        # wizard 阶段就拦了
        print(f"  ✓ wizard 拦截：{payload['error']}")
    else:
        # 推到后端再被拦
        payload['file_id'] = file_id
        r = client.post('/api/analyze', json=payload)
        d = r.get_json()
        assert not d['ok'], f"期望失败但成功了: {d}"
        print(f"  ✓ 后端兜底：{d.get('error')[:50]}")

# 4) 验证分组列在 wizard 阶段就被正确推断
print()
print("--- wizard 推断细节 ---")
# 选 gender（2 分类）→ T 检验
_, m = wizard_payload(columns, 'score', 'gender', None, 't_anova')
assert m == 'independent_t', m
print(f"  ✓ gender（n_unique=2）→ T 检验")
# 选 study_intensity（3 分类）→ ANOVA
_, m = wizard_payload(columns, 'score', 'study_intensity', None, 't_anova')
assert m == 'anova', m
print(f"  ✓ study_intensity（n_unique=3）→ ANOVA")
# 选 pass（2 分类）→ T 检验（哪怕语义上奇怪，但分组合法）
_, m = wizard_payload(columns, 'score', 'pass', None, 't_anova')
assert m == 'independent_t', m
print(f"  ✓ pass（n_unique=2）→ T 检验")

# 5) v0.7 手动 override（用户在 Step 4 下拉切换方法）
print()
print("--- v0.7 手动 override（用户在 Step 4 切换方法） ---")
override_cases = [
    # (v1, g, v2, manual, expected, label)
    ('score', 'gender', None, 'mann_whitney', 'mann_whitney', 'gender 分组下手动切到 Mann-Whitney'),
    ('anxiety_pre', None, 'anxiety_post', 'paired_t', 'paired_t', '前后两列手动切到配对 T'),
    ('score', None, 'study_hours', 'wilcoxon', 'wilcoxon', '两数值列手动切到 Wilcoxon（不推荐，但契约上能跑）'),
    ('gender', None, 'pass', 'chi_square', 'chi_square', '两分类列手动切到卡方'),
    ('reaction_time_ms', 'gender', None, 'independent_t', 'independent_t', 'gender 分组下手动切到 T 检验（覆盖自动 Mann-Whitney 推荐）'),
]
for v1, g, v2, manual, exp, label in override_cases:
    payload, derived = wizard_payload(columns, v1, g, v2, 't_anova', manual=manual)
    assert derived == exp, f"{label} 推断方法 {derived} ≠ 期望 {exp}"
    payload['file_id'] = file_id
    r = client.post('/api/analyze', json=payload)
    d = r.get_json()
    assert d['ok'], f"{label}: {d.get('error')}"
    assert d['method'] == exp
    print(f"  ✓ {label:<52} → {d['method']}, p={d.get('summary', {}).get('p'):.4f}")

print()
print("=" * 70)
print("✅ 分步向导契约测试全部通过")
print("=" * 70)