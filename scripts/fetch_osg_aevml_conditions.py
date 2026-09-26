# -*- coding: utf-8 -*-
"""OSG 超硬防振型エンドミル AE-VML（ロング刃長）の公式切削条件をCSVへ追記する。

出典: OSG カタログ「超硬防振型エンドミル AE-VMシリーズ」
      https://www.osg.co.jp/media_dl/flier/file/n_115.pdf

登録する表（いずれも数値表をPDFのテキストから機械的に抽出）:
- p.38 高能率側面切削 刃長3D  ae≦0.2D の表（ap=3D, ae=0.2D）
- p.40 高能率側面切削 刃長4D  ae≦0.15D の表（ap=4D, ae=0.15D）
- 有効長は p.33 寸法表の刃長（APMX）

見積もりモデルは径方向切込みを 0.22〜0.3D 程度まで引き上げて計算するため、
ae の小さい標準表（ae=0.05D）ではなく、ae が最大の表を採用して過大評価を避ける。
被削材は 炭素鋼(S55C)・合金鋼(SCM)・ステンレス鋼(SUS304) の3列のみ。

使い方（pypdf が必要。アプリ本体の依存には含めない）:
    python scripts/fetch_osg_aevml_conditions.py [保存済みPDFのパス]

出力: data/osg_verified_cutting_conditions.csv の末尾に追記
（既存の AE-VML L/D=* 行は置き換え、それ以外の行はそのまま残す）
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
CSV_PATH = BASE_DIR / "data" / "osg_verified_cutting_conditions.csv"
SOURCE_URL = "https://www.osg.co.jp/media_dl/flier/file/n_115.pdf"

# 刃長（APMX）: p.33 寸法表
FLUTE_LENGTH = {
    3: {6: 19, 8: 25, 10: 31, 12: 38, 16: 50, 20: 62},
    4: {6: 24, 8: 32, 10: 40, 12: 48, 16: 64, 20: 80},
}

# 高能率表の列順: 炭素鋼, 合金鋼, プリハードン鋼, ステンレス鋼, 析出硬化系ステンレス鋼, チタン合金
MATERIALS = {
    0: ("S55C", "", "Carbon steel"),
    1: ("SCM", "~30HRC", "Alloy steel"),
    3: ("SUS304", "<=200HB", "Stainless steel"),
}

# (PDFページindex, ページ内の表番号, L/D, ae/D, 誌面ページ, 表名)
TABLES = [
    (38, 2, 3, 0.2, 38, "High Efficiency Side Milling 3D, ae<=0.2D"),
    (40, 1, 4, 0.15, 40, "High Efficiency Side Milling 4D, ae<=0.15D"),
]
DIAMETERS = [6, 8, 10, 12, 16, 20]


def download_pdf() -> Path:
    path = Path(tempfile.mkdtemp()) / "n_115.pdf"
    context = ssl.create_default_context()
    with urllib.request.urlopen(SOURCE_URL, context=context, timeout=60) as response:
        path.write_bytes(response.read())
    return path


def page_tables(reader: pypdf.PdfReader, page_index: int) -> list[dict[int, list[int]]]:
    """ページ内の条件表を、外径 → [rpm, feed, rpm, feed, ...] の辞書のリストで返す。"""
    text = reader.pages[page_index].extract_text()
    tables: list[dict[int, list[int]]] = []
    current: dict[int, list[int]] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(6|8|10|12|16|20) ((?:[\d,]+ ?)+)", line.strip())
        if not match:
            continue
        diameter = int(match.group(1))
        values = [int(value.replace(",", "")) for value in match.group(2).split()]
        if diameter in current:
            tables.append(current)
            current = {}
        current[diameter] = values
    if current:
        tables.append(current)
    return tables


def build_rows(reader: pypdf.PdfReader) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for page_index, table_no, ld, ae_ratio, printed_page, label in TABLES:
        table = page_tables(reader, page_index)[table_no]
        if sorted(table) != DIAMETERS:
            raise RuntimeError(f"p.{printed_page} の表の外径列が想定と異なります: {sorted(table)}")
        for diameter in DIAMETERS:
            values = table[diameter]
            if len(values) != 12:
                raise RuntimeError(f"p.{printed_page} φ{diameter} の列数が想定と異なります: {values}")
            for column, (material, hardness, group) in MATERIALS.items():
                rpm, feed = values[column * 2], values[column * 2 + 1]
                rows.append(
                    {
                        "manufacturer": "OSG",
                        "series_code": "AE-VML",
                        "product_name": "Anti-Vibration Long Carbide End Mill AE-VML",
                        "tool_type": "SQUARE",
                        "model_family": f"AE-VML L/D={ld}",
                        "outside_diameter_mm": f"{diameter:.1f}",
                        "corner_radius_label": "-",
                        "effective_length_mm": f"{FLUTE_LENGTH[ld][diameter]:.1f}",
                        "work_material": material,
                        "hardness": hardness,
                        "material_group": group,
                        "spindle_rpm": rpm,
                        "feed_rate_mm_min": f"{feed:.1f}",
                        "axial_depth_mm": f"{diameter * ld:.1f}",
                        "radial_depth_mm": f"{diameter * ae_ratio:.2f}".rstrip("0").rstrip("."),
                        "source_url": SOURCE_URL,
                        "source_page": printed_page,
                        "memo": (
                            f"PDF checked. Cutting Data: {label}. ap={ld}D, ae={ae_ratio:g}D. "
                            "Flute length from dimension table p.33. High-speed MC and rigid holder assumed."
                        ),
                    }
                )
    return rows


def main() -> int:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else download_pdf()
    rows = build_rows(pypdf.PdfReader(str(pdf_path)))

    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        fieldnames = csv.DictReader(f).fieldnames
        f.seek(0)
        existing_text = f.read()
    # 既存行は書式ごとそのまま残し、AE-VML L/D 行だけを置き換える
    kept_lines = [line for line in existing_text.splitlines() if ",AE-VML L/D=" not in line]
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        f.write("\n".join(kept_lines) + "\n")
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writerows(rows)
    print(f"{CSV_PATH.name}: AE-VML {len(rows)}件を書き込みました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
