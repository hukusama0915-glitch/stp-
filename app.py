from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "stp_time_tool.sqlite3"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_CLEANUP_EXTENSIONS = {".stp", ".step"}
APP_VERSION = "2026-05-23-master-repair-no-face-mill"


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


UPLOAD_RETENTION_DAYS = max(0, env_int("UPLOAD_RETENTION_DAYS", 7))


def cleanup_old_uploads(retention_days: int = UPLOAD_RETENTION_DAYS) -> dict[str, int]:
    if retention_days <= 0:
        return {"deleted": 0, "skipped": 0}

    cutoff = time.time() - retention_days * 24 * 60 * 60
    deleted = 0
    skipped = 0
    for path in UPLOAD_DIR.iterdir():
        try:
            if not path.is_file() or path.suffix.lower() not in UPLOAD_CLEANUP_EXTENSIONS:
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError:
            skipped += 1
    return {"deleted": deleted, "skipped": skipped}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tools (
                tool_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                tool_type TEXT NOT NULL,
                diameter_mm REAL NOT NULL,
                flute_count INTEGER NOT NULL,
                max_depth_mm REAL NOT NULL,
                material TEXT NOT NULL,
                roughing INTEGER NOT NULL DEFAULT 1,
                finishing INTEGER NOT NULL DEFAULT 1,
                memo TEXT
            );

            CREATE TABLE IF NOT EXISTS cutting_conditions (
                condition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_id INTEGER NOT NULL,
                material_type TEXT NOT NULL,
                process_type TEXT NOT NULL,
                spindle_rpm INTEGER NOT NULL,
                feed_rate_mm_min REAL NOT NULL,
                depth_of_cut_mm REAL NOT NULL,
                width_of_cut_mm REAL NOT NULL,
                tool_change_sec INTEGER NOT NULL,
                FOREIGN KEY(tool_id) REFERENCES tools(tool_id)
            );

            CREATE TABLE IF NOT EXISTS machines (
                machine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_name TEXT NOT NULL,
                axis_count INTEGER NOT NULL,
                rapid_feed_mm_min REAL NOT NULL,
                atc_time_sec INTEGER NOT NULL,
                max_spindle_rpm INTEGER NOT NULL,
                max_tool_diameter_mm REAL,
                setup_time_min INTEGER NOT NULL,
                memo TEXT
            );

            CREATE TABLE IF NOT EXISTS histories (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                file_name TEXT NOT NULL,
                material_type TEXT NOT NULL,
                blank_allowance_mm REAL NOT NULL,
                machine_name TEXT NOT NULL,
                total_sec REAL NOT NULL,
                confidence REAL NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manufacturer_catalogs (
                catalog_id INTEGER PRIMARY KEY AUTOINCREMENT,
                manufacturer TEXT NOT NULL,
                product_name TEXT NOT NULL,
                tool_type TEXT NOT NULL,
                flute_info TEXT,
                coating TEXT,
                material_hint TEXT,
                series_codes TEXT,
                catalog_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                memo TEXT,
                UNIQUE(manufacturer, product_name, catalog_url)
            );

            CREATE TABLE IF NOT EXISTS manufacturer_cutting_conditions (
                condition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                manufacturer TEXT NOT NULL,
                series_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                tool_type TEXT NOT NULL,
                model_family TEXT NOT NULL,
                outside_diameter_mm REAL NOT NULL,
                corner_radius_label TEXT NOT NULL,
                effective_length_mm REAL NOT NULL,
                work_material TEXT NOT NULL,
                hardness TEXT,
                material_group TEXT,
                spindle_rpm INTEGER NOT NULL,
                feed_rate_mm_min REAL NOT NULL,
                axial_depth_mm REAL NOT NULL,
                radial_depth_mm REAL NOT NULL,
                source_url TEXT NOT NULL,
                source_page INTEGER,
                memo TEXT,
                UNIQUE(
                    manufacturer, series_code, model_family, outside_diameter_mm,
                    corner_radius_label, effective_length_mm, work_material, hardness
                )
            );
            """
        )
        ensure_schema(conn)
        seed_union_tool_catalogs(conn)
        seed_osg_catalogs(conn)
        seed_union_tool_cutting_conditions(conn)
        ensure_catalog_tool_master(conn)
        ensure_operational_master(conn)
    cleanup_old_uploads()


def ensure_schema(conn: sqlite3.Connection) -> None:
    machine_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(machines)").fetchall()
    }
    if "max_tool_diameter_mm" not in machine_columns:
        conn.execute("ALTER TABLE machines ADD COLUMN max_tool_diameter_mm REAL")


DEFAULT_TOOLS = [
        ("φ16 フラットEM", "EM", 16, 4, 40, "アルミ/鉄/SUS", 1, 1, "側面・ポケット荒"),
        ("φ10 フラットEM", "EM", 10, 4, 30, "アルミ/鉄/SUS", 1, 1, "汎用ポケット"),
        ("φ6 フラットEM", "EM", 6, 4, 24, "アルミ/鉄/SUS", 1, 1, "小型機・小径ポケット"),
        ("φ6 ドリル", "DRILL", 6, 2, 45, "アルミ/鉄/SUS", 1, 0, "小径穴"),
        ("φ8 ドリル", "DRILL", 8, 2, 55, "アルミ/鉄/SUS", 1, 0, "中径穴"),
        ("φ10 ドリル", "DRILL", 10, 2, 65, "アルミ/鉄/SUS", 1, 0, "中径穴"),
        ("M6 タップ", "TAP", 6, 3, 25, "アルミ/鉄/SUS", 0, 1, "ねじ穴概算"),
    ]

DEFAULT_MACHINES = [
    ("標準 3軸MC", 3, 15000, 8, 12000, 16, 30, "概算用デフォルト"),
    ("高速 5軸MC", 5, 30000, 5, 20000, 20, 45, "5軸案件の概算"),
]


def seed_master(conn: sqlite3.Connection) -> None:
    tools = DEFAULT_TOOLS
    conn.executemany(
        """
        INSERT INTO tools
        (tool_name, tool_type, diameter_mm, flute_count, max_depth_mm, material, roughing, finishing, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tools,
    )

    seed_default_machines(conn)
    seed_default_conditions_for_tools(conn)


def seed_default_machines(conn: sqlite3.Connection) -> None:
    existing = {
        row["machine_name"]
        for row in conn.execute("SELECT machine_name FROM machines").fetchall()
    }
    rows = [row for row in DEFAULT_MACHINES if row[0] not in existing]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO machines
        (machine_name, axis_count, rapid_feed_mm_min, atc_time_sec, max_spindle_rpm, max_tool_diameter_mm, setup_time_min, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def seed_default_conditions_for_tools(conn: sqlite3.Connection, tool_ids: set[int] | None = None) -> None:
    condition_rows = []
    material_factor = {"アルミ": 1.8, "鉄": 1.0, "SUS": 0.55}
    for tool_id, tool_name, tool_type, diameter, *_ in conn.execute(
        "SELECT tool_id, tool_name, tool_type, diameter_mm FROM tools"
    ).fetchall():
        if tool_ids is not None and int(tool_id) not in tool_ids:
            continue
        for material, factor in material_factor.items():
            if conn.execute(
                """
                SELECT 1 FROM cutting_conditions
                WHERE tool_id = ? AND material_type = ?
                LIMIT 1
                """,
                (tool_id, material),
            ).fetchone():
                continue
            if tool_type == "FACE":
                base_feed = 1200
                process = "平面"
                ap = 1.5
                ae = diameter * 0.55
            elif tool_type == "DRILL":
                base_feed = 180
                process = "穴"
                ap = diameter
                ae = diameter
            elif tool_type == "TAP":
                base_feed = 120
                process = "タップ"
                ap = diameter
                ae = diameter
            else:
                base_feed = 650
                process = "ポケット"
                ap = min(4.0, diameter * 0.35)
                ae = diameter * 0.4
            rpm = min(12000, max(800, int((1000 * 90 * factor) / (math.pi * diameter))))
            condition_rows.append(
                (tool_id, material, process, rpm, round(base_feed * factor, 1), ap, round(ae, 2), 8)
            )
    if not condition_rows:
        return
    conn.executemany(
        """
        INSERT INTO cutting_conditions
        (tool_id, material_type, process_type, spindle_rpm, feed_rate_mm_min,
         depth_of_cut_mm, width_of_cut_mm, tool_change_sec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        condition_rows,
    )


def ensure_operational_master(conn: sqlite3.Connection) -> None:
    remove_deprecated_default_tools(conn)
    seed_default_machines(conn)


def remove_deprecated_default_tools(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT tool_id
        FROM tools
        WHERE tool_name = ? AND tool_type = ?
        """,
        ("φ50 フェイスミル", "FACE"),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM cutting_conditions WHERE tool_id = ?", (row["tool_id"],))
        conn.execute("DELETE FROM tools WHERE tool_id = ?", (row["tool_id"],))


def purge_non_catalog_tooling(conn: sqlite3.Connection) -> dict[str, int]:
    non_catalog_tools = conn.execute(
        """
        SELECT tool_id
        FROM tools
        WHERE memo IS NULL
           OR memo NOT LIKE '%http%'
        """
    ).fetchall()
    tool_ids = [int(row["tool_id"]) for row in non_catalog_tools]
    if not tool_ids:
        return {"tools": 0, "conditions": 0}

    placeholders = ",".join("?" for _ in tool_ids)
    condition_count = conn.execute(
        f"SELECT COUNT(*) FROM cutting_conditions WHERE tool_id IN ({placeholders})",
        tool_ids,
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM cutting_conditions WHERE tool_id IN ({placeholders})",
        tool_ids,
    )
    conn.execute(
        f"DELETE FROM tools WHERE tool_id IN ({placeholders})",
        tool_ids,
    )
    ensure_catalog_tool_master(conn)
    return {"tools": len(tool_ids), "conditions": int(condition_count)}


UNION_TOOL_SOURCE_URL = "https://www.uniontool.co.jp/catalog/endmill.html"
OSG_SOURCE_URL = "https://www.osg.co.jp/media_dl/flier/endmill.html"

UNION_TOOL_CATALOG_ITEMS = [
    ("超硬エンドミル総合カタログ vol.22", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vol22_jp.pdf"),
    ("鉄鋼用ドリルシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_tungsten.pdf"),
    ("エコノミーシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_economy.pdf"),
    ("Φ3シャンクVシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vseriescatalog.pdf"),
    ("Φ3シャンクVシリーズ 4枚刃 高硬度用ロングネックラジアスエンドミル VHGLRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vseries_vhglrs.pdf"),
    ("UTCOAT 2枚刃 ロングネックラジアスエンドミル CLRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_clrs.pdf"),
    ("DLCCOAT 2枚刃/4枚刃 銅電極加工用スクエアエンドミル DLCES2000/4000", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlces2000-4000.pdf"),
    ("HMGCOAT 6枚刃 高硬度材加工用スクエアエンドミル HGS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hgs.pdf"),
    ("2枚刃ボールエンドミル HWB/HWB-S/CWB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hwb_hwb-s_cwb.pdf"),
    ("2枚刃 ロングネックボール HGLB/HWLB/HWLB-S/CWLB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hwlb_hwlb-s_cwlb_hglb.pdf"),
    ("UDC 2枚刃 高靭性超硬合金加工用ボール・ロングネックボール UDCSB/UDCSLB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_udcsb_udcslb.pdf"),
    ("4枚刃 ロングネックボールエンドミル CBN-LBF4000", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn-lbf4000.pdf"),
    ("DLCCOAT 3枚刃 アルミ加工用チップブレーカ付スクエアエンドミル DLC-ALES", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlc-ales.pdf"),
    ("部品加工用総合リーフレット", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_processing.pdf"),
    ("UTCOAT 4枚刃高能率スクエアエンドミル CEHS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cehs.pdf"),
    ("6枚刃/10枚刃超硬合金・硬脆材加工用荒加工専用ロングネックラジアスエンドミル UDCRRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_udcrrs.pdf"),
    ("UDC 超硬合金・硬脆材用シリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/UDC catalog_vol.13_jp.pdf"),
    ("CBN 4枚刃 ハイグレードロングネックラジアスエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn-lrf4000.pdf"),
    ("DLCCOAT銅電極用シリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlclb_202304.pdf"),
    ("HMGCOAT 高硬度用4枚刃ロングネックラジアス", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hglrs_2023_04.pdf"),
    ("HMGCOAT 5枚刃/6枚刃 高硬度材加工用高能率ロングネックラジアスエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_em_hgrrs_2023_04.pdf"),
    ("CBNシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn_vol9_2111.pdf"),
    ("UTCOAT 高能率仕上げ加工用 バレルエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/em_barrel_vol2.pdf"),
    ("HARDMAX 2・3枚刃テーパネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_htnb_hftnb_01.pdf"),
    ("HMGCOAT 高硬度用ボール・ロングネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/catalog_em-hgb_vol6.pdf"),
    ("HARDMAX 4枚刃テーパネックラジアス", "https://www.uniontool.co.jp/assets/pdf/catalog/em-htnrs_vol3.pdf"),
    ("HARDMAX 4・6枚刃ラジアス", "https://www.uniontool.co.jp/assets/pdf/catalog/em-hmers.pdf"),
    ("HARDMAX 3枚刃テーパネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/em-hftnb.pdf"),
    ("DLCコート 1枚刃 アルミサッシ用 スクエアエンドミル DLCAL35Y", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlcal35y.pdf"),
    ("UTコート 3枚刃 球形状ボールエンドミル C-CQBLY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-cqbly.pdf"),
    ("UTコート 2枚刃平面仕上げ加工用スクエアエンドミル C-CSMY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-csmy.pdf"),
    ("5枚刃面取り加工用エンドミル CSVY・HSVY・DLCSVY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-csvy_hsvy_dlcsvy.pdf"),
    ("UTコート 2枚刃 球形状ボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-cqby_jp.pdf"),
    ("DLCコート 1枚刃 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlccps22y_jp.pdf"),
    ("DLCコート 2枚刃 ロングネックラジアスエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlclrsy_jp.pdf"),
    ("UTコート 3枚刃 ボールエンドミル/ロングシャンクボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_cfby_jp.pdf"),
    ("UTコート 2枚刃スレッドミル/DLCコート 2枚刃 スレッドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_ctmy_jp.pdf"),
    ("DLCコート 3枚刃 ボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlccfby_jp.pdf"),
    ("DLCコート 2枚刃 フラットドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlcdfy_jp.pdf"),
    ("UDCコート2枚刃 ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_udcmxy_jp.pdf"),
    ("HARDMAX 2枚刃 ロングネックボール/ショートシャンクロングネックボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_hlb_hlb-s_jp.pdf"),
    ("DLCコート 2枚刃 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/022681-01_d-d_a4.pdf"),
    ("DLCコート 3枚刃 アルミ加工用ロングネックスクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/022683-01_d-a_a4.pdf"),
    ("UTコート 2枚刃 2段角センタードリル", "https://www.uniontool.co.jp/assets/pdf/catalog/022682-01_c_a4.pdf"),
    ("ダイヤモンドコート 多刃 ダイヤ目工具・2/4枚刃スクエア", "https://www.uniontool.co.jp/assets/pdf/catalog/DCDRSY_DCESY2000_DCESY4000_jp.pdf"),
    ("UTコート・DLCコート4枚刃 小径ねじ切り工具", "https://www.uniontool.co.jp/assets/pdf/catalog/CTMY_DLC-CTMY_jp.pdf"),
    ("HARDMAXコート 6枚刃 逆段 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/HMSY_jp.pdf"),
    ("DLCコート超硬ドリル/ノンコート超硬ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_umd_ty_jp.pdf"),
    ("UDCコート 2枚刃 ロング溝長ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_udclxy_jp.pdf"),
    ("UTコート2枚刃 フラットドリルφ3シャンク", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_utdf-ty_jp.pdf"),
    ("DLCコート 4枚刃 高能率縦横送り スクエアエンドミル（部品加工用）", "https://www.uniontool.co.jp/assets/pdf/catalog/DLCZS-TY.jp.pdf"),
]


UNION_TOOL_CATALOG_ITEMS_OFFICIAL = [
    ("超硬エンドミル総合カタログ vol.22", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vol22_jp.pdf"),
    ("鉄鋼用ドリルシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_tungsten.pdf"),
    ("エコノミーシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_economy.pdf"),
    ("Φ3シャンクVシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vseriescatalog.pdf"),
    ("Φ3シャンクVシリーズ 4枚刃 高硬度用ロングネックラジアスエンドミル VHGLRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_vseries_vhglrs.pdf"),
    ("UTCOAT 2枚刃 ロングネックラジアスエンドミル CLRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_clrs.pdf"),
    ("DLCCOAT 2枚刃/4枚刃 銅電極加工用スクエアエンドミル DLCES2000/4000", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlces2000-4000.pdf"),
    ("HMGCOAT 6枚刃 高硬度材加工用スクエアエンドミル HGS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hgs.pdf"),
    ("2枚刃ボールエンドミル HWB/HWB-S/CWB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hwb_hwb-s_cwb.pdf"),
    ("2枚刃 ロングネックボール HGLB/HWLB/HWLB-S/CWLB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hwlb_hwlb-s_cwlb_hglb.pdf"),
    ("UDC 2枚刃 高靭性超硬合金加工用ボール・ロングネックボール UDCSB/UDCSLB", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_udcsb_udcslb.pdf"),
    ("4枚刃 ロングネックボールエンドミル CBN-LBF4000", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn-lbf4000.pdf"),
    ("DLCCOAT 3枚刃 アルミ加工用チップブレーカ付スクエアエンドミル DLC-ALES", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlc-ales.pdf"),
    ("部品加工用総合リーフレット", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_processing.pdf"),
    ("UTCOAT 4枚刃高能率スクエアエンドミル CEHS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cehs.pdf"),
    ("6枚刃/10枚刃超硬合金・硬脆材加工用荒加工専用ロングネックラジアスエンドミル UDCRRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_udcrrs.pdf"),
    ("UDC 超硬合金・硬脆材用シリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/UDC catalog_vol.13_jp.pdf"),
    ("CBN 4枚刃 ハイグレードロングネックラジアスエンドミル CBN-LRF4000", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn-lrf4000.pdf"),
    ("DLCCOAT銅電極用シリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_dlclb_202304.pdf"),
    ("HMGCOAT 高硬度用4枚刃ロングネックラジアスエンドミル HGLRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_hglrs_2023_04.pdf"),
    ("HMGCOAT 5枚刃/6枚刃 高硬度材加工用高能率ロングネックラジアスエンドミル HGRRS", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_em_hgrrs_2023_04.pdf"),
    ("CBNシリーズ", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_cbn_vol9_2111.pdf"),
    ("UTCOAT 高能率仕上げ加工用 バレルエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/em_barrel_vol2.pdf"),
    ("HARDMAX 2・3枚刃テーパネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_htnb_hftnb_01.pdf"),
    ("HMGCOAT 高硬度用ボール・ロングネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/catalog_em-hgb_vol6.pdf"),
    ("HARDMAX 4枚刃テーパネックラジアス", "https://www.uniontool.co.jp/assets/pdf/catalog/em-htnrs_vol3.pdf"),
    ("HARDMAX 4・6枚刃ラジアス", "https://www.uniontool.co.jp/assets/pdf/catalog/em-hmers.pdf"),
    ("HARDMAX 3枚刃テーパネックボール", "https://www.uniontool.co.jp/assets/pdf/catalog/em-hftnb.pdf"),
    ("DLCコート 1枚刃 アルミサッシ用 スクエアエンドミル DLCAL35Y", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlcal35y.pdf"),
    ("UTコート 3枚刃 球形状ボールエンドミル C-CQBLY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-cqbly.pdf"),
    ("UTコート 2枚刃平面仕上げ加工用スクエアエンドミル C-CSMY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-csmy.pdf"),
    ("5枚刃面取り加工用エンドミル CSVY・HSVY・DLCSVY", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-csvy_hsvy_dlcsvy.pdf"),
    ("UTコート 2枚刃 球形状ボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_c-cqby_jp.pdf"),
    ("DLCコート 1枚刃 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlccps22y_jp.pdf"),
    ("DLCコート 2枚刃 ロングネックラジアスエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlclrsy_jp.pdf"),
    ("UTコート 3枚刃 ボールエンドミル/ロングシャンクボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_cfby_jp.pdf"),
    ("UTコート 2枚刃スレッドミル/DLCコート 2枚刃 スレッドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_ctmy_jp.pdf"),
    ("DLCコート 3枚刃 ボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlccfby_jp.pdf"),
    ("DLCコート 2枚刃 フラットドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_dlcdfy_jp.pdf"),
    ("UDCコート2枚刃 ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_udcmxy_jp.pdf"),
    ("HARDMAX 2枚刃 ロングネックボール/ショートシャンクロングネックボールエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_hlb_hlb-s_jp.pdf"),
    ("DLCコート 2枚刃 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/022681-01_d-d_a4.pdf"),
    ("DLCコート 3枚刃 アルミ加工用ロングネックスクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/022683-01_d-a_a4.pdf"),
    ("UTコート 2枚刃 2段角センタードリル", "https://www.uniontool.co.jp/assets/pdf/catalog/022682-01_c_a4.pdf"),
    ("ダイヤモンドコート 多刃 ダイヤ目工具・2/4枚刃スクエア", "https://www.uniontool.co.jp/assets/pdf/catalog/DCDRSY_DCESY2000_DCESY4000_jp.pdf"),
    ("UTコート・DLCコート4枚刃 小径ねじ切り工具", "https://www.uniontool.co.jp/assets/pdf/catalog/CTMY_DLC-CTMY_jp.pdf"),
    ("HARDMAXコート 6枚刃 逆段 スクエアエンドミル", "https://www.uniontool.co.jp/assets/pdf/catalog/HMSY_jp.pdf"),
    ("DLCコート超硬ドリル/ノンコート超硬ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_umd_ty_jp.pdf"),
    ("UDCコート 2枚刃 ロング溝長ドリル", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_udclxy_jp.pdf"),
    ("UTコート2枚刃 フラットドリルφ3シャンク", "https://www.uniontool.co.jp/assets/pdf/catalog/endmill_sp_utdf-ty_jp.pdf"),
    ("DLCコート 4枚刃 高能率縦横送り スクエアエンドミル（部品加工用）", "https://www.uniontool.co.jp/assets/pdf/catalog/DLCZS-TY.jp.pdf"),
]


OSG_CATALOG_ITEMS = [
    ("超硬防振型エンドミルAE-VMシリーズ", "https://www.osg.co.jp/media_dl/flier/file/n_115.pdf"),
    ("超硬防振型エンドミル自動旋盤対応型AE-VTSS", "https://www.osg.co.jp/media_dl/flier/file/n_134.pdf"),
    ("非鉄用DLCエンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_132.pdf"),
    ("銅電極用DLC超硬エンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_133.pdf"),
    ("高硬度鋼用エンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_130.pdf"),
    ("2枚刃CBNボールエンドミルCBN-FB2", "https://www.osg.co.jp/media_dl/flier/file/n_140.pdf"),
    ("スーパーエンプラ用DLC超硬エンドミルSEP-EL", "https://www.osg.co.jp/media_dl/flier/file/n_138.pdf"),
    ("アディティブ・マニュファクチャリング用エンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_125.pdf"),
    ("仕上げ用異形工具VU-Rシリーズ", "https://www.osg.co.jp/media_dl/flier/file/c_93.pdf"),
    ("フェニックスエンドミルPHX", "https://www.osg.co.jp/media_dl/flier/file/n_72.pdf"),
    ("WXL/WXSエンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_100.pdf"),
    ("セラミックエンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_121.pdf"),
    ("チタン合金加工用エンドミルUVX-TI・HFC-TI", "https://www.osg.co.jp/media_dl/flier/file/n_107.pdf"),
    ("アルミニウム高速加工用エンドミルAERO", "https://www.osg.co.jp/media_dl/flier/file/n_106.pdf"),
    ("インペラ・タービンブレード用超硬テーパボールエンドミルIB-TPBT", "https://www.osg.co.jp/media_dl/flier/file/n_127.pdf"),
    ("サイレントラフィングエンドミル", "https://www.osg.co.jp/media_dl/flier/file/n_101.pdf"),
    ("ハイプロ面取り工具", "https://www.osg.co.jp/media_dl/flier/file/h_29.pdf"),
]


OSG_CATALOG_METADATA = {
    "超硬防振型エンドミルAE-VMシリーズ": {
        "series_codes": "AE-VM, AE-VMSS, AE-VMS, AE-VMSX, AE-VML, AE-VMFE",
        "coating": "DUARISE",
        "material_hint": "汎用/炭素鋼/合金鋼/ステンレス/チタン",
    },
    "超硬防振型エンドミル自動旋盤対応型AE-VTSS": {
        "series_codes": "AE-VTSS",
        "coating": "DUARISE",
        "material_hint": "自動旋盤/小径加工",
    },
    "非鉄用DLCエンドミル": {
        "series_codes": "DLC",
        "coating": "DLC",
        "material_hint": "非鉄/アルミ/銅",
    },
    "銅電極用DLC超硬エンドミル": {
        "series_codes": "DLC",
        "coating": "DLC",
        "material_hint": "銅電極",
    },
    "高硬度鋼用エンドミル": {
        "series_codes": "高硬度鋼用",
        "coating": "",
        "material_hint": "高硬度鋼",
    },
    "2枚刃CBNボールエンドミルCBN-FB2": {
        "series_codes": "CBN-FB2",
        "coating": "CBN",
        "material_hint": "高硬度鋼/仕上げ",
    },
    "スーパーエンプラ用DLC超硬エンドミルSEP-EL": {
        "series_codes": "SEP-EL",
        "coating": "DLC",
        "material_hint": "スーパーエンプラ/樹脂",
    },
    "アディティブ・マニュファクチャリング用エンドミル": {
        "series_codes": "AM",
        "coating": "",
        "material_hint": "積層造形/AM",
    },
    "仕上げ用異形工具VU-Rシリーズ": {
        "series_codes": "VU-R",
        "coating": "",
        "material_hint": "仕上げ/異形",
    },
    "フェニックスエンドミルPHX": {
        "series_codes": "PHX",
        "coating": "",
        "material_hint": "汎用",
    },
    "WXL/WXSエンドミル": {
        "series_codes": "WXL, WXS",
        "coating": "WXL/WXS",
        "material_hint": "汎用/高精度",
    },
    "セラミックエンドミル": {
        "series_codes": "CERAMIC",
        "coating": "セラミック",
        "material_hint": "耐熱合金",
    },
    "チタン合金加工用エンドミルUVX-TI・HFC-TI": {
        "series_codes": "UVX-TI, HFC-TI",
        "coating": "",
        "material_hint": "チタン合金",
    },
    "アルミニウム高速加工用エンドミルAERO": {
        "series_codes": "AERO",
        "coating": "",
        "material_hint": "アルミ",
    },
    "インペラ・タービンブレード用超硬テーパボールエンドミルIB-TPBT": {
        "series_codes": "IB-TPBT",
        "coating": "",
        "material_hint": "インペラ/タービンブレード",
    },
    "サイレントラフィングエンドミル": {
        "series_codes": "SILENT ROUGHING",
        "coating": "",
        "material_hint": "ラフィング/荒加工",
    },
    "ハイプロ面取り工具": {
        "series_codes": "HY-PRO",
        "coating": "",
        "material_hint": "面取り",
    },
}


def infer_catalog_tool_type(name: str) -> str:
    if "ドリル" in name or "センタードリル" in name:
        return "DRILL"
    if "スレッドミル" in name or "ねじ切り" in name or "タップ" in name:
        return "THREAD"
    if "ボール" in name:
        return "BALL"
    if "ラジアス" in name:
        return "RADIUS"
    if "バレル" in name:
        return "BARREL"
    if "面取り" in name:
        return "CHAMFER"
    if "スクエア" in name or "エンドミル" in name:
        return "SQUARE"
    return "CATALOG"


def infer_catalog_coating(name: str) -> str:
    for coating in ["DLCCOAT", "DLCコート", "UTCOAT", "UTコート", "HMGCOAT", "HARDMAX", "UDC", "CBN", "ダイヤモンドコート"]:
        if coating in name:
            return coating
    if "ノンコート" in name:
        return "ノンコート"
    return ""


def infer_material_hint(name: str) -> str:
    hints = []
    if "アルミ" in name:
        hints.append("アルミ")
    if "銅電極" in name or "銅" in name:
        hints.append("銅電極")
    if "高硬度" in name:
        hints.append("高硬度材")
    if "超硬合金" in name or "硬脆材" in name:
        hints.append("超硬合金/硬脆材")
    if "鉄鋼" in name:
        hints.append("鉄鋼")
    return "/".join(dict.fromkeys(hints))


def infer_series_codes(name: str) -> str:
    codes = re.findall(r"\b[A-Z][A-Z0-9-]{2,}(?:/[A-Z][A-Z0-9-]{2,})*", name)
    split_codes: list[str] = []
    for code in codes:
        split_codes.extend(part for part in code.split("/") if part)
    return ", ".join(dict.fromkeys(split_codes))


def infer_flute_info(name: str) -> str:
    matches = re.findall(r"(\d+)\s*枚刃", name)
    return "/".join(dict.fromkeys(matches))


def seed_union_tool_catalogs(conn: sqlite3.Connection) -> None:
    rows = []
    conn.execute(
        "DELETE FROM manufacturer_catalogs WHERE manufacturer = ? OR source_url = ?",
        ("ユニオンツール", UNION_TOOL_SOURCE_URL),
    )
    for product_name, catalog_url in UNION_TOOL_CATALOG_ITEMS_OFFICIAL:
        rows.append(
            (
                "ユニオンツール",
                product_name,
                infer_catalog_tool_type(product_name),
                infer_flute_info(product_name),
                infer_catalog_coating(product_name),
                infer_material_hint(product_name),
                infer_series_codes(product_name),
                catalog_url,
                UNION_TOOL_SOURCE_URL,
                "公式カタログページ掲載のシリーズ/リーフレット情報。径・有効長はPDF本文で確認が必要。",
            )
        )
    conn.executemany(
        """
        INSERT INTO manufacturer_catalogs
        (manufacturer, product_name, tool_type, flute_info, coating, material_hint,
         series_codes, catalog_url, source_url, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def seed_osg_catalogs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM manufacturer_catalogs WHERE manufacturer = ? OR source_url = ?",
        ("OSG", OSG_SOURCE_URL),
    )
    rows = []
    for product_name, catalog_url in OSG_CATALOG_ITEMS:
        metadata = OSG_CATALOG_METADATA.get(product_name, {})
        rows.append(
            (
                "OSG",
                product_name,
                infer_catalog_tool_type(product_name),
                infer_flute_info(product_name),
                metadata.get("coating") or infer_catalog_coating(product_name),
                metadata.get("material_hint") or infer_material_hint(product_name),
                metadata.get("series_codes") or infer_series_codes(product_name),
                catalog_url,
                OSG_SOURCE_URL,
                "OSG公式製品カタログページ掲載のエンドミルPDF。シリーズ名・用途を登録済み。径・有効長・切削条件はPDF本文で確認。",
            )
        )
    conn.executemany(
        """
        INSERT INTO manufacturer_catalogs
        (manufacturer, product_name, tool_type, flute_info, coating, material_hint,
         series_codes, catalog_url, source_url, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def seed_union_tool_cutting_conditions(conn: sqlite3.Connection) -> None:
    paths = sorted((BASE_DIR / "data").glob("*_cutting_conditions.csv"))
    rows = []
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows.extend(
                (
                    row["manufacturer"],
                    row["series_code"],
                    row["product_name"],
                    row["tool_type"],
                    row["model_family"],
                    float(row["outside_diameter_mm"]),
                    row["corner_radius_label"],
                    float(row["effective_length_mm"]),
                    row["work_material"],
                    row["hardness"],
                    row["material_group"],
                    int(float(row["spindle_rpm"])),
                    float(row["feed_rate_mm_min"]),
                    float(row["axial_depth_mm"]),
                    float(row["radial_depth_mm"]),
                    row["source_url"],
                    int(row["source_page"]) if row["source_page"] else None,
                    row["memo"],
                )
                for row in reader
            )
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO manufacturer_cutting_conditions
        (manufacturer, series_code, product_name, tool_type, model_family,
         outside_diameter_mm, corner_radius_label, effective_length_mm,
         work_material, hardness, material_group, spindle_rpm, feed_rate_mm_min,
         axial_depth_mm, radial_depth_mm, source_url, source_page, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def ensure_catalog_tool_master(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0] > 0:
        return

    rows = conn.execute(
        """
        SELECT *
        FROM manufacturer_cutting_conditions
        ORDER BY manufacturer, series_code, model_family, outside_diameter_mm,
                 effective_length_mm, condition_id
        """
    ).fetchall()
    if not rows:
        seed_master(conn)
        return

    tool_ids: dict[tuple[Any, ...], int] = {}
    for row in rows:
        tool_type = "EM" if row["tool_type"] in {"SQUARE", "RADIUS", "BALL"} else row["tool_type"]
        key = (
            row["manufacturer"],
            row["series_code"],
            row["model_family"],
            float(row["outside_diameter_mm"]),
            float(row["effective_length_mm"]),
            row["corner_radius_label"],
        )
        if key in tool_ids:
            continue
        tool_name = (
            f'{row["manufacturer"]} {row["series_code"]} {row["model_family"]} '
            f'φ{float(row["outside_diameter_mm"]):g} {row["corner_radius_label"]}'
        )
        cur = conn.execute(
            """
            INSERT INTO tools
            (tool_name, tool_type, diameter_mm, flute_count, max_depth_mm,
             material, roughing, finishing, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_name,
                tool_type,
                float(row["outside_diameter_mm"]),
                4,
                float(row["effective_length_mm"]),
                row["work_material"],
                1,
                1,
                f'メーカーPDF条件から自動生成: {row["source_url"]} p.{row["source_page"] or "-"}',
            ),
        )
        tool_ids[key] = int(cur.lastrowid)

    condition_rows = []
    for row in rows:
        key = (
            row["manufacturer"],
            row["series_code"],
            row["model_family"],
            float(row["outside_diameter_mm"]),
            float(row["effective_length_mm"]),
            row["corner_radius_label"],
        )
        process_type = "ポケット" if row["tool_type"] in {"SQUARE", "RADIUS", "BALL"} else row["tool_type"]
        condition_rows.append(
            (
                tool_ids[key],
                row["work_material"],
                process_type,
                int(row["spindle_rpm"]),
                float(row["feed_rate_mm_min"]),
                float(row["axial_depth_mm"]),
                float(row["radial_depth_mm"]),
                8,
            )
        )
    conn.executemany(
        """
        INSERT INTO cutting_conditions
        (tool_id, material_type, process_type, spindle_rpm, feed_rate_mm_min,
         depth_of_cut_mm, width_of_cut_mm, tool_change_sec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        condition_rows,
    )


@dataclass
class Feature:
    feature_type: str
    dimensions: str
    quantity: int
    tool_id: int | None
    tool_name: str
    process_type: str
    machining_sec: float
    note: str
    cutting_condition: str = ""
    path_plan: str = ""
    selection_reason: str = ""


def fmt_number(value: Any, digits: int = 2) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def master_condition_summary(condition: sqlite3.Row | None) -> str:
    if condition is None:
        return "-"
    return (
        f'rpm {int(condition["spindle_rpm"]):,} / '
        f'F {fmt_number(condition["feed_rate_mm_min"])} mm/min / '
        f'ap {fmt_number(condition["depth_of_cut_mm"])} mm / '
        f'ae {fmt_number(condition["width_of_cut_mm"])} mm'
    )


def catalog_condition_summary(condition: sqlite3.Row | None) -> str:
    if condition is None:
        return "-"
    material = " ".join(
        str(condition[key] or "")
        for key in ("work_material", "hardness")
        if condition[key]
    )
    return (
        f'rpm {int(condition["spindle_rpm"]):,} / '
        f'F {fmt_number(condition["feed_rate_mm_min"])} mm/min / '
        f'ap {fmt_number(condition["axial_depth_mm"])} mm / '
        f'ae {fmt_number(condition["radial_depth_mm"])} mm'
        + (f' / {material}' if material else "")
        + (f' / p.{condition["source_page"]}' if condition["source_page"] else "")
    )


def tool_limit_summary(max_tool_diameter_mm: float | None) -> str:
    if max_tool_diameter_mm is None or max_tool_diameter_mm <= 0:
        return "最大工具径制限なし"
    return f"最大工具径 {fmt_number(max_tool_diameter_mm)} mm 以下"


def internal_tool_selection_reason(
    tool: sqlite3.Row,
    target_diameter: float | None,
    max_tool_diameter_mm: float | None,
    process_hint: str,
) -> str:
    target = f"目標径 φ{fmt_number(target_diameter)}" if target_diameter else "最大径候補"
    depth = f"有効深さ {fmt_number(tool['max_depth_mm'])} mm"
    return (
        f"{process_hint}: 社内工具マスタから{target}に近い "
        f"φ{fmt_number(tool['diameter_mm'])} を選定。{depth}、{tool_limit_summary(max_tool_diameter_mm)}。"
    )


def catalog_tool_selection_reason(
    condition: sqlite3.Row,
    target_diameter: float,
    required_depth: float,
    max_tool_diameter_mm: float | None,
    process_hint: str,
) -> str:
    material = " ".join(
        str(condition[key] or "")
        for key in ("work_material", "hardness")
        if condition[key]
    )
    return (
        f"{process_hint}: メーカー切削条件から材質候補 {material or '-'}、"
        f"目標径 φ{fmt_number(target_diameter)}、必要深さ {fmt_number(required_depth)} mm に近い "
        f"{condition['manufacturer']} {condition['series_code']} "
        f"φ{fmt_number(condition['outside_diameter_mm'])} "
        f"有効長 {fmt_number(condition['effective_length_mm'])} mm を選定。"
        f"{tool_limit_summary(max_tool_diameter_mm)}、出典 p.{condition['source_page'] or '-'}。"
    )


def condition_params(
    condition: sqlite3.Row | None,
    *,
    catalog: bool = False,
    fallback_feed: float = 100.0,
    fallback_ap: float = 1.0,
    fallback_ae: float = 1.0,
) -> tuple[float, float, float]:
    if condition is None:
        return fallback_feed, fallback_ap, fallback_ae
    if catalog:
        return (
            max(1.0, float(condition["feed_rate_mm_min"])),
            max(0.001, float(condition["axial_depth_mm"])),
            max(0.001, float(condition["radial_depth_mm"])),
        )
    return (
        max(1.0, float(condition["feed_rate_mm_min"])),
        max(0.001, float(condition["depth_of_cut_mm"])),
        max(0.001, float(condition["width_of_cut_mm"])),
    )


def path_time_sec(
    cutting_length_mm: float,
    feed_mm_min: float,
    *,
    approach_count: int = 0,
    approach_mm: float = 5.0,
    rapid_feed_mm_min: float = 8000.0,
    efficiency: float = 0.82,
) -> float:
    cutting_sec = max(0.0, cutting_length_mm) / max(1.0, feed_mm_min) * 60
    approach_sec = max(0, approach_count) * max(0.0, approach_mm) / max(1.0, rapid_feed_mm_min) * 60
    return cutting_sec / max(0.1, efficiency) + approach_sec


def path_plan_summary(
    cutting_length_mm: float,
    pass_count: int,
    approach_count: int,
    *,
    method: str,
    extra: str = "",
) -> str:
    parts = [
        method,
        f"切削距離 {fmt_number(max(0.0, cutting_length_mm), 1)} mm",
        f"パス {max(1, int(pass_count))}",
    ]
    if approach_count:
        parts.append(f"進入/退避 {approach_count}回")
    if extra:
        parts.append(extra)
    return " / ".join(parts)


def significant_volume_threshold(bbox: dict[str, float]) -> float:
    return max(100.0, float(bbox["x"]) * float(bbox["y"]) * 0.01)


def roughing_width_for_plan(width_mm: float, tool_diameter_mm: float, ratio: float = 0.35) -> float:
    diameter = max(0.1, float(tool_diameter_mm))
    planned = max(float(width_mm), diameter * ratio, 0.5)
    return min(planned, diameter * 0.8)


def axial_depth_for_plan(depth_mm: float, tool_diameter_mm: float, required_depth_mm: float, ratio: float = 1.0) -> float:
    required = max(0.5, float(required_depth_mm))
    diameter = max(0.1, float(tool_diameter_mm))
    planned = max(float(depth_mm), min(required, diameter * ratio), 0.5)
    return min(planned, required)


def feature_tool_change_names(feature: Feature) -> list[str]:
    if feature.process_type == "補正" or feature.tool_name == "補正":
        return []
    return [name.strip() for name in feature.tool_name.split(" + ") if name.strip()]


SAFETY_PROFILES = {
    "standard": {
        "label": "標準",
        "roughing": 0.12,
        "finishing": 0.25,
        "hole": 0.08,
        "small_tool": 0.08,
        "positioning_sec_per_feature": 18,
        "direction_setup_sec": 0,
    },
    "cautious": {
        "label": "慎重",
        "roughing": 0.32,
        "finishing": 0.65,
        "hole": 0.18,
        "small_tool": 0.18,
        "positioning_sec_per_feature": 40,
        "direction_setup_sec": 12 * 60,
    },
    "conservative": {
        "label": "保守的",
        "roughing": 0.60,
        "finishing": 1.05,
        "hole": 0.32,
        "small_tool": 0.32,
        "positioning_sec_per_feature": 70,
        "direction_setup_sec": 25 * 60,
    },
}


def tool_diameter_from_name(tool_name: str) -> float | None:
    matches = re.findall(r"φ\s*([0-9]+(?:\.[0-9]+)?)", tool_name)
    if not matches:
        return None
    return min(float(value) for value in matches)


def safety_allowance_feature(
    features: list[Feature],
    estimate_mode: str,
    machining_features: dict[str, Any],
    max_tool_diameter_mm: float | None,
) -> Feature | None:
    profile = SAFETY_PROFILES.get(estimate_mode, SAFETY_PROFILES["cautious"])
    roughing_sec = sum(feature.machining_sec for feature in features if "荒取り" in feature.feature_type)
    finishing_sec = sum(
        feature.machining_sec
        for feature in features
        if "仕上げ" in feature.feature_type or "面取り" in feature.feature_type
    )
    hole_sec = sum(feature.machining_sec for feature in features if "穴" in feature.feature_type)
    small_tool_sec = 0.0
    for feature in features:
        diameter = tool_diameter_from_name(feature.tool_name)
        if diameter is not None and diameter <= 6.0:
            small_tool_sec += feature.machining_sec

    side_hole_count = sum(int(group.get("count", 0)) for group in machining_features.get("side_holes") or [])
    direction_setup_sec = float(profile["direction_setup_sec"]) if side_hole_count else 0.0
    if max_tool_diameter_mm is not None and max_tool_diameter_mm <= 6.0:
        direction_setup_sec += 10 * 60

    correction_sec = (
        roughing_sec * float(profile["roughing"])
        + finishing_sec * float(profile["finishing"])
        + hole_sec * float(profile["hole"])
        + small_tool_sec * float(profile["small_tool"])
        + len(features) * float(profile["positioning_sec_per_feature"])
        + direction_setup_sec
    )
    if correction_sec <= 0:
        return None

    detail = (
        f'荒取り {int(float(profile["roughing"]) * 100)}% / '
        f'仕上げ {int(float(profile["finishing"]) * 100)}% / '
        f'穴 {int(float(profile["hole"]) * 100)}% / '
        f'小径工具 {int(float(profile["small_tool"]) * 100)}% / '
        f'位置決め {fmt_number(profile["positioning_sec_per_feature"])}秒x{len(features)}'
    )
    if direction_setup_sec:
        detail += f" / 段取り方向補正 {fmt_number(direction_setup_sec / 60, 1)}分"

    return Feature(
        "見積安全補正",
        f'{profile["label"]}モード / 追加 {seconds_label(correction_sec)}',
        1,
        None,
        "補正",
        "補正",
        correction_sec,
        "CAM未生成、エアカット、測定、位置決め、びびり回避を考慮した安全側補正",
        "-",
        detail,
        "工具選定ではなく、CAM未生成・段取り・測定・干渉確認などの安全側補正です。",
    )


def parse_step_file(path: Path, blank_allowance_mm: float) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    entity_count = len(re.findall(r"^#\d+\s*=", text, flags=re.MULTILINE))
    face_count = len(re.findall(r"ADVANCED_FACE|FACE_BOUND", text, flags=re.IGNORECASE))
    plane_count = len(re.findall(r"\bPLANE\s*\(", text, flags=re.IGNORECASE))
    cylindrical_radii = [
        float(match.group(1))
        for match in re.finditer(r"CYLINDRICAL_SURFACE\s*\([^,]+,\s*#[0-9]+,\s*([0-9.+\-Ee]+)", text, flags=re.IGNORECASE)
    ]
    point_values = re.findall(
        r"CARTESIAN_POINT\s*\([^,]*,\s*\(\s*([0-9.+\-Ee]+)\s*,\s*([0-9.+\-Ee]+)\s*,\s*([0-9.+\-Ee]+)\s*\)\s*\)",
        text,
        flags=re.IGNORECASE,
    )
    points = [(float(x), float(y), float(z)) for x, y, z in point_values[:20000]]
    if points:
        xs, ys, zs = zip(*points)
        x_len = max(xs) - min(xs)
        y_len = max(ys) - min(ys)
        z_len = max(zs) - min(zs)
    else:
        scale = max(40.0, min(260.0, math.sqrt(max(path.stat().st_size, 1)) * 0.9))
        x_len, y_len, z_len = scale, scale * 0.65, scale * 0.35

    x_len = max(1.0, x_len + blank_allowance_mm * 2)
    y_len = max(1.0, y_len + blank_allowance_mm * 2)
    z_len = max(1.0, z_len + blank_allowance_mm * 2)
    small_radii = [r for r in cylindrical_radii if 1.0 <= r <= 20.0]

    analysis = {
        "entity_count": entity_count,
        "face_count": face_count,
        "plane_count": plane_count,
        "cylindrical_radii": small_radii,
        "bbox": {"x": x_len, "y": y_len, "z": z_len},
        "points_detected": len(points),
        "parser": "STEPテキスト解析",
    }
    brep_analysis = parse_step_brep(path, blank_allowance_mm)
    if brep_analysis:
        analysis.update(brep_analysis)
    return analysis


def parse_step_brep(path: Path, blank_allowance_mm: float) -> dict[str, Any] | None:
    if os.environ.get("ENABLE_BREP_ANALYSIS", "1") != "1":
        return None

    try:
        import cadquery as cq  # type: ignore
    except Exception:
        return None

    try:
        imported = cq.importers.importStep(str(path))
        candidates = list(getattr(imported, "objects", []) or [])
        try:
            candidates.append(imported.val())
        except Exception:
            pass
        shape = next((item for item in candidates if hasattr(item, "BoundingBox") and hasattr(item, "Faces")), None)
        if shape is None:
            raise RuntimeError("B-Repソリッドを取得できませんでした")
        bbox = shape.BoundingBox()
        bounds = {
            "xmin": float(bbox.xmin),
            "xmax": float(bbox.xmax),
            "ymin": float(bbox.ymin),
            "ymax": float(bbox.ymax),
            "zmin": float(bbox.zmin),
            "zmax": float(bbox.zmax),
        }
        raw_x = float(bbox.xlen)
        raw_y = float(bbox.ylen)
        raw_z = float(bbox.zlen)
        part_volume = max(0.0, float(shape.Volume()))
        stock_x = max(1.0, raw_x + blank_allowance_mm * 2)
        stock_y = max(1.0, raw_y + blank_allowance_mm * 2)
        stock_z = max(1.0, raw_z + blank_allowance_mm * 2)
        stock_volume = stock_x * stock_y * stock_z
        raw_box_volume = max(0.0, raw_x * raw_y * raw_z)
        total_removal_volume = max(0.0, stock_volume - part_volume)
        outer_allowance_volume = max(0.0, stock_volume - raw_box_volume)
        internal_removal_volume = max(0.0, total_removal_volume - outer_allowance_volume)

        faces = shape.Faces()
        cylindrical_faces: list[dict[str, Any]] = []
        conical_faces: list[dict[str, Any]] = []
        torus_faces: list[dict[str, Any]] = []
        face_type_counts: dict[str, int] = {}
        for face in faces:
            try:
                geom_type = face.geomType()
            except Exception:
                continue
            face_type_counts[geom_type] = face_type_counts.get(geom_type, 0) + 1
            if geom_type == "CYLINDER":
                try:
                    cylinder = face._geomAdaptor().Cylinder()
                    radius = float(cylinder.Radius())
                    direction = cylinder.Axis().Direction()
                    axis = (float(direction.X()), float(direction.Y()), float(direction.Z()))
                    center = tuple(float(value) for value in face.Center().toTuple())
                    area = float(face.Area())
                except Exception:
                    continue
                if radius <= 0:
                    continue
                estimated_depth = area / max(0.000001, 2 * math.pi * radius)
                cylindrical_faces.append(
                    {
                        "radius": radius,
                        "diameter": radius * 2,
                        "area": area,
                        "estimated_depth": estimated_depth,
                        "axis": axis,
                        "center": center,
                    }
                )
            elif geom_type == "CONE":
                try:
                    cone = face._geomAdaptor().Cone()
                    direction = cone.Axis().Direction()
                    axis = (float(direction.X()), float(direction.Y()), float(direction.Z()))
                    center = tuple(float(value) for value in face.Center().toTuple())
                    area = float(face.Area())
                    face_bbox = face.BoundingBox()
                    bbox_lengths = {
                        "x": float(face_bbox.xlen),
                        "y": float(face_bbox.ylen),
                        "z": float(face_bbox.zlen),
                    }
                    ref_radius = abs(float(cone.RefRadius()))
                    semi_angle = abs(float(cone.SemiAngle()))
                except Exception:
                    continue
                axis_name = axis_label(axis)
                axis_extent = bbox_lengths[axis_name.lower()]
                radial_extent = max(value for key, value in bbox_lengths.items() if key != axis_name.lower())
                conical_faces.append(
                    {
                        "ref_radius": ref_radius,
                        "diameter": max(ref_radius * 2, radial_extent),
                        "area": area,
                        "depth": axis_extent,
                        "semi_angle": semi_angle,
                        "axis": axis,
                        "axis_label": axis_name,
                        "center": center,
                    }
                )
            elif geom_type == "TORUS":
                try:
                    torus = face._geomAdaptor().Torus()
                    direction = torus.Axis().Direction()
                    axis = (float(direction.X()), float(direction.Y()), float(direction.Z()))
                    center = tuple(float(value) for value in face.Center().toTuple())
                    area = float(face.Area())
                    face_bbox = face.BoundingBox()
                    bbox_lengths = {
                        "x": float(face_bbox.xlen),
                        "y": float(face_bbox.ylen),
                        "z": float(face_bbox.zlen),
                    }
                    major_radius = float(torus.MajorRadius())
                    minor_radius = float(torus.MinorRadius())
                except Exception:
                    continue
                if minor_radius <= 0:
                    continue
                torus_faces.append(
                    {
                        "major_radius": major_radius,
                        "minor_radius": minor_radius,
                        "area": area,
                        "axis": axis,
                        "axis_label": axis_label(axis),
                        "center": center,
                        "bbox": bbox_lengths,
                    }
                )

        machining_features = classify_brep_machining_features(
            cylindrical_faces,
            conical_faces,
            torus_faces,
            bounds,
            {"x": raw_x, "y": raw_y, "z": raw_z},
            internal_removal_volume,
            face_type_counts,
        )

        return {
            "parser": "OpenCascade B-Rep解析 + STEPテキスト補助",
            "brep_available": True,
            "solid_count": len(shape.Solids()),
            "edge_count": len(shape.Edges()),
            "face_count": len(faces),
            "plane_count": face_type_counts.get("PLANE", 0),
            "face_type_counts": face_type_counts,
            "bbox": {"x": stock_x, "y": stock_y, "z": stock_z},
            "raw_bbox": {"x": raw_x, "y": raw_y, "z": raw_z},
            "raw_bounds": bounds,
            "part_volume_mm3": part_volume,
            "stock_volume_mm3": stock_volume,
            "removal_volume_mm3": total_removal_volume,
            "outer_allowance_volume_mm3": outer_allowance_volume,
            "internal_removal_volume_mm3": internal_removal_volume,
            "cylindrical_radii": [item["radius"] for item in cylindrical_faces if 1.0 <= item["radius"] <= 20.0],
            "cylindrical_faces": cylindrical_faces[:300],
            "conical_faces": conical_faces[:300],
            "torus_faces": torus_faces[:300],
            "hole_groups": machining_features["holes"],
            "machining_features": machining_features,
        }
    except Exception as exc:
        return {
            "brep_available": False,
            "brep_error": str(exc),
        }


def axis_label(axis: tuple[float, float, float] | list[float]) -> str:
    return max((("X", abs(axis[0])), ("Y", abs(axis[1])), ("Z", abs(axis[2]))), key=lambda item: item[1])[0]


def group_feature_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[item] for item in keys)
        item = grouped.setdefault(
            key,
            {
                **{field: row[field] for field in keys},
                "count": 0,
                "total_depth": 0.0,
                "max_depth": 0.0,
                "total_volume": 0.0,
                "_metric_totals": {},
            },
        )
        count = int(row.get("count", 1))
        item["count"] += count
        depth = float(row.get("depth", row.get("avg_depth", 0.0)))
        item["total_depth"] += depth * count
        item["max_depth"] = max(item["max_depth"], depth)
        item["total_volume"] += float(row.get("volume", 0.0))
        metric_totals = item["_metric_totals"]
        for field, value in row.items():
            if field in keys or field in {"count", "volume", "depth", "avg_depth"}:
                continue
            if isinstance(value, (int, float)):
                metric_totals[field] = metric_totals.get(field, 0.0) + float(value) * count
    result = []
    for item in grouped.values():
        count = max(1, int(item["count"]))
        item["avg_depth"] = item["total_depth"] / count
        metric_totals = item.pop("_metric_totals", {})
        for field, total in metric_totals.items():
            item[field] = total / count
        result.append(item)
    return sorted(result, key=lambda item: tuple(item[field] for field in keys))


def group_surface_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[item] for item in keys)
        item = grouped.setdefault(
            key,
            {
                **{field: row[field] for field in keys},
                "count": 0,
                "total_area": 0.0,
                "total_length": 0.0,
                "total_depth": 0.0,
                "max_depth": 0.0,
            },
        )
        count = int(row.get("count", 1))
        item["count"] += count
        item["total_area"] += float(row.get("area", 0.0)) * count
        item["total_length"] += float(row.get("edge_length", 0.0)) * count
        depth = float(row.get("depth", row.get("avg_depth", 0.0)))
        item["total_depth"] += depth * count
        item["max_depth"] = max(float(item["max_depth"]), depth)
    result = []
    for item in grouped.values():
        count = max(1, int(item["count"]))
        item["avg_depth"] = float(item["total_depth"]) / count
        result.append(item)
    return sorted(result, key=lambda item: tuple(item[field] for field in keys))


def classify_brep_machining_features(
    cylindrical_faces: list[dict[str, Any]],
    conical_faces: list[dict[str, Any]],
    torus_faces: list[dict[str, Any]],
    bounds: dict[str, float],
    raw_bbox: dict[str, float],
    removal_volume: float,
    face_type_counts: dict[str, int],
) -> dict[str, Any]:
    vertical: list[dict[str, Any]] = []
    side: list[dict[str, Any]] = []
    consumed: set[int] = set()

    for index, item in enumerate(cylindrical_faces):
        diameter = round(float(item["diameter"]), 1)
        depth = float(item["estimated_depth"])
        if not (1.5 <= float(item["radius"]) <= 20.0 and depth >= 1.0):
            continue
        center = item["center"]
        label = axis_label(item["axis"])
        row = {
            **item,
            "index": index,
            "axis_label": label,
            "diameter": diameter,
            "depth": depth,
            "depth_ratio": depth / max(diameter, 0.1),
        }
        if label == "Z":
            near_x = min(abs(center[0] - bounds["xmin"]), abs(center[0] - bounds["xmax"])) <= max(diameter, 5.0)
            near_y = min(abs(center[1] - bounds["ymin"]), abs(center[1] - bounds["ymax"])) <= max(diameter, 5.0)
            if near_x and near_y:
                continue
            if depth < 3.0:
                continue
            vertical.append(row)
        else:
            if diameter >= 3.0 and depth >= min(raw_bbox["x"], raw_bbox["y"]) * 0.18:
                side.append(row)

    by_center: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for row in vertical:
        center_key = (round(float(row["center"][0]), 1), round(float(row["center"][1]), 1))
        by_center.setdefault(center_key, []).append(row)

    counterbores = []
    for center_key, rows in by_center.items():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda row: float(row["diameter"]))
        small = rows[0]
        large = rows[-1]
        if large["diameter"] <= small["diameter"] * 1.35:
            continue
        if large["depth"] > small["depth"] * 0.7:
            continue
        consumed.add(int(small["index"]))
        consumed.add(int(large["index"]))
        volume = (
            math.pi * (small["diameter"] / 2) ** 2 * small["depth"]
            + math.pi * (large["diameter"] / 2) ** 2 * large["depth"]
        )
        counterbores.append(
            {
                "through_diameter": small["diameter"],
                "counterbore_diameter": large["diameter"],
                "through_depth": small["depth"],
                "counterbore_depth": large["depth"],
                "center_x": center_key[0],
                "center_y": center_key[1],
                "count": 1,
                "volume": volume,
            }
        )

    slots = []
    remaining_vertical = [row for row in vertical if int(row["index"]) not in consumed]
    for diameter in sorted({row["diameter"] for row in remaining_vertical}):
        rows = [row for row in remaining_vertical if row["diameter"] == diameter and row["depth"] <= raw_bbox["z"] * 0.32]
        used: set[int] = set()
        for i, row in enumerate(rows):
            if int(row["index"]) in used:
                continue
            cx, cy, _ = row["center"]
            pair_index = None
            pair_distance = 0.0
            for j, other in enumerate(rows):
                if i == j or int(other["index"]) in used:
                    continue
                ox, oy, _ = other["center"]
                same_x = abs(cx - ox) <= diameter * 0.45 and abs(cy - oy) >= diameter * 2.0
                same_y = abs(cy - oy) <= diameter * 0.45 and abs(cx - ox) >= diameter * 2.0
                if same_x or same_y:
                    distance = math.hypot(cx - ox, cy - oy)
                    if distance > pair_distance:
                        pair_distance = distance
                        pair_index = j
            if pair_index is None:
                continue
            other = rows[pair_index]
            used.add(int(row["index"]))
            used.add(int(other["index"]))
            consumed.add(int(row["index"]))
            consumed.add(int(other["index"]))
            depth = (row["depth"] + other["depth"]) / 2
            length = pair_distance + diameter
            area = max(0.0, (length - diameter) * diameter + math.pi * (diameter / 2) ** 2)
            slots.append(
                {
                    "width": diameter,
                    "length": round(length, 1),
                    "depth": depth,
                    "count": 1,
                    "volume": area * depth,
                }
            )

    holes = []
    for row in remaining_vertical:
        if int(row["index"]) in consumed:
            continue
        volume = math.pi * (row["diameter"] / 2) ** 2 * row["depth"]
        holes.append(
            {
                "diameter": row["diameter"],
                "axis": "Z",
                "depth": row["depth"],
                "depth_ratio": row["depth_ratio"],
                "center_x": float(row["center"][0]),
                "center_y": float(row["center"][1]),
                "count": 1,
                "volume": volume,
            }
        )

    side_holes_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in side:
        center = row["center"]
        if row["axis_label"] == "Y":
            location_key = (round(center[0] / max(row["diameter"], 1.0)), round(center[2] / max(row["diameter"], 1.0)))
        elif row["axis_label"] == "X":
            location_key = (round(center[1] / max(row["diameter"], 1.0)), round(center[2] / max(row["diameter"], 1.0)))
        else:
            location_key = (round(center[0] / max(row["diameter"], 1.0)), round(center[1] / max(row["diameter"], 1.0)))
        key = (row["axis_label"], row["diameter"], *location_key)
        item = side_holes_by_key.setdefault(
            key,
            {
                "diameter": row["diameter"],
                "axis": row["axis_label"],
                "depth": 0.0,
                "depth_ratio": 0.0,
                "count": 1,
                "volume": 0.0,
            },
        )
        item["depth"] = max(float(item["depth"]), float(row["depth"]))
        item["depth_ratio"] = max(float(item["depth_ratio"]), float(row["depth_ratio"]))
        item["volume"] = math.pi * (row["diameter"] / 2) ** 2 * float(item["depth"])
    side_holes = list(side_holes_by_key.values())

    countersinks = []
    chamfers = []
    for item in conical_faces:
        diameter = round(float(item.get("diameter", 0.0)), 1)
        depth = float(item.get("depth", 0.0))
        if diameter <= 0 or depth <= 0:
            continue
        axis = str(item.get("axis_label") or axis_label(item["axis"]))
        center = item.get("center", (0.0, 0.0, 0.0))
        edge_length = math.pi * diameter
        matched_hole: dict[str, Any] | None = None
        if axis == "Z" and depth <= max(8.0, raw_bbox["z"] * 0.35):
            candidates = []
            for hole in holes:
                hole_diameter = float(hole["diameter"])
                if diameter <= hole_diameter * 1.2:
                    continue
                distance = math.hypot(float(center[0]) - float(hole["center_x"]), float(center[1]) - float(hole["center_y"]))
                if distance <= max(hole_diameter * 0.55, 1.5):
                    candidates.append((distance, hole))
            if candidates:
                matched_hole = min(candidates, key=lambda pair: pair[0])[1]

        if matched_hole is not None:
            hole_diameter = float(matched_hole["diameter"])
            sink_volume = math.pi * depth / 3.0 * (
                (diameter / 2) ** 2
                + (diameter / 2) * (hole_diameter / 2)
                + (hole_diameter / 2) ** 2
            )
            countersinks.append(
                {
                    "hole_diameter": round(hole_diameter, 1),
                    "sink_diameter": diameter,
                    "axis": axis,
                    "depth": depth,
                    "edge_length": edge_length,
                    "count": 1,
                    "volume": sink_volume,
                }
            )
        elif depth <= max(6.0, raw_bbox["z"] * 0.25):
            chamfers.append(
                {
                    "axis": axis,
                    "diameter": diameter,
                    "depth": depth,
                    "edge_length": edge_length,
                    "area": float(item.get("area", 0.0)),
                    "count": 1,
                }
            )

    corner_radii = []
    for item in torus_faces:
        radius = round(float(item.get("minor_radius", 0.0)), 2)
        area = float(item.get("area", 0.0))
        if not (0.2 <= radius <= 8.0 and area > 0):
            continue
        # Most modeled fillets are close to quarter-round faces; this converts surface area
        # into an approximate contour length that a CAM finishing pass would trace.
        edge_length = area / max(0.001, (math.pi / 2.0) * radius)
        corner_radii.append(
            {
                "radius": radius,
                "axis": str(item.get("axis_label") or axis_label(item["axis"])),
                "area": area,
                "edge_length": edge_length,
                "count": 1,
            }
        )

    hole_groups = group_feature_rows(holes, ("diameter", "axis"))
    side_hole_groups = group_feature_rows(side_holes, ("diameter", "axis"))
    counterbore_groups = group_feature_rows(counterbores, ("through_diameter", "counterbore_diameter"))
    countersink_groups = group_feature_rows(countersinks, ("hole_diameter", "sink_diameter", "axis"))
    slot_groups = group_feature_rows(slots, ("width", "length"))
    deep_hole_groups = group_feature_rows(
        [row for row in holes if float(row.get("depth_ratio", 0.0)) >= 5.0 or float(row.get("depth", 0.0)) >= 30.0],
        ("diameter", "axis"),
    )
    chamfer_groups = group_surface_rows(chamfers, ("axis",))
    corner_radius_groups = group_surface_rows(corner_radii, ("radius", "axis"))
    classified_volume = sum(
        item.get("total_volume", 0.0)
        for group in (hole_groups, side_hole_groups, counterbore_groups, countersink_groups, slot_groups)
        for item in group
    )
    roughing_volume = max(0.0, removal_volume - classified_volume)
    finishing_area = max(0.0, 2 * (raw_bbox["x"] + raw_bbox["y"]) * raw_bbox["z"])

    return {
        "holes": hole_groups,
        "side_holes": side_hole_groups,
        "counterbores": counterbore_groups,
        "countersinks": countersink_groups,
        "slots": slot_groups,
        "deep_holes": deep_hole_groups,
        "chamfers": chamfer_groups,
        "corner_radii": corner_radius_groups,
        "roughing_volume_mm3": roughing_volume,
        "classified_feature_volume_mm3": classified_volume,
        "finishing_side_area_mm2": finishing_area,
        "fillet_face_count": face_type_counts.get("TORUS", 0),
        "chamfer_face_count": face_type_counts.get("CONE", 0),
    }


def pick_tool(
    conn: sqlite3.Connection,
    tool_type: str,
    target_diameter: float | None = None,
    max_tool_diameter_mm: float | None = None,
) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM tools WHERE tool_type = ? ORDER BY diameter_mm", (tool_type,)).fetchall()
    if max_tool_diameter_mm is not None and max_tool_diameter_mm > 0:
        rows = [row for row in rows if float(row["diameter_mm"]) <= max_tool_diameter_mm]
    if not rows:
        rows = conn.execute("SELECT * FROM tools ORDER BY diameter_mm").fetchall()
        if max_tool_diameter_mm is not None and max_tool_diameter_mm > 0:
            rows = [row for row in rows if float(row["diameter_mm"]) <= max_tool_diameter_mm]
    if not rows:
        limit = f"（最大工具径 {max_tool_diameter_mm:g} mm 以下）" if max_tool_diameter_mm else ""
        raise RuntimeError(f"使用可能な工具マスタが未登録です{limit}。")
    if target_diameter is None:
        return rows[-1]
    return min(rows, key=lambda row: abs(float(row["diameter_mm"]) - target_diameter))


def condition_for(conn: sqlite3.Connection, tool_id: int, material_type: str, process_hint: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM cutting_conditions
        WHERE tool_id = ? AND material_type = ? AND process_type = ?
        ORDER BY condition_id LIMIT 1
        """,
        (tool_id, material_type, process_hint),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        """
        SELECT * FROM cutting_conditions
        WHERE tool_id = ? AND material_type = ?
        ORDER BY condition_id LIMIT 1
        """,
        (tool_id, material_type),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT * FROM cutting_conditions WHERE tool_id = ? ORDER BY condition_id LIMIT 1",
        (tool_id,),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        """
        SELECT * FROM cutting_conditions
        WHERE material_type = ? AND process_type = ?
        ORDER BY condition_id LIMIT 1
        """,
        (material_type, process_hint),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        """
        SELECT * FROM cutting_conditions
        WHERE material_type = ?
        ORDER BY condition_id LIMIT 1
        """,
        (material_type,),
    ).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM cutting_conditions ORDER BY condition_id LIMIT 1").fetchone()
    if row:
        return row
    raise RuntimeError("切削条件マスタが未登録です")


def manufacturer_condition_for(
    conn: sqlite3.Connection,
    series_code: str,
    material_key: str,
    target_diameter: float,
    max_effective_length: float | None = None,
    condition_id: int | None = None,
) -> sqlite3.Row | None:
    if condition_id:
        row = conn.execute(
            """
            SELECT *
            FROM manufacturer_cutting_conditions
            WHERE condition_id = ?
            """,
            (condition_id,),
        ).fetchone()
        if row is not None:
            return row

    query = """
        SELECT *
        FROM manufacturer_cutting_conditions
        WHERE series_code = ?
          AND work_material = ?
    """
    params: list[Any] = [series_code, material_key]
    if max_effective_length is not None:
        query += " AND effective_length_mm <= ?"
        params.append(max_effective_length)
    query += """
        ORDER BY ABS(outside_diameter_mm - ?), effective_length_mm DESC, condition_id
        LIMIT 1
    """
    params.append(target_diameter)
    row = conn.execute(query, params).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        SELECT *
        FROM manufacturer_cutting_conditions
        WHERE series_code = ?
        ORDER BY ABS(outside_diameter_mm - ?), effective_length_mm DESC, condition_id
        LIMIT 1
        """,
        (series_code, target_diameter),
    ).fetchone()


def material_keywords(material_type: str) -> list[str]:
    text = material_type.upper()
    if "SUS" in text or "ステンレス" in material_type:
        return ["SUS", "SUS304", "STAINLESS"]
    if "アルミ" in material_type or "AL" in text:
        return ["A5052", "A7075", "ALUMINUM", "ALUMINIUM", "ALLOYS", "アルミ"]
    if "銅" in material_type or "COPPER" in text:
        return ["COPPER", "C1100", "銅"]
    return ["S50C", "S45C", "SCM", "SS400", "CARBON STEEL", "ALLOY STEEL", "鋼", "FC"]


def auto_manufacturer_condition_for(
    conn: sqlite3.Connection,
    material_type: str,
    target_diameter: float,
    required_depth: float,
    process_hint: str,
    max_tool_diameter_mm: float | None = None,
) -> sqlite3.Row | None:
    rows = conn.execute("SELECT * FROM manufacturer_cutting_conditions").fetchall()
    if max_tool_diameter_mm is not None and max_tool_diameter_mm > 0:
        rows = [row for row in rows if float(row["outside_diameter_mm"]) <= max_tool_diameter_mm]
    if not rows:
        return None

    keywords = material_keywords(material_type)
    process_text = process_hint.upper()

    def score(row: sqlite3.Row) -> tuple[float, float, int]:
        searchable = " ".join(
            str(row[key] or "")
            for key in ("work_material", "material_group", "product_name", "memo", "tool_type", "series_code")
        ).upper()
        value = 0.0

        if any(keyword.upper() in searchable for keyword in keywords):
            value += 1000
        if material_type in {"鉄", "鋼"} and any(
            word in searchable for word in ("HARDENED", "HRC", "SKD", "STAVAX", "NAK", "HAP")
        ):
            value -= 420
        if material_type in {"鉄", "鋼"} and "STAINLESS" in searchable:
            value -= 520
        if "ポケット" in process_hint or "POCKET" in process_text:
            if any(word in searchable for word in ("POCKET", "TROCHOIDAL", "SLOTTING", "SIDE")):
                value += 120
            removal_rate = (
                float(row["feed_rate_mm_min"])
                * float(row["axial_depth_mm"])
                * float(row["radial_depth_mm"])
            )
            value += min(removal_rate / 50000.0, 1.0) * 260
        elif "側面" in process_hint or "SIDE" in process_text:
            if any(word in searchable for word in ("SIDE", "MILLING", "FINISHING")):
                value += 120

        if row["tool_type"] in {"SQUARE", "RADIUS"}:
            value += 80
        if row["manufacturer"] == "OSG":
            value += 10

        diameter = float(row["outside_diameter_mm"])
        effective_length = float(row["effective_length_mm"])
        value -= abs(diameter - target_diameter) * 18
        value -= max(0.0, required_depth - effective_length) * 22
        value -= max(0.0, effective_length - required_depth) * 0.3
        value += min(float(row["feed_rate_mm_min"]), 3000.0) / 3000.0 * 25
        return (value, -abs(diameter - target_diameter), -int(row["condition_id"]))

    return max(rows, key=score)


def estimate(
    path: Path,
    file_name: str,
    material_type: str,
    blank_allowance_mm: float,
    machine_id: int,
    use_manufacturer_conditions: bool = True,
    estimate_mode: str = "cautious",
) -> dict[str, Any]:
    analysis = parse_step_file(path, blank_allowance_mm)
    bbox = analysis["bbox"]

    with db() as conn:
        ensure_operational_master(conn)
        ensure_catalog_tool_master(conn)
        ensure_operational_master(conn)
        machine = conn.execute("SELECT * FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()
        if machine is None:
            machine = conn.execute("SELECT * FROM machines ORDER BY machine_id LIMIT 1").fetchone()
        if machine is None:
            raise RuntimeError("機械マスタが未登録です")
        rapid_feed = float(machine["rapid_feed_mm_min"])
        max_tool_diameter = float(machine["max_tool_diameter_mm"]) if machine["max_tool_diameter_mm"] else None

        features: list[Feature] = []

        face_tool = pick_tool(conn, "EM", 16, max_tool_diameter)
        face_cond = condition_for(conn, face_tool["tool_id"], material_type, "ポケット")
        face_selection_reason = internal_tool_selection_reason(face_tool, 16, max_tool_diameter, "上面加工")
        top_area = bbox["x"] * bbox["y"]
        face_diameter = float(face_tool["diameter_mm"])
        face_feed, _face_ap, face_ae = condition_params(
            face_cond,
            fallback_feed=float(face_cond["feed_rate_mm_min"]),
            fallback_ap=1.0,
            fallback_ae=max(1.0, face_diameter * 0.45),
        )
        face_pick = roughing_width_for_plan(face_ae, face_diameter, ratio=0.45)
        face_passes = max(1, math.ceil(bbox["y"] / face_pick))
        top_cutting_length = (bbox["x"] + face_diameter) * face_passes
        top_sec = path_time_sec(top_cutting_length, face_feed, approach_count=face_passes * 2, rapid_feed_mm_min=rapid_feed)
        features.append(
            Feature(
                "平面加工（上面）",
                f'{bbox["x"]:.1f} x {bbox["y"]:.1f} mm / 面積 {top_area:.0f} mm2',
                1,
                face_tool["tool_id"],
                face_tool["tool_name"],
                "平面",
                top_sec,
                "上面をエンドミル面走査として概算",
                master_condition_summary(face_cond),
                path_plan_summary(top_cutting_length, face_passes, face_passes * 2, method="面走査"),
                face_selection_reason,
            )
        )

        side_tool = pick_tool(conn, "EM", 16, max_tool_diameter)
        side_cond = condition_for(conn, side_tool["tool_id"], material_type, "ポケット")
        side_catalog_cond = None
        side_target_diameter = 6.0 if min(bbox["x"], bbox["y"]) >= 40 or bbox["z"] >= 12 else 3.0
        if use_manufacturer_conditions:
            side_catalog_cond = auto_manufacturer_condition_for(
                conn,
                material_type,
                side_target_diameter,
                bbox["z"],
                "側面",
                max_tool_diameter,
            )
        side_area = 2 * (bbox["x"] + bbox["y"]) * bbox["z"]
        if side_catalog_cond is not None:
            side_diameter = float(side_catalog_cond["outside_diameter_mm"])
            side_feed, side_ap, side_pick = condition_params(side_catalog_cond, catalog=True)
            side_tool_name = (
                f'{side_catalog_cond["manufacturer"]} {side_catalog_cond["series_code"]} '
                f'φ{side_diameter:g} {side_catalog_cond["corner_radius_label"]}'
            )
            side_note = (
                f'STP形状から自動選定: {side_catalog_cond["work_material"]} '
                f'{side_catalog_cond["hardness"]}, {side_catalog_cond["model_family"]}, rpm {side_catalog_cond["spindle_rpm"]}, '
                f'ap {side_catalog_cond["axial_depth_mm"]}, ae {side_catalog_cond["radial_depth_mm"]}, '
                f'出典 p.{side_catalog_cond["source_page"]}'
            )
            side_condition_text = catalog_condition_summary(side_catalog_cond)
            side_tool_id = None
            side_selection_reason = catalog_tool_selection_reason(
                side_catalog_cond,
                side_target_diameter,
                bbox["z"],
                max_tool_diameter,
                "側面加工",
            )
        else:
            side_diameter = float(side_tool["diameter_mm"])
            side_feed, side_ap, side_ae = condition_params(side_cond, fallback_ae=max(1.0, side_diameter * 0.35))
            side_pick = max(1.0, side_ae)
            side_tool_name = side_tool["tool_name"]
            side_note = "外周側面として概算"
            side_condition_text = master_condition_summary(side_cond)
            side_tool_id = side_tool["tool_id"]
            side_selection_reason = internal_tool_selection_reason(side_tool, 16, max_tool_diameter, "側面加工")
        side_perimeter = 2 * (bbox["x"] + bbox["y"])
        side_plan_ap = axial_depth_for_plan(side_ap, side_diameter, bbox["z"], ratio=1.0)
        side_plan_pick = roughing_width_for_plan(side_pick, side_diameter, ratio=0.22)
        side_axial_passes = max(1, math.ceil(bbox["z"] / max(0.001, side_plan_ap)))
        side_radial_stock = max(blank_allowance_mm, side_plan_pick)
        side_radial_passes = max(1, math.ceil(side_radial_stock / max(0.001, side_plan_pick)))
        side_passes = side_axial_passes * side_radial_passes
        side_cutting_length = side_perimeter * side_passes
        side_sec = path_time_sec(
            side_cutting_length,
            side_feed,
            approach_count=side_passes * 2,
            rapid_feed_mm_min=rapid_feed,
            efficiency=0.78,
        )
        features.append(
            Feature(
                "平面加工（側面）",
                f'周長 {2 * (bbox["x"] + bbox["y"]):.1f} mm / 高さ {bbox["z"]:.1f} mm',
                1,
                side_tool_id,
                side_tool_name,
                "平面",
                side_sec,
                side_note,
                side_condition_text,
                path_plan_summary(
                    side_cutting_length,
                    side_passes,
                    side_passes * 2,
                    method="外周輪郭",
                    extra=f"Z {side_axial_passes}段 x 径 {side_radial_passes}回",
                ),
                side_selection_reason,
            )
        )

        machining_features = analysis.get("machining_features") or {}
        if "holes" in machining_features:
            hole_groups = machining_features.get("holes") or []
        else:
            hole_groups = analysis.get("hole_groups") or []
        if not hole_groups and not machining_features:
            radii = analysis["cylindrical_radii"]
            grouped_holes: dict[float, int] = {}
            for radius in radii:
                diameter = round(radius * 2, 1)
                grouped_holes[diameter] = grouped_holes.get(diameter, 0) + 1
            if not grouped_holes and analysis["face_count"] > 25:
                grouped_holes[6.0] = max(1, min(8, analysis["face_count"] // 18))
            hole_groups = [
                {
                    "diameter": diameter,
                    "count": count,
                    "avg_depth": max(3.0, bbox["z"] * 0.75),
                    "axis": "Z",
                }
                for diameter, count in sorted(grouped_holes.items())
            ]

        for group in hole_groups:
            diameter = float(group["diameter"])
            count = int(group["count"])
            drill = pick_tool(conn, "DRILL", diameter, max_tool_diameter)
            cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            drill_selection_reason = internal_tool_selection_reason(drill, diameter, max_tool_diameter, "穴加工")
            depth = min(float(drill["max_depth_mm"]), max(3.0, float(group.get("avg_depth", bbox["z"] * 0.75))))
            drill_feed, _drill_ap, _drill_ae = condition_params(cond)
            depth_ratio = float(group.get("depth_ratio", depth / max(diameter, 0.1)))
            deep_hole = depth_ratio >= 5.0 or depth >= 30.0
            peck_passes = max(1, math.ceil(depth / max(diameter * 3.0, 1.0)))
            peck_extra = 0.28 if deep_hole else 0.18
            approach_count = count * (peck_passes + (2 if deep_hole else 1))
            drill_cutting_length = depth * count * (1.0 + peck_extra * max(0, peck_passes - 1))
            hole_sec = path_time_sec(
                drill_cutting_length,
                drill_feed,
                approach_count=approach_count,
                approach_mm=min(depth + 5.0, 60.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.82 if deep_hole else 0.9,
            )
            features.append(
                Feature(
                    "穴加工（ドリル）",
                    f"φ{diameter:.1f} / 深さ {depth:.1f} mm / 軸 {group.get('axis', '-')}",
                    count,
                    drill["tool_id"],
                    drill["tool_name"],
                    "穴",
                    hole_sec,
                    (
                        "B-Rep円筒面から深穴候補を抽出し、ペック退避を重めに補正"
                        if deep_hole and analysis.get("brep_available")
                        else "B-Rep円筒面から穴候補を抽出"
                        if analysis.get("brep_available")
                        else "円筒面から穴候補を抽出"
                    ),
                    master_condition_summary(cond),
                    path_plan_summary(
                        drill_cutting_length,
                        peck_passes,
                        approach_count,
                        method="ドリル送り",
                        extra=f"{count}穴" + (f" / L/D {depth_ratio:.1f}" if deep_hole else ""),
                    ),
                    drill_selection_reason,
                )
            )

        for group in machining_features.get("side_holes") or []:
            diameter = float(group["diameter"])
            count = int(group["count"])
            drill = pick_tool(conn, "DRILL", diameter, max_tool_diameter)
            cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            drill_selection_reason = internal_tool_selection_reason(drill, diameter, max_tool_diameter, "横穴加工")
            depth = min(float(drill["max_depth_mm"]), max(3.0, float(group.get("avg_depth", bbox["x"] * 0.5))))
            drill_feed, _drill_ap, _drill_ae = condition_params(cond)
            depth_ratio = float(group.get("depth_ratio", depth / max(diameter, 0.1)))
            deep_hole = depth_ratio >= 5.0 or depth >= 30.0
            peck_passes = max(1, math.ceil(depth / max(diameter * 3.0, 1.0)))
            peck_extra = 0.3 if deep_hole else 0.18
            approach_count = count * (peck_passes + (2 if deep_hole else 1))
            side_hole_cutting_length = depth * count * (1.0 + peck_extra * max(0, peck_passes - 1))
            side_hole_sec = path_time_sec(
                side_hole_cutting_length,
                drill_feed,
                approach_count=approach_count,
                approach_mm=min(depth + 5.0, 80.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.8 if deep_hole else 0.88,
            )
            features.append(
                Feature(
                    "横穴加工（ドリル）",
                    f"φ{diameter:.1f} / 深さ {depth:.1f} mm / 軸 {group.get('axis', '-')}",
                    count,
                    drill["tool_id"],
                    drill["tool_name"],
                    "穴",
                    side_hole_sec,
                    "B-Rep円筒面から横深穴候補を抽出し、ペック退避を重めに補正" if deep_hole else "B-Rep円筒面から側面穴候補を抽出",
                    master_condition_summary(cond),
                    path_plan_summary(
                        side_hole_cutting_length,
                        peck_passes,
                        approach_count,
                        method="横穴ドリル送り",
                        extra=f"{count}穴" + (f" / L/D {depth_ratio:.1f}" if deep_hole else ""),
                    ),
                    drill_selection_reason,
                )
            )

        for group in machining_features.get("counterbores") or []:
            through_diameter = float(group["through_diameter"])
            counterbore_diameter = float(group["counterbore_diameter"])
            count = int(group["count"])
            drill = pick_tool(conn, "DRILL", through_diameter, max_tool_diameter)
            counterbore_tool = pick_tool(conn, "EM", counterbore_diameter, max_tool_diameter)
            drill_cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            counterbore_cond = condition_for(conn, counterbore_tool["tool_id"], material_type, "ポケット")
            counterbore_selection_reason = (
                internal_tool_selection_reason(drill, through_diameter, max_tool_diameter, "座ぐり下穴")
                + " / "
                + internal_tool_selection_reason(counterbore_tool, counterbore_diameter, max_tool_diameter, "座ぐり加工")
            )
            through_depth = min(
                float(drill["max_depth_mm"]),
                max(3.0, float(group.get("through_depth", group.get("avg_depth", bbox["z"] * 0.75)))),
            )
            counterbore_depth = max(0.5, float(group.get("counterbore_depth", group.get("avg_depth", 2.0))))
            drill_feed, _drill_ap, _drill_ae = condition_params(drill_cond)
            counterbore_feed, counterbore_ap, counterbore_ae = condition_params(counterbore_cond)
            counterbore_tool_diameter = float(counterbore_tool["diameter_mm"])
            counterbore_plan_ap = axial_depth_for_plan(counterbore_ap, counterbore_tool_diameter, counterbore_depth, ratio=0.7)
            counterbore_plan_ae = roughing_width_for_plan(counterbore_ae, counterbore_tool_diameter, ratio=0.22)
            drill_pecks = max(1, math.ceil(through_depth / max(through_diameter * 3.0, 1.0)))
            drill_cutting_length = through_depth * count * (1.0 + 0.18 * max(0, drill_pecks - 1))
            drill_sec = path_time_sec(
                drill_cutting_length,
                drill_feed,
                approach_count=count * (drill_pecks + 1),
                approach_mm=min(through_depth + 5.0, 80.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.9,
            )
            counterbore_volume = math.pi * (counterbore_diameter / 2) ** 2 * counterbore_depth * count
            counterbore_depth_passes = max(1, math.ceil(counterbore_depth / max(0.001, counterbore_plan_ap)))
            counterbore_radial_width = max(0.0, (counterbore_diameter - through_diameter) / 2)
            counterbore_radial_passes = max(1, math.ceil(counterbore_radial_width / max(0.001, counterbore_plan_ae)))
            counterbore_passes = counterbore_depth_passes * counterbore_radial_passes
            counterbore_cutting_length = math.pi * counterbore_diameter * counterbore_passes * count
            counterbore_sec = path_time_sec(
                counterbore_cutting_length,
                counterbore_feed,
                approach_count=count * counterbore_passes * 2,
                approach_mm=min(counterbore_depth + 5.0, 40.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.8,
            )
            features.append(
                Feature(
                    "座ぐり穴加工",
                    (
                        f"下穴 φ{through_diameter:.1f} x {through_depth:.1f} mm / "
                        f"座ぐり φ{counterbore_diameter:.1f} x {counterbore_depth:.1f} mm"
                    ),
                    count,
                    None,
                    f'{drill["tool_name"]} + {counterbore_tool["tool_name"]}',
                    "穴",
                    drill_sec + counterbore_sec,
                    "B-Rep円筒面の同芯径違いから座ぐり候補を抽出",
                    f"下穴: {master_condition_summary(drill_cond)} / 座ぐり: {master_condition_summary(counterbore_cond)}",
                    path_plan_summary(
                        drill_cutting_length + counterbore_cutting_length,
                        drill_pecks + counterbore_passes,
                        count * (drill_pecks + counterbore_passes + 1),
                        method="ドリル+円弧補間",
                        extra=f"{count}か所",
                    ),
                    counterbore_selection_reason,
                )
            )

        for group in machining_features.get("countersinks") or []:
            hole_diameter = float(group["hole_diameter"])
            sink_diameter = float(group["sink_diameter"])
            count = int(group["count"])
            sink_depth = max(0.1, float(group.get("avg_depth", 0.8)))
            chamfer_tool = pick_tool(conn, "EM", min(max(sink_diameter * 0.45, 3.0), 8.0), max_tool_diameter)
            chamfer_cond = condition_for(conn, chamfer_tool["tool_id"], material_type, "ポケット")
            chamfer_feed, _chamfer_ap, _chamfer_ae = condition_params(chamfer_cond)
            cutting_length = max(
                math.pi * sink_diameter * count,
                float(group.get("edge_length", math.pi * sink_diameter)) * count,
            )
            countersink_sec = path_time_sec(
                cutting_length,
                chamfer_feed * 0.45,
                approach_count=count * 2,
                approach_mm=min(sink_depth + 4.0, 12.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.68,
            )
            features.append(
                Feature(
                    "皿もみ・穴口面取り",
                    f"下穴 φ{hole_diameter:.1f} / 皿径 φ{sink_diameter:.1f} / 深さ {sink_depth:.2f} mm",
                    count,
                    chamfer_tool["tool_id"],
                    chamfer_tool["tool_name"],
                    "仕上げ",
                    countersink_sec,
                    "B-Rep円錐面と同芯穴から皿もみ候補を抽出",
                    master_condition_summary(chamfer_cond),
                    path_plan_summary(
                        cutting_length,
                        count,
                        count * 2,
                        method="円錐面取り",
                    ),
                    internal_tool_selection_reason(chamfer_tool, sink_diameter, max_tool_diameter, "皿もみ・穴口面取り"),
                )
            )

        for group in machining_features.get("slots") or []:
            width = float(group["width"])
            length = float(group["length"])
            count = int(group["count"])
            depth = max(0.5, float(group.get("avg_depth", bbox["z"] * 0.25)))
            slot_tool = pick_tool(conn, "EM", width, max_tool_diameter)
            slot_cond = condition_for(conn, slot_tool["tool_id"], material_type, "ポケット")
            slot_catalog_cond = None
            if use_manufacturer_conditions:
                slot_catalog_cond = auto_manufacturer_condition_for(conn, material_type, width, depth, "ポケット", max_tool_diameter)
            volume = float(group.get("total_volume", max(0.0, length * width * depth * count)))
            if slot_catalog_cond is not None:
                slot_diameter = float(slot_catalog_cond["outside_diameter_mm"])
                slot_feed, slot_ap, slot_ae = condition_params(slot_catalog_cond, catalog=True)
                slot_tool_name = (
                    f'{slot_catalog_cond["manufacturer"]} {slot_catalog_cond["series_code"]} '
                    f'φ{slot_diameter:g} '
                    f'{slot_catalog_cond["corner_radius_label"]}'
                )
                slot_tool_id = None
                slot_note = (
                    f'B-Repスロット候補から自動選定: {slot_catalog_cond["work_material"]} '
                    f'{slot_catalog_cond["hardness"]}, rpm {slot_catalog_cond["spindle_rpm"]}, '
                    f'ap {slot_catalog_cond["axial_depth_mm"]}, ae {slot_catalog_cond["radial_depth_mm"]}, '
                    f'出典 p.{slot_catalog_cond["source_page"]}'
                )
                slot_condition_text = catalog_condition_summary(slot_catalog_cond)
                slot_selection_reason = catalog_tool_selection_reason(
                    slot_catalog_cond,
                    width,
                    depth,
                    max_tool_diameter,
                    "溝加工",
                )
            else:
                slot_diameter = float(slot_tool["diameter_mm"])
                slot_feed, slot_ap, slot_ae = condition_params(slot_cond)
                slot_tool_name = slot_tool["tool_name"]
                slot_tool_id = slot_tool["tool_id"]
                slot_note = "B-Rep円筒端部ペアからスロット候補を抽出"
                slot_condition_text = master_condition_summary(slot_cond)
                slot_selection_reason = internal_tool_selection_reason(slot_tool, width, max_tool_diameter, "溝加工")
            slot_plan_ap = axial_depth_for_plan(slot_ap, slot_diameter, depth, ratio=0.8)
            slot_plan_ae = roughing_width_for_plan(slot_ae, slot_diameter, ratio=0.25)
            slot_depth_passes = max(1, math.ceil(depth / max(0.001, slot_plan_ap)))
            slot_radial_passes = max(1, math.ceil(width / max(0.001, slot_plan_ae)))
            slot_passes = slot_depth_passes * slot_radial_passes
            slot_cutting_length = max(
                volume / max(0.001, slot_plan_ap * slot_plan_ae),
                (length + math.pi * width / 2) * slot_passes * count,
            )
            slot_sec = path_time_sec(
                slot_cutting_length,
                slot_feed,
                approach_count=count * slot_passes * 2,
                approach_mm=min(depth + 5.0, 45.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.78,
            )
            features.append(
                Feature(
                    "溝加工（スロット）",
                    f"幅 {width:.1f} / 長さ {length:.1f} / 深さ {depth:.1f} mm",
                    count,
                    slot_tool_id,
                    slot_tool_name,
                    "ポケット",
                    slot_sec,
                    slot_note,
                    slot_condition_text,
                    path_plan_summary(
                        slot_cutting_length,
                        slot_passes,
                        count * slot_passes * 2,
                        method="溝走査",
                        extra=f"Z {slot_depth_passes}段 x 幅 {slot_radial_passes}回",
                    ),
                    slot_selection_reason,
                )
            )

        if machining_features.get("roughing_volume_mm3") is not None:
            pocket_volume = max(0.0, float(machining_features["roughing_volume_mm3"]))
        elif analysis.get("removal_volume_mm3") is not None:
            pocket_volume = max(0.0, float(analysis["removal_volume_mm3"]))
        else:
            complexity = min(0.22, max(0.06, analysis["face_count"] / 500))
            pocket_volume = bbox["x"] * bbox["y"] * bbox["z"] * complexity

        if analysis["face_count"] >= 18 and pocket_volume > significant_volume_threshold(bbox):
            pocket_tool = pick_tool(conn, "EM", 10, max_tool_diameter)
            pocket_cond = condition_for(conn, pocket_tool["tool_id"], material_type, "ポケット")
            pocket_catalog_cond = None
            if use_manufacturer_conditions:
                pocket_target_diameter = 6.0 if bbox["x"] * bbox["y"] >= 2500 else 3.0
                pocket_required_depth = min(bbox["z"], max(3.0, bbox["z"] * 0.3))
                pocket_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    pocket_target_diameter,
                    pocket_required_depth,
                    "ポケット",
                    max_tool_diameter,
                )
            volume = pocket_volume
            if pocket_catalog_cond is not None:
                pocket_diameter = float(pocket_catalog_cond["outside_diameter_mm"])
                pocket_feed, pocket_ap, pocket_ae = condition_params(pocket_catalog_cond, catalog=True)
                pocket_tool_name = (
                    f'{pocket_catalog_cond["manufacturer"]} {pocket_catalog_cond["series_code"]} '
                    f'φ{pocket_diameter:g} '
                    f'{pocket_catalog_cond["corner_radius_label"]}'
                )
                pocket_note = (
                    f'STP形状から自動選定: {pocket_catalog_cond["work_material"]} '
                    f'{pocket_catalog_cond["hardness"]}, {pocket_catalog_cond["model_family"]}, rpm {pocket_catalog_cond["spindle_rpm"]}, '
                    f'ap {pocket_catalog_cond["axial_depth_mm"]}, ae {pocket_catalog_cond["radial_depth_mm"]}, '
                    f'出典 p.{pocket_catalog_cond["source_page"]}'
                )
                pocket_condition_text = catalog_condition_summary(pocket_catalog_cond)
                pocket_tool_id = None
                pocket_selection_reason = catalog_tool_selection_reason(
                    pocket_catalog_cond,
                    pocket_target_diameter,
                    pocket_required_depth,
                    max_tool_diameter,
                    "荒取り・ポケット加工",
                )
            else:
                pocket_diameter = float(pocket_tool["diameter_mm"])
                pocket_feed, pocket_ap, pocket_ae = condition_params(pocket_cond)
                pocket_tool_name = pocket_tool["tool_name"]
                pocket_note = "B-Rep内部体積差から除去量を算出" if analysis.get("brep_available") else "面数からポケット相当の除去量を概算"
                pocket_condition_text = master_condition_summary(pocket_cond)
                pocket_tool_id = pocket_tool["tool_id"]
                pocket_selection_reason = internal_tool_selection_reason(pocket_tool, 10, max_tool_diameter, "荒取り・ポケット加工")
            pocket_depth = min(bbox["z"], max(1.0, volume / max(1.0, bbox["x"] * bbox["y"])))
            pocket_plan_ap = axial_depth_for_plan(pocket_ap, pocket_diameter, pocket_depth, ratio=0.85)
            pocket_lane_pitch = roughing_width_for_plan(pocket_ae, pocket_diameter, ratio=0.3)
            pocket_depth_passes = max(1, math.ceil(pocket_depth / max(0.001, pocket_plan_ap)))
            pocket_lanes = max(1, math.ceil(min(bbox["x"], bbox["y"]) / pocket_lane_pitch))
            pocket_volume_path = volume / max(0.001, pocket_plan_ap * pocket_lane_pitch)
            pocket_scan_path = max(bbox["x"], bbox["y"]) * pocket_lanes * pocket_depth_passes
            pocket_cutting_length = max(pocket_volume_path, pocket_scan_path)
            pocket_passes = pocket_depth_passes * pocket_lanes
            pocket_sec = path_time_sec(
                pocket_cutting_length,
                pocket_feed,
                approach_count=pocket_depth_passes * 2,
                approach_mm=min(pocket_depth + 5.0, 60.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.74,
            )
            features.append(
                Feature(
                    "荒取り・ポケット加工",
                    f"推定除去体積 {volume:.0f} mm3",
                    1,
                    pocket_tool_id,
                    pocket_tool_name,
                    "ポケット",
                    pocket_sec,
                    pocket_note,
                    pocket_condition_text,
                    path_plan_summary(
                        pocket_cutting_length,
                        pocket_passes,
                        pocket_depth_passes * 2,
                        method="等間隔走査",
                        extra=f"Z {pocket_depth_passes}段 x レーン {pocket_lanes}",
                    ),
                    pocket_selection_reason,
                )
            )

        if analysis["face_count"] >= 18:
            finish_tool = pick_tool(conn, "EM", 10, max_tool_diameter)
            finish_cond = condition_for(conn, finish_tool["tool_id"], material_type, "ポケット")
            finish_catalog_cond = None
            finish_target_diameter = min(10.0, max(3.0, min(bbox["x"], bbox["y"]) * 0.08))
            finish_required_depth = min(bbox["z"], 10.0)
            if use_manufacturer_conditions:
                finish_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    finish_target_diameter,
                    finish_required_depth,
                    "側面",
                    max_tool_diameter,
                )
            if finish_catalog_cond is not None:
                finish_diameter = float(finish_catalog_cond["outside_diameter_mm"])
                finish_feed, finish_ap, finish_ae = condition_params(finish_catalog_cond, catalog=True)
                finish_tool_name = (
                    f'{finish_catalog_cond["manufacturer"]} {finish_catalog_cond["series_code"]} '
                    f'φ{finish_diameter:g} {finish_catalog_cond["corner_radius_label"]}'
                )
                finish_tool_id = None
                finish_condition_text = catalog_condition_summary(finish_catalog_cond)
                finish_selection_reason = catalog_tool_selection_reason(
                    finish_catalog_cond,
                    finish_target_diameter,
                    finish_required_depth,
                    max_tool_diameter,
                    "仕上げ加工",
                )
            else:
                finish_diameter = float(finish_tool["diameter_mm"])
                finish_feed, finish_ap, finish_ae = condition_params(finish_cond)
                finish_tool_name = finish_tool["tool_name"]
                finish_tool_id = finish_tool["tool_id"]
                finish_condition_text = master_condition_summary(finish_cond)
                finish_selection_reason = internal_tool_selection_reason(finish_tool, 10, max_tool_diameter, "仕上げ加工")

            raw_bbox = analysis.get("raw_bbox") or bbox
            feature_summary = machining_features if machining_features else {}
            if feature_summary.get("roughing_volume_mm3") is not None:
                roughing_volume = float(feature_summary["roughing_volume_mm3"])
            elif analysis.get("internal_removal_volume_mm3") is not None:
                roughing_volume = float(analysis["internal_removal_volume_mm3"])
            else:
                roughing_volume = float(analysis.get("removal_volume_mm3") or 0.0)
            estimated_pocket_depth = min(bbox["z"], max(1.0, roughing_volume / max(1.0, bbox["x"] * bbox["y"])))
            has_internal_finish = (
                roughing_volume > significant_volume_threshold(bbox)
                or bool(feature_summary.get("slots"))
                or bool(feature_summary.get("counterbores"))
                or bool(feature_summary.get("countersinks"))
            )
            estimated_pocket_floor_area = 0.0
            if has_internal_finish:
                estimated_pocket_floor_area = min(
                    bbox["x"] * bbox["y"] * 0.85,
                    max(bbox["x"] * bbox["y"] * 0.18, roughing_volume / max(1.0, estimated_pocket_depth)),
                )
            floor_finish_area = top_area + estimated_pocket_floor_area
            finish_pitch = max(0.5, min(finish_diameter * 0.25, max(0.5, finish_ae * 0.5)))
            floor_finish_lanes = max(1, math.ceil(min(bbox["x"], bbox["y"]) / finish_pitch))
            floor_finish_length = max(floor_finish_area / finish_pitch, max(bbox["x"], bbox["y"]) * floor_finish_lanes)
            floor_finish_sec = path_time_sec(
                floor_finish_length,
                finish_feed,
                approach_count=floor_finish_lanes * 2,
                approach_mm=8.0,
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.68,
            )
            features.append(
                Feature(
                    "仕上げ加工（上面・底面）",
                    f"仕上げ面積 {floor_finish_area:.0f} mm2 / ピッチ {finish_pitch:.2f} mm",
                    1,
                    finish_tool_id,
                    finish_tool_name,
                    "仕上げ",
                    floor_finish_sec,
                    "上面とポケット底面を仕上げ走査として追加" if has_internal_finish else "上面仕上げ走査として追加",
                    finish_condition_text,
                    path_plan_summary(
                        floor_finish_length,
                        floor_finish_lanes,
                        floor_finish_lanes * 2,
                        method="仕上げ面走査",
                        extra=f"ピッチ {fmt_number(finish_pitch, 2)} mm",
                    ),
                    finish_selection_reason,
                )
            )

            outer_perimeter = 2 * (float(raw_bbox["x"]) + float(raw_bbox["y"]))
            internal_perimeter = 0.0
            if has_internal_finish:
                internal_perimeter = min(
                    outer_perimeter * 1.6,
                    max(outer_perimeter * 0.35, math.sqrt(max(1.0, estimated_pocket_floor_area)) * 4),
                )
            wall_finish_perimeter = outer_perimeter + internal_perimeter
            wall_finish_step = max(1.0, min(finish_ap, 8.0))
            wall_finish_z_passes = max(1, math.ceil(float(raw_bbox["z"]) / wall_finish_step))
            wall_finish_length = wall_finish_perimeter * wall_finish_z_passes
            wall_finish_sec = path_time_sec(
                wall_finish_length,
                finish_feed,
                approach_count=wall_finish_z_passes * 2,
                approach_mm=10.0,
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.72,
            )
            features.append(
                Feature(
                    "仕上げ加工（側壁・輪郭）",
                    f"外周+内壁 周長 {wall_finish_perimeter:.1f} mm / 高さ {raw_bbox['z']:.1f} mm",
                    1,
                    finish_tool_id,
                    finish_tool_name,
                    "仕上げ",
                    wall_finish_sec,
                    "外周とポケット内壁の仕上げ輪郭加工を追加" if has_internal_finish else "外周側面の仕上げ輪郭加工を追加",
                    finish_condition_text,
                    path_plan_summary(
                        wall_finish_length,
                        wall_finish_z_passes,
                        wall_finish_z_passes * 2,
                        method="仕上げ輪郭",
                        extra=f"Z {wall_finish_z_passes}段",
                    ),
                    finish_selection_reason,
                )
            )

            chamfer_tool = pick_tool(conn, "EM", min(6.0, finish_diameter), max_tool_diameter)
            chamfer_cond = condition_for(conn, chamfer_tool["tool_id"], material_type, "ポケット")
            chamfer_selection_reason = internal_tool_selection_reason(
                chamfer_tool,
                min(6.0, finish_diameter),
                max_tool_diameter,
                "面取り・バリ取り",
            )
            chamfer_feed, _chamfer_ap, _chamfer_ae = condition_params(chamfer_cond)
            hole_chamfer_length = 0.0
            for group in (feature_summary.get("holes") or []) + (feature_summary.get("side_holes") or []):
                hole_chamfer_length += math.pi * float(group.get("diameter", 0.0)) * int(group.get("count", 1))
            for group in feature_summary.get("counterbores") or []:
                hole_chamfer_length += math.pi * float(group.get("counterbore_diameter", 0.0)) * int(group.get("count", 1))
            for group in feature_summary.get("slots") or []:
                hole_chamfer_length += 2 * float(group.get("length", 0.0)) * int(group.get("count", 1))
            chamfer_length = outer_perimeter + hole_chamfer_length
            recognized_chamfer_length = sum(
                float(group.get("total_length", 0.0))
                for group in feature_summary.get("chamfers") or []
            )
            if recognized_chamfer_length > 0:
                chamfer_length = max(chamfer_length, outer_perimeter + recognized_chamfer_length)
            chamfer_sec = path_time_sec(
                chamfer_length,
                chamfer_feed * 0.55,
                approach_count=max(2, math.ceil(chamfer_length / 180.0)),
                approach_mm=6.0,
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.7,
            )
            features.append(
                Feature(
                    "面取り・バリ取り",
                    f"推定エッジ長 {chamfer_length:.1f} mm",
                    1,
                    chamfer_tool["tool_id"],
                    chamfer_tool["tool_name"],
                    "仕上げ",
                    chamfer_sec,
                    "外周・穴・座ぐり・スロット周辺の面取り相当を追加",
                    master_condition_summary(chamfer_cond),
                    path_plan_summary(
                        chamfer_length,
                        max(1, math.ceil(chamfer_length / 180.0)),
                        max(2, math.ceil(chamfer_length / 180.0)),
                        method="面取り輪郭",
                    ),
                    chamfer_selection_reason,
                )
            )

        corner_radius_groups = machining_features.get("corner_radii") or []
        if corner_radius_groups:
            total_corner_area = sum(float(group.get("total_area", 0.0)) for group in corner_radius_groups)
            total_corner_length = sum(float(group.get("total_length", 0.0)) for group in corner_radius_groups)
            total_corner_count = sum(int(group.get("count", 0)) for group in corner_radius_groups)
            smallest_radius = min(float(group.get("radius", 0.0)) for group in corner_radius_groups)
            if total_corner_area > 0 and smallest_radius > 0:
                corner_tool_target = min(6.0, max(3.0, smallest_radius * 2.0))
                corner_tool = pick_tool(conn, "EM", corner_tool_target, max_tool_diameter)
                corner_cond = condition_for(conn, corner_tool["tool_id"], material_type, "ポケット")
                corner_feed, _corner_ap, corner_ae = condition_params(corner_cond)
                corner_stepover = max(0.08, min(max(corner_ae * 0.35, smallest_radius * 0.35), 0.6))
                corner_cutting_length = max(total_corner_length, total_corner_area / corner_stepover)
                corner_approaches = max(total_corner_count, math.ceil(corner_cutting_length / 120.0))
                corner_sec = path_time_sec(
                    corner_cutting_length,
                    corner_feed * 0.55,
                    approach_count=corner_approaches,
                    approach_mm=6.0,
                    rapid_feed_mm_min=rapid_feed,
                    efficiency=0.66,
                )
                features.append(
                    Feature(
                        "小R・フィレット仕上げ",
                        f"R{smallest_radius:.2f}以上 / 面数 {total_corner_count} / 面積 {total_corner_area:.0f} mm2",
                        1,
                        corner_tool["tool_id"],
                        corner_tool["tool_name"],
                        "仕上げ",
                        corner_sec,
                        "B-Repトーラス面から小R・フィレット仕上げ候補を抽出",
                        master_condition_summary(corner_cond),
                        path_plan_summary(
                            corner_cutting_length,
                            max(1, total_corner_count),
                            corner_approaches,
                            method="小R走査",
                            extra=f"ピッチ {fmt_number(corner_stepover, 2)} mm",
                        ),
                        internal_tool_selection_reason(corner_tool, corner_tool_target, max_tool_diameter, "小R・フィレット仕上げ"),
                    )
                )

        safety_feature = safety_allowance_feature(features, estimate_mode, machining_features, max_tool_diameter)
        if safety_feature is not None:
            features.append(safety_feature)

        machining_sec = sum(feature.machining_sec for feature in features)
        unique_tools = {tool_name for feature in features for tool_name in feature_tool_change_names(feature)}
        tool_change_sec = len(unique_tools) * float(machine["atc_time_sec"])
        setup_sec = float(machine["setup_time_min"]) * 60
        travel_length = (bbox["x"] + bbox["y"] + bbox["z"]) * max(1, len(features))
        rapid_sec = travel_length / max(1.0, float(machine["rapid_feed_mm_min"])) * 60
        total_sec = setup_sec + machining_sec + tool_change_sec + rapid_sec

        confidence = 0.86 if analysis.get("brep_available") else 0.7
        if not analysis.get("brep_available") and analysis["points_detected"] == 0:
            confidence -= 0.15
        if not analysis.get("brep_available") and analysis["face_count"] > 120:
            confidence -= 0.12
        if len(features) <= 2:
            confidence -= 0.08
        confidence = max(0.35, min(0.9, confidence))

        tool_usage: dict[str, dict[str, Any]] = {}
        for feature in features:
            if feature.process_type == "補正" or feature.tool_name == "補正":
                continue
            item = tool_usage.setdefault(
                feature.tool_name,
                {"tool_name": feature.tool_name, "usage_count": 0, "machining_sec": 0.0, "cutting_conditions": set()},
            )
            item["usage_count"] += feature.quantity
            item["machining_sec"] += feature.machining_sec
            if feature.cutting_condition:
                item["cutting_conditions"].add(feature.cutting_condition)

        tool_usage_rows = []
        for item in tool_usage.values():
            tool_usage_rows.append(
                {
                    "tool_name": item["tool_name"],
                    "usage_count": item["usage_count"],
                    "machining_sec": item["machining_sec"],
                    "cutting_conditions": " / ".join(sorted(item["cutting_conditions"])) or "-",
                }
            )

        result = {
            "file_name": file_name,
            "material_type": material_type,
            "machine": dict(machine),
            "blank_allowance_mm": blank_allowance_mm,
            "analysis": analysis,
            "features": [asdict(feature) for feature in features],
            "tool_usage": tool_usage_rows,
            "breakdown": {
                "setup_sec": setup_sec,
                "machining_sec": machining_sec,
                "tool_change_sec": tool_change_sec,
                "rapid_sec": rapid_sec,
                "total_sec": total_sec,
            },
            "confidence": confidence,
            "estimate_mode": estimate_mode if estimate_mode in SAFETY_PROFILES else "cautious",
            "estimate_mode_label": SAFETY_PROFILES.get(estimate_mode, SAFETY_PROFILES["cautious"])["label"],
            "condition_source": "STP形状からメーカー条件を自動選定" if use_manufacturer_conditions else "社内マスタ条件",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        cur = conn.execute(
            """
            INSERT INTO histories
            (created_at, file_name, material_type, blank_allowance_mm, machine_name,
             total_sec, confidence, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["created_at"],
                file_name,
                material_type,
                blank_allowance_mm,
                machine["machine_name"],
                total_sec,
                confidence,
                json.dumps(result, ensure_ascii=False),
            ),
        )
        result["history_id"] = cur.lastrowid
        return result


def seconds_label(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, sec = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


@app.template_filter("seconds_label")
def seconds_label_filter(seconds: float) -> str:
    return seconds_label(seconds)


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/health")
def api_health() -> Response:
    with db() as conn:
        face_mill_count = conn.execute(
            "SELECT COUNT(*) FROM tools WHERE tool_name = ? AND tool_type = ?",
            ("φ50 フェイスミル", "FACE"),
        ).fetchone()[0]
        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "deprecated_face_mill_count": face_mill_count,
            }
        )


@app.get("/api/master")
def api_master() -> Response:
    with db() as conn:
        ensure_operational_master(conn)
        ensure_catalog_tool_master(conn)
        ensure_operational_master(conn)
        return jsonify(
            {
                "tools": rows_to_dicts(conn.execute("SELECT * FROM tools ORDER BY tool_id").fetchall()),
                "conditions": rows_to_dicts(
                    conn.execute(
                        """
                        SELECT c.*, t.tool_name, t.memo AS tool_memo
                        FROM cutting_conditions c
                        JOIN tools t ON t.tool_id = c.tool_id
                        ORDER BY c.condition_id
                        """
                    ).fetchall()
                ),
                "machines": rows_to_dicts(conn.execute("SELECT * FROM machines ORDER BY machine_id").fetchall()),
                "manufacturer_catalogs": rows_to_dicts(
                    conn.execute(
                        """
                        SELECT *
                        FROM manufacturer_catalogs
                        ORDER BY manufacturer, tool_type, product_name
                        """
                    ).fetchall()
                ),
                "manufacturer_cutting_conditions": rows_to_dicts(
                    conn.execute(
                        """
                        SELECT *
                        FROM manufacturer_cutting_conditions
                        ORDER BY series_code, outside_diameter_mm, corner_radius_label,
                                 effective_length_mm, work_material
                        """
                    ).fetchall()
                ),
            }
        )


@app.post("/api/analyze")
def api_analyze() -> Response:
    upload = request.files.get("stp_file")
    if upload is None or upload.filename == "":
        return jsonify({"error": "STPファイルを選択してください。"}), 400
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".stp", ".step"}:
        return jsonify({"error": "拡張子 .stp または .step のファイルを指定してください。"}), 400

    material_type = request.form.get("material_type", "鉄")
    blank_allowance_mm = float(request.form.get("blank_allowance_mm", "5") or 5)
    machine_id = int(request.form.get("machine_id", "1") or 1)
    use_manufacturer_conditions = request.form.get("use_manufacturer_conditions", "on") == "on"
    estimate_mode = request.form.get("estimate_mode", "cautious")

    cleanup_old_uploads()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", upload.filename)
    path = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    upload.save(path)
    try:
        result = estimate(
            path,
            upload.filename,
            material_type,
            blank_allowance_mm,
            machine_id,
            use_manufacturer_conditions=use_manufacturer_conditions,
            estimate_mode=estimate_mode,
        )
        result["time_label"] = seconds_label(result["breakdown"]["total_sec"])
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/histories")
def api_histories() -> Response:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT history_id, created_at, file_name, material_type, blank_allowance_mm,
                   machine_name, total_sec, confidence
            FROM histories
            ORDER BY history_id DESC
            LIMIT 100
            """
        ).fetchall()
    payload = rows_to_dicts(rows)
    for row in payload:
        row["time_label"] = seconds_label(row["total_sec"])
    return jsonify(payload)


@app.get("/api/histories/<int:history_id>")
def api_history(history_id: int) -> Response:
    with db() as conn:
        row = conn.execute("SELECT payload_json FROM histories WHERE history_id = ?", (history_id,)).fetchone()
    if row is None:
        return jsonify({"error": "履歴が見つかりません。"}), 404
    payload = json.loads(row["payload_json"])
    payload["history_id"] = history_id
    payload["time_label"] = seconds_label(payload["breakdown"]["total_sec"])
    return jsonify(payload)


@app.get("/api/histories/<int:history_id>/csv")
def api_history_csv(history_id: int) -> Response:
    with db() as conn:
        row = conn.execute("SELECT payload_json FROM histories WHERE history_id = ?", (history_id,)).fetchone()
    if row is None:
        return jsonify({"error": "履歴が見つかりません。"}), 404
    payload = json.loads(row["payload_json"])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["ファイル名", payload["file_name"]])
    writer.writerow(["材質", payload["material_type"]])
    writer.writerow(["機械", payload["machine"]["machine_name"]])
    writer.writerow(["見積安全率", payload.get("estimate_mode_label", "-")])
    writer.writerow(["合計時間", seconds_label(payload["breakdown"]["total_sec"])])
    writer.writerow([])
    writer.writerow(["フィーチャ", "寸法", "数量", "工具", "工程", "切削条件", "工具選定理由", "加工パス", "加工時間秒", "備考"])
    for feature in payload["features"]:
        writer.writerow(
            [
                feature["feature_type"],
                feature["dimensions"],
                feature["quantity"],
                feature["tool_name"],
                feature["process_type"],
                feature.get("cutting_condition", ""),
                feature.get("selection_reason", ""),
                feature.get("path_plan", ""),
                round(feature["machining_sec"], 2),
                feature["note"],
            ]
        )
    csv_bytes = out.getvalue().encode("utf-8-sig")
    filename = f"stp_estimate_{history_id}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/tools")
def api_create_tool() -> Response:
    data = request.get_json(force=True)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tools
            (tool_name, tool_type, diameter_mm, flute_count, max_depth_mm, material, roughing, finishing, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["tool_name"],
                data["tool_type"],
                float(data["diameter_mm"]),
                int(data["flute_count"]),
                float(data["max_depth_mm"]),
                data.get("material", ""),
                1 if data.get("roughing", True) else 0,
                1 if data.get("finishing", True) else 0,
                data.get("memo", ""),
            ),
        )
    return jsonify({"tool_id": cur.lastrowid})


@app.delete("/api/tools/<int:tool_id>")
def api_delete_tool(tool_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM cutting_conditions WHERE tool_id = ?", (tool_id,))
        conn.execute("DELETE FROM tools WHERE tool_id = ?", (tool_id,))
        ensure_operational_master(conn)
    return jsonify({"ok": True})


@app.post("/api/conditions")
def api_create_condition() -> Response:
    data = request.get_json(force=True)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cutting_conditions
            (tool_id, material_type, process_type, spindle_rpm, feed_rate_mm_min,
             depth_of_cut_mm, width_of_cut_mm, tool_change_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(data["tool_id"]),
                data["material_type"],
                data["process_type"],
                int(data["spindle_rpm"]),
                float(data["feed_rate_mm_min"]),
                float(data["depth_of_cut_mm"]),
                float(data["width_of_cut_mm"]),
                int(data["tool_change_sec"]),
            ),
        )
    return jsonify({"condition_id": cur.lastrowid})


@app.delete("/api/conditions/<int:condition_id>")
def api_delete_condition(condition_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM cutting_conditions WHERE condition_id = ?", (condition_id,))
        ensure_operational_master(conn)
    return jsonify({"ok": True})


@app.post("/api/machines")
def api_create_machine() -> Response:
    data = request.get_json(force=True)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO machines
            (machine_name, axis_count, rapid_feed_mm_min, atc_time_sec,
             max_spindle_rpm, max_tool_diameter_mm, setup_time_min, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["machine_name"],
                int(data["axis_count"]),
                float(data["rapid_feed_mm_min"]),
                int(data["atc_time_sec"]),
                int(data["max_spindle_rpm"]),
                float(data["max_tool_diameter_mm"]) if data.get("max_tool_diameter_mm") else None,
                int(data["setup_time_min"]),
                data.get("memo", ""),
            ),
        )
    return jsonify({"machine_id": cur.lastrowid})


@app.put("/api/machines/<int:machine_id>")
def api_update_machine(machine_id: int) -> Response:
    data = request.get_json(force=True)
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE machines
            SET machine_name = ?,
                axis_count = ?,
                rapid_feed_mm_min = ?,
                atc_time_sec = ?,
                max_spindle_rpm = ?,
                max_tool_diameter_mm = ?,
                setup_time_min = ?,
                memo = ?
            WHERE machine_id = ?
            """,
            (
                data["machine_name"],
                int(data["axis_count"]),
                float(data["rapid_feed_mm_min"]),
                int(data["atc_time_sec"]),
                int(data["max_spindle_rpm"]),
                float(data["max_tool_diameter_mm"]) if data.get("max_tool_diameter_mm") else None,
                int(data["setup_time_min"]),
                data.get("memo", ""),
                machine_id,
            ),
        )
    if cur.rowcount == 0:
        return jsonify({"error": "機械マスタが見つかりません。"}), 404
    return jsonify({"ok": True})


@app.delete("/api/machines/<int:machine_id>")
def api_delete_machine(machine_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM machines WHERE machine_id = ?", (machine_id,))
        ensure_operational_master(conn)
    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
