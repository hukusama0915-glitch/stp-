# -*- coding: utf-8 -*-
"""OSG 超硬ドリル ADO-3D/5D/8D の公式切削条件を data/osg_drill_conditions.csv へ書き出す。

出典: OSG カタログ「超硬ドリルシリーズ ADO・AD・AD-LDS」
      https://www.osg.co.jp/media_dl/flier/file/n_116.pdf  誌面 p.52「切削条件基準表 ADO-3D/5D/8D」

- 穴深さ 8D 以下に適用する表（カタログ注記4）。水溶性切削油剤・MQL使用時の条件（注記1）。
- 送り量は表の範囲（例 0.12～0.24 mm/rev）をそのまま最小・最大として保存し、計算では中央値を使う。
- 被削材は 炭素鋼(S35C・S50C)・合金鋼(SCM 16～28HRC)・ステンレス鋼(SUS300/400系) の3列。
- 油穴なしの AD-2D/4D の表（p.56）にはステンレスの列が無いため、ADO の表を採用する。

使い方（pypdf が必要。アプリ本体の依存には含めない）:
    python scripts/fetch_osg_drill_conditions.py [保存済みPDFのパス]
"""
from __future__ import annotations

import csv
import re
import ssl
import sys
import tempfile
import urllib.request
from pathlib import Path

import pypdf

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "data" / "osg_drill_conditions.csv"
SOURCE_URL = "https://www.osg.co.jp/media_dl/flier/file/n_116.pdf"
PAGE_INDEX = 52  # PDFの53ページ目（誌面 p.52）
PRINTED_PAGE = 52
DIAMETERS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 20]

# (ページ内の表番号, 列番号) → 被削材
MATERIALS = [
    (0, 1, "S50C", "~210HB", "Carbon steel", "鉄"),
    (0, 2, "SCM", "16~28HRC", "Alloy steel", ""),
    (1, 2, "SUS304", "SUS300/400系", "Stainless steel", "SUS"),
]

FIELDS = [
    "manufacturer", "series_code", "product_name", "drill_diameter_mm", "work_material", "hardness",
    "material_group", "app_material", "spindle_rpm", "feed_per_rev_min", "feed_per_rev_max",
    "max_depth_ratio", "source_url", "source_page", "memo",
]


def download_pdf() -> Path:
    path = Path(tempfile.mkdtemp()) / "n_116.pdf"
    with urllib.request.urlopen(SOURCE_URL, context=ssl.create_default_context(), timeout=60) as response:
        path.write_bytes(response.read())
    return path


def parse_tables(text: str) -> list[dict[int, list[tuple[int, float, float]]]]:
    """外径 → [(回転数, 送り最小, 送り最大) ×4列] の表を上から順に返す。"""
    row_pattern = re.compile(r"^(\d{1,2}) ((?:[\d,]+ [\d.]+ ～ [\d.]+ ?){4})$")
    cell_pattern = re.compile(r"([\d,]+) ([\d.]+) ～ ([\d.]+)")
    tables: list[dict[int, list[tuple[int, float, float]]]] = []
    current: dict[int, list[tuple[int, float, float]]] = {}
    for line in text.splitlines():
        match = row_pattern.match(line.strip())
        if not match:
            continue
        diameter = int(match.group(1))
        cells = [
            (int(rpm.replace(",", "")), float(f_min), float(f_max))
            for rpm, f_min, f_max in cell_pattern.findall(match.group(2))
        ]
        if diameter in current:
            tables.append(current)
            current = {}
        current[diameter] = cells
    if current:
        tables.append(current)
    return tables


def build_rows(reader: pypdf.PdfReader) -> list[dict[str, object]]:
    tables = parse_tables(reader.pages[PAGE_INDEX].extract_text())
    if len(tables) != 2 or any(sorted(table) != DIAMETERS for table in tables):
        raise RuntimeError("ADO-3D/5D/8D の切削条件基準表を想定どおりに読み取れませんでした。")
    rows: list[dict[str, object]] = []
    for table_no, column, material, hardness, group, app_material in MATERIALS:
        for diameter in DIAMETERS:
            rpm, f_min, f_max = tables[table_no][diameter][column]
            rows.append(
                {
                    "manufacturer": "OSG",
                    "series_code": "ADO",
                    "product_name": "Carbide Drill ADO-3D/5D/8D (with oil hole)",
                    "drill_diameter_mm": f"{diameter:.1f}",
                    "work_material": material,
                    "hardness": hardness,
                    "material_group": group,
                    "app_material": app_material,
                    "spindle_rpm": rpm,
                    "feed_per_rev_min": f_min,
                    "feed_per_rev_max": f_max,
                    "max_depth_ratio": 8,
                    "source_url": SOURCE_URL,
                    "source_page": PRINTED_PAGE,
                    "memo": "PDF checked. Cutting Conditions ADO-3D/5D/8D. Water-soluble coolant or MQL. Depth <= 8D.",
                }
            )
    return rows


def main() -> int:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else download_pdf()
    rows = build_rows(pypdf.PdfReader(str(pdf_path)))
    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{OUTPUT.name}: {len(rows)}件を書き込みました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
