"""图表生成 · 端到端测试

覆盖：
  1. /api/upload 拿到 file_id
  2. /api/chart 4 个方法全部产出 base64 PNG
  3. cache_hit 在第二次同参请求时为 True
  4. 错误路径：file_id 失效 / 不支持的方法
  5. 返回的 base64 解码后是合法 PNG
"""
import base64
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import app


def _post(client, path, **kwargs):
    return client.post(path, **kwargs)


def _assert_png(b64_str: str, method: str):
    """校验 base64 解码后是合法 PNG（前 8 字节是 PNG 文件头）。"""
    if not b64_str.startswith("data:image/png;base64,"):
        raise AssertionError(f"{method}: image 字段不是 data:image/png;base64,...")
    raw = base64.b64decode(b64_str.split(",", 1)[1])
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{method}: 解码后前 8 字节不是 PNG 文件头（{raw[:8]!r}）")
    return len(raw)


print("=" * 70)
print("图表生成 · 端到端测试")
print("=" * 70)

client = app.test_client()

# 1) 上传
with open(ROOT / "examples" / "student_scores.csv", "rb") as f:
    csv_bytes = f.read()
r = _post(client, "/api/upload", data={
    "file": (io.BytesIO(csv_bytes), "student_scores.csv"),
}, content_type="multipart/form-data")
up = r.get_json()
assert up["ok"], up
file_id = up["file_id"]
print(f"\n✓ 上传：file_id={file_id} · {up['rows']} 行 · {len(up['columns'])} 列")

# 2) 7 个方法的 chart（v0.7 加 3 个）
print()
print("--- 7 个方法出图 ---")
results = {}
for method, cols in [
    ("independent_t", {"group_col": "gender", "value_col": "score"}),
    ("anova", {"group_col": "study_intensity", "value_col": "score"}),
    ("correlation", {"value_col": "study_hours", "value_col2": "score"}),
    ("chi_square", {"group_col": "gender", "value_col": "pass"}),
    # v0.7
    ("paired_t", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}),
    ("mann_whitney", {"group_col": "gender", "value_col": "reaction_time_ms"}),
    ("wilcoxon", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}),
]:
    payload = {"file_id": file_id, "method": method, **cols}
    r = _post(client, "/api/chart", json=payload)
    d = r.get_json()
    assert d["ok"], f"{method}: {d.get('error')}"
    assert d["cache_hit"] is False, f"{method}: 第一次请求 cache_hit 应为 False"
    raw_len = _assert_png(d["image"], method)
    results[method] = raw_len
    print(f"  ✓ {method:<14} → PNG {raw_len:>6} 字节")

# 3) 缓存命中
print()
print("--- 缓存验证 ---")
r = _post(client, "/api/chart", json={
    "file_id": file_id, "method": "independent_t",
    "group_col": "gender", "value_col": "score",
})
d = r.get_json()
assert d["ok"] and d["cache_hit"] is True
print(f"  ✓ 第二次同参请求 cache_hit=True（PNG {results['independent_t']} 字节复用）")

# 4) 错误路径
print()
print("--- 错误路径 ---")
r = _post(client, "/api/chart", json={
    "file_id": "invalid_xxx", "method": "independent_t",
    "group_col": "gender", "value_col": "score",
})
assert r.status_code == 400
print(f"  ✓ 失效 file_id → HTTP 400：{r.get_json()['error'][:50]}")

r = _post(client, "/api/chart", json={
    "file_id": file_id, "method": "unsupported_method",
})
assert r.status_code == 400
print(f"  ✓ 不支持的方法 → HTTP 400：{r.get_json()['error'][:50]}")

# 5) 相关方法缺 value_col2
r = _post(client, "/api/chart", json={
    "file_id": file_id, "method": "correlation",
    "value_col": "study_hours",
})
assert r.status_code == 400
print(f"  ✓ 相关方法缺 value_col2 → HTTP 400：{r.get_json()['error'][:50]}")

print()
print("=" * 70)
print("✅ 图表端到端测试全部通过")
print("=" * 70)