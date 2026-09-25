# -*- coding: utf-8 -*-
"""サンプルSTPをまとめて解析し、結果サマリをJSONで出力する検証ハーネス。

使い方:
    .venv/Scripts/python scripts/run_estimate_samples.py [出力JSONパス]

DBを汚さないよう、一時DBに切り替えて実行する。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import app as app_module  # noqa: E402

# 履歴汚染を避けるため一時DBへ切り替え
_tmpdir = tempfile.mkdtemp(prefix="stp_tool_harness_")
app_module.DB_PATH = Path(_tmpdir) / "harness.sqlite3"
app_module.init_db()


def summarize(result: dict) -> dict:
    return {
        "total_sec": round(result["breakdown"]["total_sec"], 1),
        "time_label": app_module.seconds_label(result["breakdown"]["total_sec"]),
        "machining_sec": round(result["breakdown"]["machining_sec"], 1),
        "confidence": result["confidence"],
        "feature_count": len(result["features"]),
        "features": [
            {
                "type": f["feature_type"],
                "dims": f["dimensions"],
                "qty": f["quantity"],
                "tool": f["tool_name"],
                "sec": round(f["machining_sec"], 1),
                "reachability": f.get("reachability", ""),
            }
            for f in result["features"]
        ],
        "edm_candidates": result.get("edm_candidates", []),
        "reachability_issues": result["analysis"].get("reachability_issues", []),
    }


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    samples = sorted((BASE_DIR / "samples").glob("*.stp"))
    report: dict[str, dict] = {}
    for sample in samples:
        for material in ("鉄", "SUS"):
            key = f"{sample.name}|{material}"
            try:
                result = app_module.estimate(
                    sample,
                    sample.name,
                    material,
                    5.0,
                    1,
                    use_manufacturer_conditions=True,
                    estimate_mode="cautious",
                )
                report[key] = summarize(result)
            except Exception as exc:  # noqa: BLE001
                report[key] = {"error": str(exc)}
            print(f"{key}: {report[key].get('time_label', report[key].get('error'))}")

    if out_path:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"written: {out_path}")


if __name__ == "__main__":
    main()
