"""v0.8 · 流式 SSE 渲染端到端测试。"""
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import io as _io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app import app  # noqa: E402


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """把 SSE 文本流解析成 [(event, data), ...]。"""
    events: list[tuple[str, dict]] = []
    cur_event = ""
    cur_data_parts: list[str] = []
    for line in raw.split("\n"):
        if line.startswith("event: "):
            cur_event = line[7:].strip()
        elif line.startswith("data: "):
            cur_data_parts.append(line[6:])
        elif line == "" and cur_event:
            payload = "\n".join(cur_data_parts)
            try:
                data = json.loads(payload) if payload.startswith(("{", "[")) else {"raw": payload}
            except json.JSONDecodeError:
                data = {"raw": payload}
            events.append((cur_event, data))
            cur_event = ""
            cur_data_parts = []
    return events


def main() -> None:
    client = app.test_client()
    # 上传示例数据
    csv = Path("examples/student_scores.csv").read_bytes()
    r = client.post(
        "/api/upload",
        data={"file": (_io.BytesIO(csv), "student_scores.csv")},
        content_type="multipart/form-data",
    )
    file_id = r.get_json()["file_id"]
    print(f"上传成功: file_id={file_id}")

    scenarios = [
        ("independent_t", {"group_col": "gender", "value_col": "score"}, "T 检验"),
        ("anova", {"group_col": "study_intensity", "value_col": "score"}, "ANOVA"),
        ("correlation", {"value_col": "study_hours", "value_col2": "score"}, "相关"),
        ("chi_square", {"group_col": "gender", "value_col": "pass"}, "卡方"),
        ("paired_t", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}, "配对 T"),
        ("mann_whitney", {"group_col": "gender", "value_col": "reaction_time_ms"}, "Mann-Whitney"),
        ("wilcoxon", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}, "Wilcoxon"),
    ]

    print()
    print("=" * 70)
    print("7 个方法的 SSE 事件流")
    print("=" * 70)

    for method, params, label in scenarios:
        payload = {"file_id": file_id, "method": method, "stream": 1, **params}
        r = client.post("/api/analyze", json=payload)
        assert r.status_code == 200, f"{label}: HTTP {r.status_code}"
        assert "text/event-stream" in r.headers.get("Content-Type", ""), \
            f"{label}: Content-Type = {r.headers.get('Content-Type')}"

        events = parse_sse(r.get_data(as_text=True))
        event_names = [e[0] for e in events]

        # 必备事件：init → clean → compute → write → markdown → chart → done
        assert "stage" in event_names, f"{label}: 缺 stage 事件"
        assert "markdown" in event_names, f"{label}: 缺 markdown 事件"
        assert "chart" in event_names, f"{label}: 缺 chart 事件"
        assert "done" in event_names, f"{label}: 缺 done 事件"

        # markdown 事件必有 markdown 字段
        md_data = next(d for ev, d in events if ev == "markdown")
        assert "markdown" in md_data and len(md_data["markdown"]) > 50, \
            f"{label}: markdown 内容为空"

        # done 事件 ok=True
        done_data = next(d for ev, d in events if ev == "done")
        assert done_data.get("ok") is True, f"{label}: done.ok = {done_data}"

        # chart 事件有 image（除非该方法无图）
        chart_data = next(d for ev, d in events if ev == "chart")
        if chart_data.get("error"):
            print(f"  ⚠ {label:<12} chart 错误: {chart_data['error']}")
        else:
            assert chart_data.get("image"), f"{label}: chart image 为空"

        # 阶段事件有 progress 单调递增
        progress = [d.get("progress", 0) for ev, d in events if ev == "stage"]
        assert progress == sorted(progress), f"{label}: progress 非单调 {progress}"

        print(f"  ✓ {label:<12} {len(events)} 个事件, "
              f"progress={progress[0]}→{progress[-1]}, "
              f"md={len(md_data['markdown'])}字, "
              f"chart={'有' if chart_data.get('image') else '无'}")

    # ---- 错误路径 ----
    print()
    print("=" * 70)
    print("错误路径")
    print("=" * 70)

    r = client.post("/api/analyze", json={"file_id": "invalid", "method": "independent_t", "stream": 1})
    events = parse_sse(r.get_data(as_text=True))
    assert any(ev == "error" for ev, _ in events), "失效 file_id 应有 error 事件"
    err = next(d for ev, d in events if ev == "error")
    print(f"  ✓ 失效 file_id → error: {err['message'][:30]}")

    r = client.post("/api/analyze", json={"file_id": file_id, "method": "unsupported", "stream": 1})
    events = parse_sse(r.get_data(as_text=True))
    err = next(d for ev, d in events if ev == "error")
    print(f"  ✓ 不支持方法 → error: {err['message'][:30]}")

    r = client.post("/api/analyze", json={"file_id": file_id, "method": "paired_t", "stream": 1})
    events = parse_sse(r.get_data(as_text=True))
    err = next(d for ev, d in events if ev == "error")
    print(f"  ✓ 配对 T 缺 value_col2 → error: {err['message'][:30]}")

    # ---- 非流式兼容 ----
    print()
    print("=" * 70)
    print("非流式（stream=0）向后兼容")
    print("=" * 70)
    r = client.post("/api/analyze", json={"file_id": file_id, "method": "independent_t",
                                          "group_col": "gender", "value_col": "score"})
    d = r.get_json()
    assert d["ok"] is True
    assert "markdown" in d and len(d["markdown"]) > 50
    print(f"  ✓ stream=0 仍返回 JSON（{len(d['markdown'])} 字 markdown）")

    print()
    print("=" * 70)
    print("✅ 流式 SSE 渲染端到端测试全部通过")
    print("=" * 70)


if __name__ == "__main__":
    main()
