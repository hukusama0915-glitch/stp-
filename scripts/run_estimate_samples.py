# -*- coding: utf-8 -*-
"""サンプルSTPをまとめて解析し、結果サマリをJSONで出力する検証ハーネス。

使い方:
    .venv/Scripts/python scripts/run_estimate_samples.py [出力JSONパス] [--compare 基準JSON] [--tolerance 5]

--compare を指定すると、基準JSONと合計時間・フィーチャ構成を比較し、
許容差（%）を超えた差分があれば終了コード1で終わる。

開発DBを汚さないよう、import前に環境変数で一時DBへ切り替えて実行する。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# app の import 時に init_db() が走るため、import より前に一時DBを指定する
_tmpdir = tempfile.mkdtemp(prefix="stp_tool_harness_")
os.environ["STP_TOOL_DB_PATH"] = str(Path(_tmpdir) / "harness.sqlite3")

import app as app_module  # noqa: E402

# 形状を埋めて（MCのみで）算出するケースも回すサンプル
FILL_SAMPLES = ("wire_cut_test_plate.stp", "mixed_feature_test_part.stp")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


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


def compare_reports(baseline: dict[str, dict], current: dict[str, dict], tolerance_pct: float) -> list[str]:
    """基準と現在の結果を比較し、許容差を超えた差分の説明リストを返す。"""
    problems: list[str] = []
    for key in sorted(set(baseline) | set(current)):
        before = baseline.get(key)
        after = current.get(key)
        if before is None or after is None:
            problems.append(f"{key}: {'追加' if before is None else '削除'}されたケース")
            continue
        if "error" in before or "error" in after:
            if before.get("error") != after.get("error"):
                problems.append(f"{key}: エラー状態が変化 {before.get('error')} -> {after.get('error')}")
            continue
        base_sec = float(before["total_sec"])
        diff_pct = (float(after["total_sec"]) - base_sec) / base_sec * 100 if base_sec else 0.0
        if abs(diff_pct) > tolerance_pct:
            problems.append(
                f"{key}: 合計 {before['time_label']} -> {after['time_label']} ({diff_pct:+.1f}%)"
            )
        before_types = sorted(f["type"] for f in before["features"])
        after_types = sorted(f["type"] for f in after["features"])
        if before_types != after_types:
            problems.append(f"{key}: フィーチャ構成が変化 {len(before_types)}件 -> {len(after_types)}件")
        if len(before.get("edm_candidates", [])) != len(after.get("edm_candidates", [])):
            problems.append(
                f"{key}: 放電候補数が変化 {len(before.get('edm_candidates', []))} -> {len(after.get('edm_candidates', []))}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="サンプルSTPの見積もり回帰ハーネス")
    parser.add_argument("output", nargs="?", type=Path, help="結果JSONの出力先")
    parser.add_argument("--compare", type=Path, help="比較する基準JSON")
    parser.add_argument("--tolerance", type=float, default=5.0, help="合計時間の許容差（%%）")
    args = parser.parse_args()

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

    # ドリル穴・ワイヤ形状を埋めたモデル（MCのみ）の見積もり
    fill_options = {"drill_holes": True, "wire_shapes": True, "drill_max_diameter_mm": 13.0}
    for name in FILL_SAMPLES:
        sample = BASE_DIR / "samples" / name
        work_copy = Path(_tmpdir) / name  # 埋めたモデルはコピーの隣に書き出される
        shutil.copyfile(sample, work_copy)
        for material in ("鉄", "SUS"):
            key = f"{name}|{material}|埋め(ドリル+ワイヤ)"
            try:
                fill_payload, filled_path, fill_items = app_module.prepare_filled_model(work_copy, fill_options)
                if filled_path is None:
                    raise RuntimeError(fill_payload.get("error") or "埋めたモデルを作成できませんでした")
                result = app_module.estimate(
                    filled_path, name, material, 5.0, 1,
                    use_manufacturer_conditions=True, estimate_mode="cautious",
                )
                report[key] = summarize(result)
                with app_module.db() as conn:
                    nc_machine = conn.execute("SELECT * FROM machines WHERE machine_id = 1").fetchone()
                nc_plan = app_module.nc_hole_plan(fill_items, material, nc_machine, "cautious")
                report[key]["fill"] = {
                    "drill_count": fill_payload["drill_count"],
                    "wire_count": fill_payload["wire_count"],
                    "wire_cut_area_mm2": fill_payload["wire_cut_area_mm2"],
                    "nc_hole_sec": nc_plan.get("total_sec"),
                    "nc_hole_uncomputed": nc_plan.get("uncomputed_count"),
                }
            except Exception as exc:  # noqa: BLE001
                report[key] = {"error": str(exc)}
            print(f"{key}: {report[key].get('time_label', report[key].get('error'))}")

    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"written: {args.output}")

    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        problems = compare_reports(baseline, report, args.tolerance)
        if problems:
            print()
            print(f"基準との差分 {len(problems)}件（許容差 ±{args.tolerance:g}%）:")
            for line in problems:
                print(f"  - {line}")
            return 1
        print()
        print(f"基準との差分なし（許容差 ±{args.tolerance:g}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
