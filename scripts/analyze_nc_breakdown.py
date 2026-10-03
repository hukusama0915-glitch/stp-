"""実績NCを工具（工具交換）ごとに分解する: 切削・アプローチ・Z上昇・早送り・ドウェル・測定。

アプローチ = G00 の直後に続く「Z だけ下がる G01」（実績では 3mm@F500 → 1mm@F80 の2段）。
Z上昇 = Z だけ上がる G01（送り速度での引上げ）。それ以外の G01/G02/G03 を切削とする。
時間は指令送り（加減速なし）で計算する。早送りは引数の早送り速度（mm/min）。
出力: JSON（工具ブロックごとの集計）

使い方:
    python scripts/analyze_nc_breakdown.py <NCファイル> <出力JSON> [早送り mm/min（既定 50000）]

実績NCが増えたら、これで工具ごとの切削・アプローチを出し、金型モードの係数（mold_mode.py の
APPROACHES_PER_LEVEL・CUTTING_EFFICIENCY）や data/inhouse_mold_feeds.csv の見直しに使う。
2026-10-03 に 301 スキャナカバー（V77）で使用: 工具62本・切削87.1h・アプローチ26.6h（87,848回）。
"""
import json
import math
import re
import sys
from pathlib import Path

nc_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
RAPID = float(sys.argv[3]) if len(sys.argv) > 3 else 50000.0

token = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
blocks = []
cur = None
x = y = z = 0.0
motion = 0
feed = 0.0
after_rapid = False
pending_tool = None


def new_block(tool):
    return {
        "tool": tool, "diam": None, "radi": None, "rpm": None,
        "cut_len": 0.0, "cut_min": 0.0, "cut_moves": 0,
        "appr_count": 0, "appr_len": 0.0, "appr_min": 0.0,
        "retract_len": 0.0, "retract_min": 0.0,
        "rapid_len": 0.0, "rapid_min": 0.0, "rapid_count": 0,
        "dwell_sec": 0.0, "g65": 0, "feeds": {}, "z_levels": set(), "plunge_feeds": {},
    }


with open(nc_path, "rb") as fh:
    for raw in fh:
        line = raw.decode("ascii", "ignore").strip()
        if not line or line[0] in "%O":
            continue
        if line.startswith("("):
            if line.startswith("(TOOLINFO") and cur is not None:
                m = re.search(r"DIAM=([\d.]+)", line)
                r = re.search(r"RADI=([\d.]+)", line)
                if m:
                    cur["diam"] = float(m.group(1))
                if r:
                    cur["radi"] = float(r.group(1))
            continue
        words = token.findall(line)
        letters = {k: float(v) for k, v in words if k not in "G"}
        gcodes = [int(float(v)) for k, v in words if k == "G"]
        if "T" in letters:
            pending_tool = int(letters["T"])
        if "M" in letters and int(letters["M"]) == 6:
            # 工具交換（T は前の行で指定されている）
            cur = new_block(pending_tool)
            blocks.append(cur)
        if cur is None:
            continue
        if "S" in letters and cur["rpm"] is None:
            cur["rpm"] = letters["S"]
        if 4 in gcodes and "X" in letters and not any(g in (0, 1, 2, 3) for g in gcodes):
            cur["dwell_sec"] += letters["X"]
            continue
        if 65 in gcodes:
            cur["g65"] += 1
            continue
        if 28 in gcodes or 43 in gcodes or 91 in gcodes:
            # 原点復帰・工具長補正の移動は早送り扱い（距離は数えない）
            for g in gcodes:
                if g in (0, 1, 2, 3):
                    motion = g
            if "Z" in letters and 43 in gcodes:
                z = letters["Z"]
            continue
        for g in gcodes:
            if g in (0, 1, 2, 3):
                motion = g
        if "F" in letters:
            feed = letters["F"]
        if not any(k in letters for k in "XYZ"):
            continue
        nx, ny, nz = letters.get("X", x), letters.get("Y", y), letters.get("Z", z)
        dx, dy, dz = nx - x, ny - y, nz - z
        if motion == 0:
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            cur["rapid_len"] += dist
            cur["rapid_min"] += dist / RAPID
            cur["rapid_count"] += 1
            after_rapid = True
        else:
            if motion in (2, 3) and ("I" in letters or "J" in letters):
                i, j = letters.get("I", 0.0), letters.get("J", 0.0)
                cxp, cyp = x + i, y + j
                r0 = math.hypot(x - cxp, y - cyp)
                a0 = math.atan2(y - cyp, x - cxp)
                a1 = math.atan2(ny - cyp, nx - cxp)
                sweep = a1 - a0
                if motion == 2:  # 時計回り
                    sweep = -((-sweep) % (2 * math.pi)) or -2 * math.pi
                else:
                    sweep = sweep % (2 * math.pi) or 2 * math.pi
                if math.hypot(dx, dy) > 1e-6 and abs(abs(sweep) - 2 * math.pi) < 1e-9:
                    sweep = 0.0
                dist = math.hypot(abs(sweep) * r0, dz)
            else:
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            minutes = dist / max(feed, 1.0)
            pure_z = abs(dx) < 1e-9 and abs(dy) < 1e-9 and abs(dz) > 0
            if pure_z and dz < 0 and after_rapid:
                cur["appr_len"] += dist
                cur["appr_min"] += minutes
                key = str(int(feed))
                cur["plunge_feeds"][key] = cur["plunge_feeds"].get(key, 0) + 1
                if feed <= 100:  # 最後の切込み（F80）で1回と数える
                    cur["appr_count"] += 1
                    cur["z_levels"].add(round(nz, 3))
                    after_rapid = False
            elif pure_z and dz > 0:
                cur["retract_len"] += dist
                cur["retract_min"] += minutes
                after_rapid = False
            else:
                cur["cut_len"] += dist
                cur["cut_min"] += minutes
                cur["cut_moves"] += 1
                key = str(int(feed))
                cur["feeds"][key] = cur["feeds"].get(key, 0.0) + dist
                after_rapid = False
        x, y, z = nx, ny, nz

for b in blocks:
    b["z_levels"] = len(b["z_levels"])
    top = sorted(b["feeds"].items(), key=lambda kv: -kv[1])[:3]
    b["feeds"] = {k: round(v / 1000, 2) for k, v in top}
out_path.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
tot = {k: sum(b[k] for b in blocks) for k in ("cut_min", "appr_min", "retract_min", "rapid_min", "dwell_sec", "g65", "appr_count", "cut_len")}
print(f"工具ブロック {len(blocks)} / 切削 {tot['cut_min']/60:.1f}h ({tot['cut_len']/1000:.0f}m) / アプローチ {tot['appr_min']/60:.1f}h ({tot['appr_count']:,}回) / Z上昇 {tot['retract_min']/60:.1f}h / 早送り {tot['rapid_min']/60:.1f}h / ドウェル {tot['dwell_sec']/3600:.2f}h / G65 {tot['g65']}回")
