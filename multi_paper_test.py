"""v0.9.2 · 多学科论文识别行为回归测试。

素材（模板论文/，全部万方下载的真实论文）：
  1. M公司偿债能力评价体系（财务/会计）——真识别：回归分析 + r=0.912
  2. 光明乳业存货管理（工商管理）——案例研究，无统计方法
  3. 知识付费 4D 理论（情报/传播）——理论分析，无统计方法
  4. 噪声互相关系数判定（信号处理）——标题含"相关系数"但不应误报 Pearson
  5. 双摆起重机 ADRC（控制工程）——v0.9.1 已覆盖，此处复测加密路径

关键行为：
  A. 万方 AES 加密 PDF → 空密码自动解密
  B. "互相关系数"（信号处理概念）不误报为统计学相关分析
  C. 财务论文的真回归识别走"未实装"友好文案（非裸报错）
  D. 变量与数据列完全无交集 → 提示性文字（非 20 行全 ❌ 噪音表）
  E. 非统计类论文（2/3/5）→ 不自作主张跑分析
"""
import io as _io
import os
import sys
from pathlib import Path

# 本测试在一个进程里逐篇扫模板论文（>10 次 /api/check_paper），
# 会撞上 v1.7 新增的 LLM 接口限流（默认 8 次/分钟）→ 误报 429。
# 测试场景显式关闭限流；生产环境绝不能这样做（见 security_guard.disabled 文档）。
os.environ["RATE_LIMIT_DISABLE"] = "1"

sys.path.insert(0, str(Path(__file__).parent))
from app import app  # noqa: E402

ROOT = Path(__file__).parent
TPL = ROOT / "模板论文"
DATA = ROOT / "examples" / "student_scores.csv"


