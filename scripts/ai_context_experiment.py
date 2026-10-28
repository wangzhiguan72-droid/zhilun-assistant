"""Offline input-coverage ablation; no API requests or model-quality scoring.

Run: python scripts/ai_context_experiment.py --output _ai_capacity_20261028/context-ablation.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paper_context import build_paper_context


def run() -> dict:
    markers = {"method": "METHOD_RANDOM_ALLOCATION", "result": "RESULT_P_0043",
               "conclusion": "CONCLUSION_LIMITED_GENERALIZATION"}
    text = ("摘要\n研究背景。\n" + "背景论述。"*6000 + "\n方法\n" + markers["method"]
            + "\n" + "处理过程。"*3000 + "\n结果\n" + markers["result"]
            + "\n" + "讨论语境。"*3000 + "\n结论\n" + markers["conclusion"])
    arms = [{"arm": "local_only", "model_calls": 0, "included_chars": 0,
             "available_to_model": {k: False for k in markers}}]
    for mode in ("head", "evidence", "full"):
        ctx = build_paper_context(text, mode=mode, directive="结论",
                                 comparisons=[{"paper": markers["result"]}])
        arms.append({"arm": mode, "model_calls": 0,
                     "included_chars": ctx["included_chars"], "complete": ctx["complete"],
                     "available_to_model": {k: v in ctx["prompt_text"] for k, v in markers.items()},
                     "passages": [{k: v for k, v in p.items() if k != "text"} for p in ctx["passages"]]})
    assert not any(arms[1]["available_to_model"].values())
    assert all(arms[2]["available_to_model"].values())
    assert all(arms[3]["available_to_model"].values())
    assert arms[2]["included_chars"] <= 24000
    return {"experiment": "synthetic_input_coverage", "model_requests": 0,
            "model_quality_measured": False, "source_chars": len(text), "arms": arms,
            "interpretation": "验证模型能否拿到方法/结果/结论；不测模型判断、准确率、科研质量或用户收益。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    for arm in result["arms"]:
        print(arm["arm"], arm["included_chars"], arm["available_to_model"])
    print("Offline coverage checks passed; model calls: 0")
