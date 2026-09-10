"""论文排查 · 回归分析专项测试（v1.0）"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:5000"


def post_multipart_2files(path, paper_path, data_path):
    boundary = "----papercheck_reg"
    def part(name, filename, body, ctype="application/octet-stream"):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + body + b"\r\n"
    with open(paper_path, "rb") as f:
        paper_body = f.read()
    with open(data_path, "rb") as f:
        data_body = f.read()
    parts = [
        part("paper", paper_path.split("\\")[-1], paper_body, "text/markdown"),
        part("data", data_path.split("\\")[-1], data_body, "text/csv"),
        f"--{boundary}--\r\n".encode()
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{BASE}{path}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


print("=" * 70)
print("论文排查 · 回归分析专项测试")
print("=" * 70)
result = post_multipart_2files(
    "/api/check_paper",
    r"D:\论文排版辅助agent\examples\sample_regression_paper.md",
    r"D:\论文排版辅助agent\examples\sample_regression_data.csv",
)
assert result.get("ok"), result

pc = result["paper_claims"]
print(f"\n上传：论文 {result['filename_paper']} · 数据 {result['filename_data']}（{result['rows']} 行）")

print("\n--- 论文中识别到的统计方法 ---")
for m in pc['methods']:
    print(f"  • {m['method_label']:<14} | key={m['method_key']:<15} | context={m['context'][:50]}…")

print("\n--- 论文中识别到的统计量 ---")
for q in pc['quantities']:
    print(f"  • {q['kind']:<5} | raw={q['raw']:<25} | value={q.get('value')}")

print("\n--- 论文中识别到的变量 ---")
for v in pc['variables']:
    print(f"  • {v['name']:<10} | 来源={','.join(set(v['sources']))}")

print("\n--- 真实数据重跑结果 ---")
real = result["audit"]["real"]
print(f"  方法 key: {real['method_key']}")
for k, v in real.items():
    if k in ("ok", "error", "method_key", "group_means", "group_sizes", "coefficients"):
        if isinstance(v, list):
            print(f"  {k}: {v}")
        continue
    if isinstance(v, float):
        print(f"  {k}: {v:.4f}")
    else:
        print(f"  {k}: {v}")

print("\n--- 回归系数 ---")
for c in real.get("coefficients", []):
    print(f"  {c.get('variable', '-'):<10} β={c.get('beta', 0):.3f}  p={c.get('p', 1):.4f}")

print("\n--- 声称 vs 实际 对比 ---")
for c in result["audit"]["comparisons"]:
    print(f"  status={c.get('status'):<10} kind={c.get('kind','-')} paper={c.get('paper','-')} real={c.get('real','-')}")

print("\n--- 改进建议 ---")
for i, s in enumerate(result["audit"]["suggestions"], 1):
    print(f"  {i}. {s}")

print("\n" + "=" * 70)
print("✅ 回归分析专项测试通过")
print("=" * 70)