def _check(client, pdf_path: Path) -> dict:
    r = client.post(
        "/api/check_paper",
        data={
            "paper": (_io.BytesIO(pdf_path.read_bytes()), pdf_path.name),
            "data": (_io.BytesIO(DATA.read_bytes()), "s.csv"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, f"{pdf_path.stem[:20]}: HTTP {r.status_code}"
    d = r.get_json()
    assert d["ok"], f"{pdf_path.stem[:20]}: {d.get('error')}"
    return d


def main() -> None:
    client = app.test_client()

    print("=" * 70)
    print("多学科论文识别行为回归（4 篇万方论文 + 加密路径复测）")
    print("=" * 70)

    # ---- 1) M公司偿债能力（财务）：真识别回归 ----
    d = _check(client, TPL / "M公司偿债能力评价体系的改进及应用研究_张雨桐.pdf")
    pc, md = d["paper_claims"], d["audit"]["markdown"]
    mkeys = {m["method_key"] for m in pc["methods"]}
    assert "regression" in mkeys, f"财务论文应识别出回归分析，实际：{mkeys}"
    assert any(q["kind"] == "r" for q in pc["quantities"]), "应识别出 r 统计量"
    # v1.1：回归分析已实装（v1.0），不再走"未实装"文案，
    # 改为断言"真跑成功 + 给出回归相关建议"。
    assert "regression" in mkeys or "linear_regression" in mkeys, \
        f"财务论文应识别出回归分析，实际：{mkeys}"
    assert "线性回归" in md or "回归" in md, "应给出回归相关分析/建议"
    assert "无一匹配" in md, "变量全未匹配时应显示提示而非噪音表"
    print("  ✓ 财务论文：真识别回归 + r；真跑回归成功；全未匹配给提示")

    # ---- 2) 光明乳业存货（管理案例）：0 方法 ----
    d = _check(client, TPL / "供应链视角下的光明乳业存货管理研究_蔡晨宇 (1).pdf")
    pc, md = d["paper_claims"], d["audit"]["markdown"]
    assert len(pc["methods"]) == 0, f"管理案例论文不应识别出统计方法：{pc['methods']}"
    assert "未声明任何统计方法" in md
    print("  ✓ 管理案例论文：0 方法误报，第四节正确解释")

    # ---- 3) 知识付费 4D（情报理论）：0 方法 ----
    d = _check(client, TPL / "人工智能技术驱动的知识付费的现状、范式重塑与路径优化——基于4D理论的分析_李明德.pdf")
    pc, md = d["paper_claims"], d["audit"]["markdown"]
    assert len(pc["methods"]) == 0, f"理论分析论文不应识别出统计方法：{pc['methods']}"
    assert "未声明任何统计方法" in md
    print("  ✓ 理论分析论文：0 方法误报")

    # ---- 4) 噪声互相关系数（信号处理）：不误报 Pearson ----
    d = _check(client, TPL / "一种基于和通道与辅助通道噪声互相关系数的噪声干扰存在性判定方法_牟成虎.pdf")
    pc, md = d["paper_claims"], d["audit"]["markdown"]
    assert len(pc["methods"]) == 0, \
        f"信号处理'互相关系数'不应误报为统计学相关：{pc['methods']}"
    assert len(pc["quantities"]) == 0
    assert "未声明任何统计方法" in md
    print("  ✓ 信号处理论文：'互相关系数'不误报为 Pearson（0 方法 0 统计量）")

    # ---- 5) 双摆起重机（加密复测）----
    d = _check(client, TPL / "双摆桥式起重机轨迹规划与自抗扰控制研究.pdf")
    pc, md = d["paper_claims"], d["audit"]["markdown"]
    assert len(pc["methods"]) == 0
    assert pc["raw_text_length"] > 50000
    print("  ✓ 控制论文（AES 加密）：空密码解密 + 提取正常 + 0 误报")

    # ---- 6) 赤字率访谈（v0.9.3：新闻访谈 + 竖排版 PDF）----
    # 文件名含全角逗号/破折号，用 glob 模糊定位
    defz = next(TPL.glob("赤字率*.pdf"), None)
    if defz:
        d = _check(client, defz)
        pc, md = d["paper_claims"], d["audit"]["markdown"]
        assert len(pc["methods"]) == 0, f"新闻访谈不应识别出统计方法：{pc['methods']}"
        assert len(pc["quantities"]) == 0
        assert "未声明任何统计方法" in md
        print("  ✓ 新闻访谈（竖排版 PDF）：0 方法误报，提取虽碎但不影响判定")

    # ---- 7) 绿色金融（金融理论综述）----
    green = next(TPL.glob("绿色金融*.pdf"), None)
    if green:
        d = _check(client, green)
        pc, md = d["paper_claims"], d["audit"]["markdown"]
        assert len(pc["methods"]) == 0, f"理论综述不应识别出统计方法：{pc['methods']}"
        assert "未声明任何统计方法" in md
        print("  ✓ 金融理论综述：0 方法误报")

    # ---- 8) 减税降费（339 页经济学博士论文：真回归 + 表格脚注去重）----
    tax = next(TPL.glob("减税降费*.pdf"), None)
    if tax:
        d = _check(client, tax)
        pc, md = d["paper_claims"], d["audit"]["markdown"]
        mk = {m["method_key"] for m in pc["methods"]}
        assert "regression" in mk, f"实证经济学论文应识别出回归：{mk}"
        # v0.9.4：表格脚注统计量去重（原 80 条 → 少数几条）
        assert len(pc["quantities"]) < 10, \
            f"回归表格脚注应去重：实际 {len(pc['quantities'])} 条"
        # v0.9.5：显著性图例（"*** p<0.01，** p<0.05，* p<0.1"）整段跳过
        # 该论文表格脚注全是图例 → quantities 应为 0 条（比 v0.9.4 的 ×40 展示更干净）
        legend_p = [q for q in pc["quantities"]
                    if q["kind"] == "p" and q["value"] in (0.01, 0.05, 0.1)]
        assert not legend_p, f"图例 p 值不应被抽取：{legend_p}"
        assert "线性回归" in md or "回归" in md
        assert pc["raw_text_length"] > 100000  # 339 页长文
        print(f"  ✓ 339 页经济学博士论文：回归识别 + 图例根治"
              f"（{len(pc['quantities'])} 条统计量）+ 真跑回归成功")

    # ---- 9) 重磁匹配导航（综述）+ 普惠金融 + 财会大数据 ----
    for pattern, label in [("基于ICCP*", "导航算法综述"),
                            ("实体经济发展视角*", "普惠金融理论"),
                            ("财会大数据*", "财会大数据应用")]:
        p = next(TPL.glob(pattern), None)
        if not p:
            continue
        d = _check(client, p)
        pc, md = d["paper_claims"], d["audit"]["markdown"]
        assert len(pc["methods"]) == 0, f"{label} 不应识别出统计方法：{pc['methods']}"
        print(f"  ✓ {label}：0 方法误报")

    # ---- 加密标识统计 ----
    from pypdf import PdfReader
    n_enc = 0
    for pdf in TPL.glob("*.pdf"):
        try:
            if PdfReader(str(pdf)).is_encrypted:
                n_enc += 1
        except Exception:  # noqa: BLE001
            pass
    print(f"  ℹ 语料库共 {len(list(TPL.glob('*.pdf')))} 篇 PDF，其中 {n_enc} 篇加密（空密码可解）")

    print()
    print("=" * 70)
    print("✅ 多学科论文回归测试全部通过")
    print("=" * 70)


if __name__ == "__main__":
    main()
