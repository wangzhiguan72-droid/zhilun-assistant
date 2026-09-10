"""v0.9.1 · 非统计类论文（工科/控制/仿真）识别行为回归测试。

素材：模板论文/双摆桥式起重机轨迹规划与自抗扰控制研究.pdf（85 页硕士论文）
- 纯工科控制论文：轨迹规划 + 粒子群优化 + 李雅普诺夫 + ADRC
- 无任何统计检验方法

期望行为（v0.9.1 修复后）：
  1. 方法识别 = 0（不误报）
  2. 统计量识别 = 0（不误报）
  3. 变量只剩高精度来源（quoted/near_stat/common_dict），丢弃 after_keyword 垃圾
  4. 报告第四节不"自作主张"跑 T 检验，而是解释"非统计类论文无需核查"
  5. PDF 上传解析正常
"""
import io as _io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app import app  # noqa: E402

ROOT = Path(__file__).parent
PDF = ROOT / "模板论文" / "双摆桥式起重机轨迹规划与自抗扰控制研究.pdf"


def main() -> None:
    assert PDF.exists(), f"测试素材缺失：{PDF}"
    client = app.test_client()

    print("=" * 70)
    print("工科论文（非统计类）识别行为回归")
    print("=" * 70)

    # 1) PDF 端到端
    r = client.post(
        "/api/check_paper",
        data={
            "paper": (_io.BytesIO(PDF.read_bytes()), "双摆桥式起重机.pdf"),
            "data": (_io.BytesIO((ROOT / "examples/student_scores.csv").read_bytes()),
                     "student_scores.csv"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    d = r.get_json()
    assert d["ok"], d.get("error")

    pc = d["paper_claims"]
    # 2) 方法 / 统计量 0 误报
    assert len(pc["methods"]) == 0, f"工科论文不应识别出统计方法，实际：{pc['methods']}"
    assert len(pc["quantities"]) == 0, f"工科论文不应识别出统计量，实际：{pc['quantities']}"
    print(f"  ✓ 方法识别 = 0（无误报）")
    print(f"  ✓ 统计量识别 = 0（无误报）")

    # 3) 变量只剩高精度来源
    bad_sources = [v for v in pc["variables"] if "after_keyword" in v["sources"]]
    assert not bad_sources, f"after_keyword 垃圾未被清除：{bad_sources[:3]}"
    print(f"  ✓ 变量只剩高精度来源（{len(pc['variables'])} 个，无 after_keyword 垃圾）")

    # 4) 报告第四节不跑 T 检验
    md = d["audit"]["markdown"]
    assert "未声明任何统计方法" in md, "第四节应解释'非统计类论文'而非自作主张跑分析"
    assert "非统计类论文" in md
    assert "independent_t** 方法" not in md, "不应出现'采用 independent_t 方法'"
    print(f"  ✓ 第四节正确解释非统计类论文（不跑 T 检验）")

    # 5) 改进建议是针对性 2 条
    assert "非假设检验范式" in md
    print(f"  ✓ 改进建议针对'非统计类论文'给 2 条说明")

    # 6) PDF 文本提取量合理（85 页论文）
    assert pc["raw_text_length"] > 50000, f"PDF 提取文本过少：{pc['raw_text_length']}"
    print(f"  ✓ PDF 提取 {pc['raw_text_length']} 字符")

    # 7) 变量提取降噪的单元验证（extract_variables 直接调）
    from extract_paper import extract_variables
    txt_path = ROOT / "模板论文" / "双摆桥式起重机轨迹规划与自抗扰控制研究_提取.txt"
    if not txt_path.exists():
        # 兜底：现场从 PDF 提取
        from pypdf import PdfReader
        reader = PdfReader(str(PDF))
        if reader.is_encrypted:
            reader.decrypt("")
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    else:
        text = txt_path.read_text(encoding="utf-8")
    vars_raw = extract_variables(text)
    # 长度收紧后应远小于 338（v0.9.1 之前）
    assert len(vars_raw) < 200, f"降噪不足：{len(vars_raw)}（修复前 338）"
    # 所有 after_keyword 来源的变量长度 ≤ 8
    for v in vars_raw:
        if "after_keyword" in v["sources"]:
            assert len(v["name"]) <= 8, f"超长片段漏过：{v['name']}"
    print(f"  ✓ extract_variables 降噪：{len(vars_raw)} 个（修复前 338）")

    print()
    print("=" * 70)
    print("✅ 非统计类论文回归测试全部通过")
    print("=" * 70)


if __name__ == "__main__":
    main()
