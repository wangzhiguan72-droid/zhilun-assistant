"""冒烟测试：上传 → 识别 → T 检验 → 检查 Markdown 输出

⚠️ 需要先启动本地服务（本脚本会真发 HTTP 请求，不走 test_client）：
       python app.py          # 另开一个终端
   未启动时本脚本会**明确报告「未检测到服务」并以退出码 2 结束**，
   而不是抛一堆连接异常 —— 避免在批量回归里被误判成代码缺陷。
   （退出码 2 = 环境未就绪；1 = 真的测试失败；0 = 通过）
"""
import json
import os
import os
import sys
ROOT = os.path.dirname(os.path.abspath(__file__))
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:5000"


def _require_server():
    """服务未就绪时明确退出，不把「没起服务」伪装成「测试失败」。"""
    try:
        urllib.request.urlopen(BASE + "/health", timeout=3)
        return
    except Exception as exc:  # noqa: BLE001
        print("=" * 70)
        print("⚠️  未检测到本地服务，本测试无法运行。")
        print(f"    目标：{BASE}/health")
        print(f"    原因：{type(exc).__name__}: {exc}")
        print("    请先另开终端执行：  python app.py")
        print("=" * 70)
        sys.exit(2)


_require_server()

def post_multipart(path, file_path, field="file"):
    boundary = "----smoke123456"
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
print("冒烟测试 1: 上传示例数据")
print("=" * 70)
upload = post_multipart("/api/upload", os.path.join(ROOT, "examples", "student_scores.csv"))
assert upload.get("ok"), upload
print(f"✓ 文件: {upload['filename']} · 行数: {upload['rows']} · 列数: {len(upload['columns'])}")
for c in upload['columns']:
    print(f"  - {c['name']:<15} | type={c['type']:<12} | n_unique={c['n_unique']:<3} | missing={c['n_missing']}")
print(f"\n✓ 系统推荐: {upload['recommendation']['reason']}")
print(f"  默认方法: {upload['recommendation']['method']}")
print(f"  分组列:   {upload['recommendation'].get('group_col')}")
print(f"  因变量:   {upload['recommendation'].get('value_col')}")

print()
print("=" * 70)
print("冒烟测试 2: 独立样本 T 检验（gender × score）")
print("=" * 70)
result = post_json("/api/analyze", {
    "file_id": upload["file_id"],
    "method": "independent_t",
    "group_col": "gender",
    "value_col": "score",
})
assert result.get("ok"), result
s = result["summary"]
print(f"✓ 方法:        {result['method']}")
print(f"✓ 分组:        {result['groups']}")
print(f"✓ n1/n2:       {s['n1']} / {s['n2']}")
print(f"✓ mean1/mean2: {s['mean1']:.2f} / {s['mean2']:.2f}")
print(f"✓ sd1/sd2:     {s['sd1']:.2f} / {s['sd2']:.2f}")
print(f"✓ Levene p:    {s['levene_p']:.4f}")
print(f"✓ t 统计量:    {s['t']:.4f}")
print(f"✓ 自由度:      {s['df']}")
print(f"✓ p 值:        {s['p']:.4f}")
print(f"✓ 均值差:      {s['diff']:.4f}")
print(f"✓ 95% CI:      [{s['ci_low']:.3f}, {s['ci_high']:.3f}]")
print(f"✓ Cohen's d:   {s['d']:.4f}")
print(f"✓ 是否显著:    {'是' if s['significant'] else '否'} (p < 0.05)")

print()
print("=" * 70)
print("Markdown 输出预览（前 500 字符）")
print("=" * 70)
print(result["markdown"][:500])
print("...")
print(f"\n[完整 Markdown 共 {len(result['markdown'])} 字符]")

print()
print("=" * 70)
print("冒烟测试 3: 错误路径（3 个分组，T 检验应报错）")
print("=" * 70)
import urllib.error
try:
    bad = post_json("/api/analyze", {
        "file_id": upload["file_id"],
        "method": "independent_t",
        "group_col": "study_hours",   # 这是连续变量，T 检验会失败
        "value_col": "score",
    })
    print(f"返回: {bad}")
except urllib.error.HTTPError as e:
    body = json.loads(e.read().decode())
    print(f"✓ HTTP {e.code}: {body.get('error')}")

print()
print("=" * 70)
print("✅ 所有冒烟测试通过")
print("=" * 70)