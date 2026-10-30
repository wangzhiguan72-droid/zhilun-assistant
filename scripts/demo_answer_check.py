"""演示数据 × 标准答案 逐条对照（v2.60）

用法：.venv/Scripts/python.exe scripts/demo_answer_check.py <演示数据目录>
目录里需有 demo-预埋错误样本.xlsx、demo-论文示例.docx、demo-问卷数据.xlsx
（标准答案见同目录 demo-标准答案.pdf）。全部命中且零误报 → rc=0。

口径：走与界面相同的接口（/api/upload → /api/datacheck、/api/check_paper），
每条输出必须对上一条标准答案，对不上的计为误报；「无法比对」不计误报。
"""
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app import app  # noqa: E402

# 标准答案 → 体检条目标题里必须出现的关键词（任一）
DATA_KEY = {
    1: ("合计 ≠ 分项之和", ["与分项之和对不上"]),
    2: ("取值越界（成绩 > 100）", ["卷面得分」出现越界值"]),
    3: ("计数列出现小数", ["各班人数」出现了小数"]),
    4: ("重复行", ["完全重复的行"]),
    5: ("前后测差值过于规律", ["差值完全一致"]),
    6: ("常数列", ["整列取值完全相同"]),
    7: ("反向题疑似漏反向计分", ["题项与总分呈负相关"]),
    8: ("直线作答", ["直线作答"]),
    9: ("作答时长过短", ["作答时长过快"]),
    10: ("缺失模式异常", ["缺失率超过"]),
    11: ("末位偏好", ["末位数字明显偏好"]),
}


def _post(client, url, files):
    data = {k: (io.BytesIO(p.read_bytes()), p.name) for k, p in files.items()}
    return client.post(url, data=data, content_type="multipart/form-data").get_json()


def _match(titles, key):
    hit = {}
    for no, (_, kws) in key.items():
        hit[no] = [t for t in titles if any(k in t for k in kws)]
    used = {t for ts in hit.values() for t in ts}
    return hit, [t for t in titles if t not in used]


def main(demo: Path) -> int:
    c = app.test_client()
    up = _post(c, "/api/upload", {"file": demo / "demo-预埋错误样本.xlsx"})
    dc = c.post("/api/datacheck", json={"file_id": up["file_id"]}).get_json()
    issues = dc.get("issues") or []
    titles = [i["title"] for i in issues]
    hit, fp = _match(titles, DATA_KEY)
    found = sum(1 for v in hit.values() if v)
    print(f"数据体检：标准答案 11 处，检出 {found} 处，误报 {len(fp)} 条（共输出 {len(titles)} 条）")
    for no, (name, _) in DATA_KEY.items():
        print(f"  {'✓' if hit[no] else '✗'} #{no} {name}")
    for t in fp:
        print(f"  ⚠ 误报：{t}")

    a = _post(c, "/api/check_paper", {"paper": demo / "demo-论文示例.docx",
                                      "data": demo / "demo-问卷数据.xlsx"})["audit"]
    abstract = {m["abstract_raw"] for m in a["abstract_check"]["mismatches"]}
    p1 = {"t(94) = 2.41", "p = 0.018"} <= abstract
    tbl = a.get("table_check", {}).get("mismatches", [])
    p2 = any("实验B班" in m["paper"] and m["kind"] == "table_mean" for m in tbl)
    # 误报：被判「不一致」却不属于这两处埋雷的条目
    mism = [x for x in a["comparisons"] if x.get("status") == "mismatch"]
    paper_fp = [x["paper"] for x in mism
                if not ("实验B班" in x["paper"] and x.get("kind") == "table_mean")]
    paper_fp += [m for m in abstract if m not in {"t(94) = 2.41", "p = 0.018"}]
    print(f"论文核对：标准答案 2 处，检出 {int(p1) + int(p2)} 处，误报 {len(paper_fp)} 条")
    print(f"  {'✓' if p1 else '✗'} #1 摘要 t/p 与正文不一致")
    print(f"  {'✓' if p2 else '✗'} #2 表格实验B班后测均值 92.0（实算 79.7）")
    for t in paper_fp:
        print(f"  ⚠ 误报：{t}")
    ok = found == 11 and not fp and p1 and p2 and not paper_fp
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(main(Path(sys.argv[1]).resolve()))
