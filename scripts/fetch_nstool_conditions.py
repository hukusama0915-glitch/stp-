# -*- coding: utf-8 -*-
"""日進工具（NS TOOL）公式の切削条件参考表XLSXを取得し、CSVへ変換する。

対象は数値の切込み量(ap/ae)が表に含まれるロングネック系3シリーズ:
- MHR230  無限コーティング 2枚刃ロングネックエンドミル（深リブ用）
- MHR430  無限コーティング 4枚刃ロングネックエンドミル（深リブ用）
- MHR230R 無限コーティング 2枚刃ロングネックラジアスエンドミル

MSE245/MSE445等は切込み量がカタログ図（画像）でしか提供されないため、
数値を捏造しないよう切削条件としては登録しない。

使い方:
    python scripts/fetch_nstool_conditions.py [--offline 保存済みXLSXのディレクトリ]

出力: data/nstool_cutting_conditions.csv
（app.py起動時に data/*_cutting_conditions.csv として自動読み込みされる）
"""
from __future__ import annotations

import csv
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "data" / "nstool_cutting_conditions.csv"
M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

SERIES = {
    "MHR230": {
        "url": "https://www.ns-tool.com/ja/products/webcatalog/data/MHR230/MHR230_S.xlsx",
        "product_name": "無限コーティング 2枚刃ロングネックエンドミル（深リブ用） MHR230",
        "tool_type": "SQUARE",
        # (rpm列, feed列, ap列, ae列 or None=溝加工なのでae=工具径)
        "layout": {"dia": "A", "neck": "B", "radius": None},
        # variants: (rpm/feed係数, 被削材, 硬度, 材料グループ, memo追記)
        "groups": [
            (("C", "D", "E", None), [
                (1.0, "S50C", "", "Carbon steels", ""),
                (0.8, "SCM/SKD/SUS", "", "Alloy / stainless steels",
                 "カタログ※1に基づき合金鋼・ステンレス鋼は回転数・送り80%換算。"),
            ]),
            (("F", "G", "H", None), [(1.0, "NAK55/NAK80/HPM1", "~43HRC", "Prehardened steels", "")]),
            (("I", "J", "K", None), [(1.0, "銅・アルミニウム合金", "", "Copper / aluminum alloys", "")]),
        ],
        "memo": "日進工具 公式切削条件参考表(XLSX)。深リブ・溝加工条件のためae=工具径として登録。",
    },
    "MHR430": {
        "url": "https://www.ns-tool.com/ja/products/webcatalog/data/MHR430/MHR430_S.xlsx",
        "product_name": "無限コーティング 4枚刃ロングネックエンドミル（深リブ用） MHR430",
        "tool_type": "SQUARE",
        "layout": {"dia": "A", "neck": "B", "radius": None},
        "groups": [
            (("C", "D", "E", "F"), [
                (1.0, "S50C", "", "Carbon steels", ""),
                (0.8, "SCM/SKD/SUS", "", "Alloy / stainless steels",
                 "カタログ※1に基づき合金鋼・ステンレス鋼は回転数・送り80%換算。"),
            ]),
            (("G", "H", "I", "J"), [(1.0, "NAK55/NAK80/HPM1", "~43HRC", "Prehardened steels", "")]),
        ],
        "memo": "日進工具 公式切削条件参考表(XLSX)。側面切削条件（ap/ae表記あり）。",
    },
    "MHR230R": {
        "url": "https://www.ns-tool.com/ja/products/webcatalog/data/MHR230R/MHR230R_S.xlsx",
        "product_name": "無限コーティング 2枚刃ロングネックラジアスエンドミル MHR230R",
        "tool_type": "RADIUS",
        "layout": {"dia": "A", "neck": "C", "radius": "B"},
        "groups": [
            (("D", "E", "F", "G"), [(1.0, "S50C/NAK55/NAK80/HPM1", "~43HRC", "Carbon / prehardened steels", "")]),
            (("H", "I", "J", "K"), [(1.0, "HPM38/STAVAX/SKD61", "~55HRC", "Hardened steels", "")]),
            (("L", "M", "N", "O"), [(1.0, "銅・アルミニウム合金", "", "Copper / aluminum alloys", "")]),
        ],
        "memo": "日進工具 公式切削条件参考表(XLSX)。等高線・側面仕上げ条件（ap/ae表記あり）。",
    },
}

CSV_HEADER = [
    "manufacturer", "series_code", "product_name", "tool_type", "model_family",
    "outside_diameter_mm", "corner_radius_label", "effective_length_mm",
    "work_material", "hardness", "material_group", "spindle_rpm",
    "feed_rate_mm_min", "axial_depth_mm", "radial_depth_mm",
    "source_url", "source_page", "memo",
]


def col_letter(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def read_sheet(path: Path) -> list[dict[str, str]]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{M}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{M}t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter(f"{M}row"):
        cells: dict[str, str] = {}
        for c in row.findall(f"{M}c"):
            v = c.find(f"{M}v")
            if v is None:
                continue
            text = shared[int(v.text)] if c.get("t") == "s" else (v.text or "")
            if text.strip():
                cells[col_letter(c.get("r", ""))] = text.strip()
        rows.append(cells)
    return rows


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def convert(series_code: str, spec: dict, xlsx_path: Path) -> list[list]:
    rows = read_sheet(xlsx_path)
    layout = spec["layout"]
    out: list[list] = []
    dia: float | None = None
    radius: float | None = None
    for cells in rows:
        new_dia = to_float(cells.get(layout["dia"]))
        if new_dia is not None:
            dia = new_dia
            radius = None
        if layout["radius"]:
            new_radius = to_float(cells.get(layout["radius"]))
            if new_radius is not None:
                radius = new_radius
        neck = to_float(cells.get(layout["neck"]))
        if dia is None or neck is None:
            continue
        for (rpm_col, feed_col, ap_col, ae_col), variants in spec["groups"]:
            rpm = to_float(cells.get(rpm_col))
            feed = to_float(cells.get(feed_col))
            ap = to_float(cells.get(ap_col))
            if rpm is None or feed is None or ap is None:
                continue
            ae = to_float(cells.get(ae_col)) if ae_col else dia
            if ae is None:
                ae = dia
            corner_label = f"R{radius:g}" if radius is not None else "-"
            model_family = f"{series_code} φ{dia:g}x{neck:g}" + (f" R{radius:g}" if radius is not None else "")
            for factor, work, hardness, group, memo_suffix in variants:
                out.append([
                    "NS TOOL", series_code, spec["product_name"], spec["tool_type"], model_family,
                    dia, corner_label, neck,
                    work, hardness, group, int(rpm * factor),
                    round(feed * factor, 1), ap, round(ae, 4),
                    spec["url"], "", spec["memo"] + memo_suffix,
                ])
    return out


def main() -> None:
    offline_dir: Path | None = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--offline":
        offline_dir = Path(sys.argv[2])

    all_rows: list[list] = []
    for series_code, spec in SERIES.items():
        if offline_dir is not None:
            xlsx_path = offline_dir / f"{series_code}.xlsx"
        else:
            xlsx_path = Path(__file__).parent / f"_{series_code}.xlsx"
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(spec["url"], context=context) as response:
                xlsx_path.write_bytes(response.read())
        rows = convert(series_code, spec, xlsx_path)
        print(f"{series_code}: {len(rows)}件")
        all_rows.extend(rows)

    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(all_rows)
    print(f"written: {OUTPUT} ({len(all_rows)}件)")


if __name__ == "__main__":
    main()
