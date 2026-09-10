"""四种统计方法的端到端测试"""
import json
import urllib.request

BASE = "http://127.0.0.1:5000"


def post_multipart(path, file_path, field="file"):
    boundary = "----multi"
    with open(file_path, "rb") as f:
        body = f.read()
    filename = file_path.split("\\")[-1]
    parts = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def post_json(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


print("=" * 70)
print("上传数据")
print("=" * 70)
up = post_multipart("/api/upload", r"D:\论文排版辅助agent\examples\student_scores.csv")
assert up["ok"]
file_id = up["file_id"]
for c in up["columns"]:
    print(f"  - {c['name']:<14} | {c['type']:<12} | n_unique={c['n_unique']}")


def run(method, **payload):
    print()
    print("=" * 70)
    print(f"测试: {method}")
    print("=" * 70)
    res = post_json("/api/analyze", {"file_id": file_id, "method": method, **payload})
    if not res.get("ok"):
        print(f"  ✗ 失败: {res.get('error')}")
        return None
    s = res["summary"]
    print(f"  ✓ 方法: {res['method']}")
    for k, v in s.items():
        if isinstance(v, float):
            print(f"    {k}: {v:.4f}")
        elif isinstance(v, dict):
            print(f"    {k}: {v}")
        elif isinstance(v, list):
            print(f"    {k}: {len(v)} 项")
        else:
            print(f"    {k}: {v}")
    print(f"\n  Markdown 长度: {len(res['markdown'])} 字符")
    return res


# 1) 独立样本 T 检验：gender × score
r1 = run("independent_t", group_col="gender", value_col="score")

# 2) ANOVA：study_intensity × score（3 组：低/中/高）
r2 = run("anova", group_col="study_intensity", value_col="score")

# 3) Pearson 相关：score × study_hours
r3 = run("correlation", value_col="score", value_col2="study_hours")

# 4) 卡方：gender × pass（2×2 列联表）
r4 = run("chi_square", group_col="gender", value_col="pass")

# 5) v0.7 配对 T 检验：anxiety_pre × anxiety_post
r5 = run("paired_t", value_col="anxiety_pre", value_col2="anxiety_post")

# 6) v0.7 Mann-Whitney U：gender × reaction_time_ms（含偏态+离群值）
r6 = run("mann_whitney", group_col="gender", value_col="reaction_time_ms")

# 7) v0.7 Wilcoxon 符号秩：anxiety_pre × anxiety_post
r7 = run("wilcoxon", value_col="anxiety_pre", value_col2="anxiety_post")

print()
print("=" * 70)
print("✅ 已测试：independent_t / anova / correlation / chi_square / "
      "paired_t / mann_whitney / wilcoxon")
print("=" * 70)