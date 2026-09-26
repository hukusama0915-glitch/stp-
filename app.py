from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException


BASE_DIR = Path(__file__).resolve().parent
# 検証ハーネス等から開発DBを汚さずに起動できるよう、環境変数でDBパスを差し替え可能にする
DB_PATH = Path(os.environ.get("STP_TOOL_DB_PATH") or BASE_DIR / "stp_time_tool.sqlite3")
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_CLEANUP_EXTENSIONS = {".stp", ".step"}
APP_VERSION = "2026-09-25-brushup"
MAX_UPLOAD_MB = 80
MATERIAL_TYPES = ("鉄", "アルミ", "SUS")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


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


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """成功時にcommit・例外時にrollbackし、最後に必ず接続を閉じる。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class InputError(ValueError):
    """利用者の入力値に起因するエラー（HTTP 400で返す）。"""


def input_number(
    source: Any,
    key: str,
    label: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> float | int | None:
    """フォーム/JSONから数値を取り出して範囲検証する。空欄は default（None なら必須扱い）。"""
    raw = source.get(key) if source is not None else None
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        if default is None:
            raise InputError(f"{label}を入力してください。")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InputError(f"{label}は数値で入力してください。") from None
    if not math.isfinite(value):
        raise InputError(f"{label}は数値で入力してください。")
    if minimum is not None and value < minimum:
        raise InputError(f"{label}は{minimum:g}以上で入力してください。")
    if maximum is not None and value > maximum:
        raise InputError(f"{label}は{maximum:g}以下で入力してください。")
    return int(round(value)) if integer else value


def input_text(source: Any, key: str, label: str, *, max_length: int = 120, required: bool = True) -> str:
    value = str((source.get(key) if source is not None else "") or "").strip()
    if required and not value:
        raise InputError(f"{label}を入力してください。")
    if len(value) > max_length:
        raise InputError(f"{label}は{max_length}文字以内で入力してください。")
    return value


def request_json_object() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InputError("リクエスト形式が不正です（JSONオブジェクトを送信してください）。")
    return data


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
        seed_nstool_catalogs(conn)
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
    # 機械マスタが空のときだけ初期値を入れる。
    # 既存行がある場合に補完すると、利用者が削除した既定機械が即座に復活してしまう。
    if conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] > 0:
        return
    rows = DEFAULT_MACHINES
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
NSTOOL_SOURCE_URL = "https://www.ns-tool.com/ja/products/"

# 日進工具（NS TOOL）: 公式Webカタログ。切削条件参考表(XLSX)は
# scripts/fetch_nstool_conditions.py で data/nstool_cutting_conditions.csv に変換する。
NSTOOL_CATALOG_ITEMS = [
    (
        "無限コーティング 2枚刃ロングネックエンドミル（深リブ用） MHR230",
        "https://www.ns-tool.com/ja/products/detail/44",
    ),
    (
        "無限コーティング 4枚刃ロングネックエンドミル（深リブ用） MHR430",
        "https://www.ns-tool.com/ja/products/detail/66",
    ),
    (
        "無限コーティング 2枚刃ロングネックラジアスエンドミル MHR230R",
        "https://www.ns-tool.com/ja/products/detail/45",
    ),
    (
        "無限コーティング 2枚刃エンドミル MSE245",
        "https://www.ns-tool.com/ja/products/detail/99",
    ),
    (
        "無限コーティング 4枚刃エンドミル MSE445",
        "https://www.ns-tool.com/ja/products/detail/103",
    ),
]

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


def seed_nstool_catalogs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM manufacturer_catalogs WHERE manufacturer = ? OR source_url = ?",
        ("日進工具", NSTOOL_SOURCE_URL),
    )
    rows = []
    for product_name, catalog_url in NSTOOL_CATALOG_ITEMS:
        series = infer_series_codes(product_name)
        rows.append(
            (
                "日進工具",
                product_name,
                infer_catalog_tool_type(product_name),
                infer_flute_info(product_name),
                "無限コーティング",
                infer_material_hint(product_name),
                series,
                catalog_url,
                NSTOOL_SOURCE_URL,
                "NS TOOL公式Webカタログ。MHR系は切削条件参考表XLSXから条件を登録済み。MSE系の切込み量はカタログ図参照。",
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
    # CSVを正とし、CSVに含まれるメーカーの既存行は入れ替える（被削材区分の変更等で
    # UNIQUEキーが変わった旧行が残留しないようにする）
    for manufacturer in {row[0] for row in rows}:
        conn.execute(
            "DELETE FROM manufacturer_cutting_conditions WHERE manufacturer = ?",
            (manufacturer,),
        )
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
    reachability: str = ""
    feature_key: str = ""
    selection_candidates: list[dict[str, Any]] = field(default_factory=list)


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


def tool_diameter(tool: sqlite3.Row | dict[str, Any] | None) -> float | None:
    if tool is None:
        return None
    for key in ("diameter_mm", "outside_diameter_mm"):
        try:
            value = tool[key]  # type: ignore[index]
        except (KeyError, IndexError):
            continue
        if value is not None:
            return float(value)
    return None


def tool_effective_length(tool: sqlite3.Row | dict[str, Any] | None) -> float | None:
    if tool is None:
        return None
    for key in ("max_depth_mm", "effective_length_mm"):
        try:
            value = tool[key]  # type: ignore[index]
        except (KeyError, IndexError):
            continue
        if value is not None:
            return float(value)
    return None


def reachability_assessment(
    *,
    tool_diameter_mm: float,
    available_width_mm: float | None = None,
    required_depth_mm: float | None = None,
    effective_length_mm: float | None = None,
    corner_radius_mm: float | None = None,
    context: str = "",
) -> tuple[str, float]:
    notes: list[str] = []
    factor = 1.0
    label = f"{context}: " if context else ""

    if available_width_mm is not None and available_width_mm > 0:
        width = float(available_width_mm)
        if tool_diameter_mm > width:
            notes.append(f"{label}工具径φ{fmt_number(tool_diameter_mm)}が幅{fmt_number(width)}mmを超過")
            factor = max(factor, 1.55)
        elif tool_diameter_mm > width * 0.9:
            notes.append(f"{label}工具径φ{fmt_number(tool_diameter_mm)}に対して幅{fmt_number(width)}mmで逃げが小さい")
            factor = max(factor, 1.18)

    if corner_radius_mm is not None and corner_radius_mm > 0:
        allowed = float(corner_radius_mm) * 2.0
        if tool_diameter_mm > allowed:
            notes.append(f"{label}R{fmt_number(corner_radius_mm)}に対して工具径φ{fmt_number(tool_diameter_mm)}が大きい")
            factor = max(factor, 1.6)
        elif tool_diameter_mm > allowed * 0.9:
            notes.append(f"{label}R{fmt_number(corner_radius_mm)}に対して工具径余裕が小さい")
            factor = max(factor, 1.2)

    if required_depth_mm is not None and required_depth_mm > 0 and effective_length_mm is not None:
        depth = float(required_depth_mm)
        effective = float(effective_length_mm)
        if effective < depth:
            notes.append(f"{label}必要深さ{fmt_number(depth)}mmに対して有効長{fmt_number(effective)}mmが不足")
            factor = max(factor, 1.65)
        elif effective < depth * 1.15:
            notes.append(f"{label}必要深さ{fmt_number(depth)}mmに対して有効長余裕が小さい")
            factor = max(factor, 1.22)

    return " / ".join(notes), factor


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


WALL_TOOL_STANDARD_DIAMETERS = (6.0, 8.0, 10.0, 12.0, 16.0)


def wall_tool_target_diameter(wall_height_mm: float, min_span_mm: float) -> float:
    """外周側面の狙い工具径。突出し L/D≈3 に収まる標準径を壁高さから選ぶ。

    固定φ6だと高い壁で有効長の短い工具が選ばれ、材質によって径がばらつく
    （例: 高さ28mmで鉄はφ6 有効長9mm、SUSはφ10）ため、壁高さを基準にそろえる。
    """
    if min_span_mm < 40 and wall_height_mm < 12:
        return 3.0
    needed = wall_height_mm / 3.0
    for diameter in WALL_TOOL_STANDARD_DIAMETERS:
        if diameter >= needed:
            return diameter
    return WALL_TOOL_STANDARD_DIAMETERS[-1]


def roughing_width_for_plan(width_mm: float, tool_diameter_mm: float, ratio: float = 0.35) -> float:
    diameter = max(0.1, float(tool_diameter_mm))
    planned = max(float(width_mm), diameter * ratio, 0.5)
    return min(planned, diameter * 0.8)


# 工具剛性の制約が強い条件で、カタログapを引き上げてよい上限倍率
AP_AMPLIFY_LIMIT_FINE = 3.0
AP_AMPLIFY_LIMIT_LONG_NECK = 3.0
LONG_NECK_LD_RATIO = 3.0


def is_long_neck(diameter_mm: float, effective_length_mm: float, axial_depth_mm: float) -> bool:
    """首部で長さを稼ぐロングネック条件か。

    有効長/径≥3 でも、長刃工具（例: OSG AE-VML、ap=3D）は刃長全体で切削できるため対象外。
    カタログapが径未満（首部剛性でapが制限されている）のものをロングネックとみなす。
    """
    diameter = float(diameter_mm)
    return (
        diameter > 0
        and float(effective_length_mm) >= diameter * LONG_NECK_LD_RATIO
        and float(axial_depth_mm) < diameter
    )


def is_long_neck_row(row: sqlite3.Row) -> bool:
    return is_long_neck(row["outside_diameter_mm"], row["effective_length_mm"], row["axial_depth_mm"])


def is_deep_flank_row(row: sqlite3.Row) -> bool:
    """ap≥2D の長刃側面切削条件か（ae が小さく、面仕上げのピッチ算出には使えない）。"""
    return float(row["axial_depth_mm"]) >= float(row["outside_diameter_mm"]) * 2


def axial_depth_for_plan(
    depth_mm: float,
    tool_diameter_mm: float,
    required_depth_mm: float,
    ratio: float = 1.0,
    effective_length_mm: float | None = None,
) -> float:
    required = max(0.5, float(required_depth_mm))
    diameter = max(0.1, float(tool_diameter_mm))
    catalog_ap = float(depth_mm)
    planned = max(catalog_ap, min(required, diameter * ratio), 0.5)
    # 微細用条件（ap<0.3mm）は工具剛性の制約が強く、工具径基準への引き上げは
    # 非現実的な除去レートになるため3倍までに制限する
    if 0 < catalog_ap < 0.3:
        planned = min(planned, max(catalog_ap * AP_AMPLIFY_LIMIT_FINE, 0.5))
    # ロングネック（有効長/径≥3）の小さいapは首部の剛性由来。
    # 工具径まで引き上げると除去レートを過大評価するため、カタログapの3倍までに制限する
    if catalog_ap > 0 and effective_length_mm is not None and is_long_neck(diameter, effective_length_mm, catalog_ap):
        planned = min(planned, max(catalog_ap * AP_AMPLIFY_LIMIT_LONG_NECK, 0.5))
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


DEFAULT_EDM_POLICY = {
    "enabled": True,
    "max_width_mm": 3.0,
    "min_depth_mm": 10.0,
    "min_aspect": 6.0,
    "min_taper_deg": 2.0,
}

# 放電加工の参考レート。放電条件・電極段数で大きく変動するため参考値扱い。
EDM_SINKER_BURN_RATE_MM3_MIN = {"アルミ": 12.0, "銅": 10.0}
EDM_SINKER_BURN_RATE_DEFAULT = 8.0
EDM_FINE_HOLE_FEED_MM_MIN = 3.0
EDM_SETUP_SEC_PER_SHAPE = 15 * 60


def edm_policy_from_form(form: Any) -> dict[str, Any]:
    def value(name: str, default: float) -> float:
        try:
            return float(form.get(name, str(default)) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": form.get("edm_enabled", "on") == "on",
        "max_width_mm": max(0.0, value("edm_max_width_mm", DEFAULT_EDM_POLICY["max_width_mm"])),
        "min_depth_mm": max(0.0, value("edm_min_depth_mm", DEFAULT_EDM_POLICY["min_depth_mm"])),
        "min_aspect": max(0.0, value("edm_min_aspect", DEFAULT_EDM_POLICY["min_aspect"])),
        "min_taper_deg": max(0.0, value("edm_min_taper_deg", DEFAULT_EDM_POLICY["min_taper_deg"])),
    }


def milling_tool_available(
    conn: sqlite3.Connection,
    required_diameter_mm: float,
    required_depth_mm: float,
) -> bool:
    """径・深さの制約を満たす切削工具が社内マスタまたはメーカー条件に存在するか。"""
    if required_diameter_mm <= 0:
        return False
    row = conn.execute(
        "SELECT 1 FROM tools WHERE tool_type = 'EM' AND diameter_mm <= ? AND max_depth_mm >= ? LIMIT 1",
        (required_diameter_mm, required_depth_mm),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        """
        SELECT 1 FROM manufacturer_cutting_conditions
        WHERE outside_diameter_mm <= ? AND effective_length_mm >= ?
        LIMIT 1
        """,
        (required_diameter_mm, required_depth_mm),
    ).fetchone()
    return row is not None


def edm_replacement_reason(
    width_mm: float,
    depth_mm: float,
    policy: dict[str, Any],
    *,
    tool_available: bool = True,
    taper_deg: float = 0.0,
) -> str | None:
    """放電加工へ置き換えるべき場合はその理由を返す。切削で続行するならNone。"""
    if not policy.get("enabled", True):
        return None
    width = max(0.0, float(width_mm))
    depth = max(0.0, float(depth_mm))
    taper = max(0.0, float(taper_deg))
    aspect = depth / width if width > 0 else 0.0
    if width > 0 and width <= policy["max_width_mm"] and depth >= policy["min_depth_mm"]:
        return (
            f"指定条件該当: 幅{fmt_number(width)}mm ≤ {fmt_number(policy['max_width_mm'])}mm "
            f"かつ 深さ{fmt_number(depth)}mm ≥ {fmt_number(policy['min_depth_mm'])}mm"
        )
    min_taper = float(policy.get("min_taper_deg", 0.0) or 0.0)
    if min_taper > 0 and taper >= min_taper:
        return (
            f"テーパ角 {fmt_number(taper, 1)}° ≥ {fmt_number(min_taper, 1)}°"
            "（抜き勾配付きの壁は型彫り放電向き）"
        )
    if (
        policy["min_aspect"] > 0
        and width > 0
        and aspect >= policy["min_aspect"]
        and width <= policy["max_width_mm"] * 2
    ):
        return f"深さ/幅比 {fmt_number(aspect, 1)} ≥ {fmt_number(policy['min_aspect'], 1)}（細長形状）"
    if not tool_available:
        return "径と深さを満たす切削工具が工具マスタ・メーカー条件に存在しない"
    return None


def edm_reference_sec(
    edm_type: str,
    *,
    volume_mm3: float = 0.0,
    depth_mm: float = 0.0,
    count: int = 1,
    material_type: str = "鉄",
) -> float:
    """放電加工時間の参考値（電極製作・段取り替えの詳細は含まない）。"""
    if edm_type == "細穴放電":
        burn_sec = depth_mm / EDM_FINE_HOLE_FEED_MM_MIN * 60 * max(1, count)
        return burn_sec + EDM_SETUP_SEC_PER_SHAPE
    rate = EDM_SINKER_BURN_RATE_DEFAULT
    for key, value in EDM_SINKER_BURN_RATE_MM3_MIN.items():
        if key in material_type:
            rate = value
            break
    return volume_mm3 / rate * 60 + EDM_SETUP_SEC_PER_SHAPE


def parse_step_file(path: Path, blank_allowance_mm: float) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    entity_count = len(re.findall(r"^#\d+\s*=", text, flags=re.MULTILINE))
    if entity_count == 0 and "ISO-10303" not in text[:4096].upper():
        # 形状を読めないファイルに対して、サイズ由来の仮寸法で見積もりを出さない
        raise InputError("STEP（ISO-10303-21）形式として読み取れませんでした。CADから出力したSTP/STEPファイルを指定してください。")
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


# ---------------------------------------------------------------------------
# モデル埋め（MCのみの加工時間算出用）
# ドリル穴・ワイヤカット形状をソリッドで埋めたモデルを作り、それを解析し直す。
# ---------------------------------------------------------------------------

FILL_MIN_HOLE_ARC_RATIO = 0.95  # 全周円筒（穴）とみなす円弧率
FILL_THROUGH_TOLERANCE = 0.01  # 押し出し柱と材料の重なりがこの比率未満なら貫通
DEFAULT_FILL_DRILL_MAX_DIAMETER_MM = 13.0


def fill_options_from_form(form: Any) -> dict[str, Any]:
    return {
        "drill_holes": form.get("fill_drill_holes") == "on",
        "wire_shapes": form.get("fill_wire_shapes") == "on",
        # この径を超える丸穴はドリル穴として扱わない（0で上限なし）
        "drill_max_diameter_mm": float(
            input_number(
                form,
                "fill_drill_max_diameter_mm",
                "ドリル穴とみなす最大径",
                default=DEFAULT_FILL_DRILL_MAX_DIAMETER_MM,
                minimum=0,
                maximum=200,
            )
        ),
    }


def _axis_extent(face: Any, origin: Any, axis: Any) -> tuple[float, float]:
    import cadquery as cq  # type: ignore

    values = [(cq.Vector(*vertex.toTuple()) - origin).dot(axis) for vertex in face.Vertices()]
    if not values:
        return 0.0, 0.0
    return min(values), max(values)


def _canonical_axis(origin: Any, axis: Any) -> tuple[Any, Any]:
    """軸の向きをそろえ、原点に最も近い軸上の点を基準点にする（同一軸線の判定用）。"""
    for component in (axis.x, axis.y, axis.z):
        if abs(component) > 1e-9:
            if component < 0:
                axis = axis * -1
            break
    base = origin - axis * origin.dot(axis)
    return base, axis


def _same_axis_line(origin_a: Any, axis_a: Any, origin_b: Any, axis_b: Any, tol: float = 0.02) -> bool:
    if abs(abs(axis_a.dot(axis_b)) - 1.0) > 1e-4:
        return False
    offset = origin_b - origin_a
    radial = offset - axis_a * offset.dot(axis_a)
    return radial.Length <= tol


def _hole_label(diameter: float, axis: Any, coaxial_smaller: bool) -> str:
    if coaxial_smaller:
        return "座ぐり"
    if diameter < 3.0:
        return "微細穴"
    if abs(axis.z) < 0.99:
        return "横穴"
    return "穴"


def _wire_shape_label(wire: Any) -> tuple[str, float, float]:
    edges = wire.Edges()
    bounds = wire.BoundingBox()
    width, length = sorted((float(bounds.xlen), float(bounds.ylen)))
    if len(edges) == 1 and edges[0].geomType() == "CIRCLE":
        return "丸穴", width, length
    if width > 0 and length / width >= 3.0:
        return "溝", width, length
    return "抜き窓・異形穴", width, length


def build_filled_model(path: Path, options: dict[str, bool], output_stem: Path) -> dict[str, Any]:
    """指定カテゴリの形状を埋めたモデルをSTEPで書き出し、埋めた内容を返す。

    - ドリル穴: 空洞側の全周円筒（穴・横穴・座ぐり・微細穴）を同径の円柱で埋める。
      同軸の円錐面（皿もみ・ドリル先端）も一緒に埋める。
    - ワイヤカット形状: 上向き平面の内側輪郭を最下面まで押し出し、材料と重ならない
      （＝板厚方向に貫通する）ものを柱で埋める。抜き窓・異形穴・溝・丸穴が対象。
    返り値の filled_path / bodies_path は埋め後モデルと、埋めた部分だけのモデル。
    """
    import cadquery as cq  # type: ignore

    imported = cq.importers.importStep(str(path))
    shape = imported.val()
    if not hasattr(shape, "Faces") or not hasattr(shape, "BoundingBox"):
        raise InputError("B-Repソリッドとして読み込めないため、形状を埋められません（STEP風の簡易データなど）。")
    bounds = shape.BoundingBox()
    zmin = float(bounds.zmin)
    original_volume = float(shape.Volume())

    tools: list[Any] = []
    items: list[dict[str, Any]] = []
    filled_axes: list[tuple[Any, Any]] = []

    if options.get("drill_holes"):
        # 円筒面は半周ずつに分割されていることが多いので、同一軸線・同一半径でまとめてから判定する
        cylinder_groups: list[dict[str, Any]] = []
        for face in shape.Faces():
            if face.geomType() != "CYLINDER":
                continue
            cylinder = face._geomAdaptor().Cylinder()
            radius = float(cylinder.Radius())
            if radius <= 0:
                continue
            direction = cylinder.Axis().Direction()
            location = cylinder.Axis().Location()
            origin, axis = _canonical_axis(
                cq.Vector(location.X(), location.Y(), location.Z()),
                cq.Vector(direction.X(), direction.Y(), direction.Z()).normalized(),
            )
            t_min, t_max = _axis_extent(face, origin, axis)
            group = next(
                (
                    g
                    for g in cylinder_groups
                    if abs(g["radius"] - radius) < 1e-3
                    and _same_axis_line(g["origin"], g["axis"], origin, axis)
                    and t_min <= g["t_max"] + 1e-3
                    and t_max >= g["t_min"] - 1e-3
                ),
                None,
            )
            if group is None:
                cylinder_groups.append(
                    {"radius": radius, "origin": origin, "axis": axis, "t_min": t_min, "t_max": t_max, "area": float(face.Area())}
                )
            else:
                group["t_min"] = min(group["t_min"], t_min)
                group["t_max"] = max(group["t_max"], t_max)
                group["area"] += float(face.Area())

        holes: list[dict[str, Any]] = []
        for group in cylinder_groups:
            length = group["t_max"] - group["t_min"]
            if length < 0.05:
                continue
            if group["area"] / (2 * math.pi * group["radius"] * length) < FILL_MIN_HOLE_ARC_RATIO:
                continue
            middle = group["origin"] + group["axis"] * ((group["t_min"] + group["t_max"]) / 2)
            if shape.isInside(middle, 1e-4):
                continue  # 材料側の円筒（ボス等）は対象外
            holes.append({**group, "length": length})

        max_diameter = float(options.get("drill_max_diameter_mm") or 0.0)
        for hole in holes:
            coaxial_smaller = [
                other
                for other in holes
                if other is not hole
                and other["radius"] < hole["radius"] - 1e-3
                and _same_axis_line(hole["origin"], hole["axis"], other["origin"], other["axis"])
            ]
            # 座ぐりは下穴の径で、それ以外は自身の径でドリル穴かを判断する
            reference_diameter = min([o["radius"] for o in coaxial_smaller] + [hole["radius"]]) * 2
            if max_diameter > 0 and reference_diameter > max_diameter + 1e-6:
                continue
            hole["coaxial_smaller"] = bool(coaxial_smaller)
            start = hole["origin"] + hole["axis"] * hole["t_min"]
            tools.append(cq.Solid.makeCylinder(hole["radius"], hole["length"], start, hole["axis"]))
            filled_axes.append((hole["origin"], hole["axis"]))
            diameter = hole["radius"] * 2
            items.append(
                {
                    "category": "drill",
                    "kind": _hole_label(diameter, hole["axis"], hole["coaxial_smaller"]),
                    "diameter_mm": round(diameter, 3),
                    "depth_mm": round(hole["length"], 2),
                    "axis": axis_label((hole["axis"].x, hole["axis"].y, hole["axis"].z)),
                    "volume_mm3": math.pi * hole["radius"] ** 2 * hole["length"],
                    "center": [round(v, 2) for v in (start + hole["axis"] * (hole["length"] / 2)).toTuple()],
                }
            )
        # 埋めた穴と同軸の円錐面（皿もみ・ドリル先端）も埋める
        for face in shape.Faces():
            if face.geomType() != "CONE":
                continue
            cone = face._geomAdaptor().Cone()
            direction = cone.Axis().Direction()
            location = cone.Axis().Location()
            axis = cq.Vector(direction.X(), direction.Y(), direction.Z()).normalized()
            origin = cq.Vector(location.X(), location.Y(), location.Z())
            if not any(_same_axis_line(o, a, origin, axis) for o, a in filled_axes):
                continue
            t_min, t_max = _axis_extent(face, origin, axis)
            if t_max - t_min < 0.01:
                continue

            def radius_at(t: float) -> float:
                radii = [
                    ((cq.Vector(*v.toTuple()) - origin) - axis * (cq.Vector(*v.toTuple()) - origin).dot(axis)).Length
                    for v in face.Vertices()
                    if abs((cq.Vector(*v.toTuple()) - origin).dot(axis) - t) < 1e-3
                ]
                return max(radii) if radii else 0.0

            r_start, r_end = radius_at(t_min), radius_at(t_max)
            middle = origin + axis * ((t_min + t_max) / 2)
            if shape.isInside(middle, 1e-4):
                continue
            tools.append(
                cq.Solid.makeCone(r_start, r_end, t_max - t_min, origin + axis * t_min, axis)
            )

    if options.get("wire_shapes"):
        for face in shape.Faces():
            if face.geomType() != "PLANE" or face.normalAt().z < 0.99:
                continue
            face_z = float(face.Center().z)
            height = face_z - zmin
            if height < 0.05:
                continue
            for wire in face.innerWires():
                prism = cq.Solid.extrudeLinear(cq.Face.makeFromWires(wire), cq.Vector(0, 0, -height))
                prism_volume = abs(float(prism.Volume()))
                if prism_volume <= 0:
                    continue
                if float(prism.intersect(shape).Volume()) >= prism_volume * FILL_THROUGH_TOLERANCE:
                    continue  # 途中で材料に当たる＝貫通していない（止まりポケット等）
                kind, width, length = _wire_shape_label(wire)
                center = prism.Center()
                if kind == "丸穴" and any(
                    _same_axis_line(o, a, cq.Vector(center.x, center.y, 0), cq.Vector(0, 0, 1)) for o, a in filled_axes
                ):
                    continue  # ドリル穴として埋め済み
                perimeter = sum(float(edge.Length()) for edge in wire.Edges())
                tools.append(prism)
                items.append(
                    {
                        "category": "wire",
                        "kind": kind,
                        "width_mm": round(width, 2),
                        "length_mm": round(length, 2),
                        "diameter_mm": round(width, 3) if kind == "丸穴" else None,
                        "thickness_mm": round(height, 2),
                        "perimeter_mm": round(perimeter, 1),
                        "cut_area_mm2": round(perimeter * height, 1),
                        "volume_mm3": prism_volume,
                        "center": [round(v, 2) for v in center.toTuple()],
                    }
                )

    if not tools:
        return {"items": [], "filled_path": None, "bodies_path": None, "added_volume_mm3": 0.0}

    filled = shape.fuse(*tools).clean()
    bodies = cq.Compound.makeCompound(tools)
    filled_path = output_stem.with_name(output_stem.name + "__filled.step")
    bodies_path = output_stem.with_name(output_stem.name + "__fillbodies.step")
    cq.exporters.export(filled, str(filled_path), "STEP")
    cq.exporters.export(bodies, str(bodies_path), "STEP")
    return {
        "items": items,
        "filled_path": filled_path,
        "bodies_path": bodies_path,
        "added_volume_mm3": max(0.0, float(filled.Volume()) - original_volume),
    }


def summarize_fill_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同じ寸法の埋め形状をまとめて一覧用の行にする。"""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        if item["category"] == "drill":
            key = ("drill", item["kind"], item["diameter_mm"], item["depth_mm"], item["axis"])
            label = f'φ{fmt_number(item["diameter_mm"])} / 深さ {item["depth_mm"]:.1f} mm / 軸 {item["axis"]}'
        else:
            key = ("wire", item["kind"], item["width_mm"], item["length_mm"], item["thickness_mm"], item["perimeter_mm"])
            size = (
                f'φ{fmt_number(item["diameter_mm"])}'
                if item["kind"] == "丸穴"
                else f'{item["width_mm"]:.1f} x {item["length_mm"]:.1f} mm'
            )
            label = f'{size} / 板厚 {item["thickness_mm"]:.1f} mm / 周長 {item["perimeter_mm"]:.1f} mm'
        group = groups.setdefault(
            key,
            {
                "category": item["category"],
                "kind": item["kind"],
                "dimensions": label,
                "count": 0,
                "volume_mm3": 0.0,
                "cut_area_mm2": 0.0,
                "centers": [],
            },
        )
        group["count"] += 1
        group["volume_mm3"] += float(item["volume_mm3"])
        group["cut_area_mm2"] += float(item.get("cut_area_mm2") or 0.0)
        group["centers"].append(item["center"])
    rows = list(groups.values())
    rows.sort(key=lambda row: (row["category"] != "drill", row["kind"], row["dimensions"]))
    for row in rows:
        row["volume_mm3"] = round(row["volume_mm3"], 1)
        row["cut_area_mm2"] = round(row["cut_area_mm2"], 1)
    return rows


# ---------------------------------------------------------------------------
# 別工程の時間算出（埋めた形状を NC穴加工機・ワイヤ放電加工機で加工する場合）
# ---------------------------------------------------------------------------

DRILL_CONDITIONS_PATH = BASE_DIR / "data" / "osg_drill_conditions.csv"
DRILL_TIP_LENGTH_RATIO = 0.18  # 先端角140°のドリル先端長 ≒ D/2 / tan70°
DRILL_APPROACH_MM = 1.0  # 切削送りで入る逃げ量
DRILL_R_POINT_MM = 3.0  # 穴上のR点（早送りで戻る高さ）
DRILL_STEP_RATIO = 2.0  # 8D超の穴は 2D ごとのステップ送り（カタログ注記8 の 1D～2D の上限側）
_drill_condition_cache: dict[str, Any] = {"mtime": None, "rows": []}


def load_drill_conditions() -> list[dict[str, Any]]:
    """公式ドリル条件CSVを読み込む（ファイル更新時のみ再読込）。"""
    if not DRILL_CONDITIONS_PATH.is_file():
        return []
    mtime = DRILL_CONDITIONS_PATH.stat().st_mtime
    if _drill_condition_cache["mtime"] != mtime:
        with DRILL_CONDITIONS_PATH.open("r", encoding="utf-8", newline="") as f:
            rows = []
            for row in csv.DictReader(f):
                rows.append(
                    {
                        **row,
                        "drill_diameter_mm": float(row["drill_diameter_mm"]),
                        "spindle_rpm": float(row["spindle_rpm"]),
                        "feed_per_rev_min": float(row["feed_per_rev_min"]),
                        "feed_per_rev_max": float(row["feed_per_rev_max"]),
                        "max_depth_ratio": float(row["max_depth_ratio"]),
                    }
                )
        _drill_condition_cache.update({"mtime": mtime, "rows": rows})
    return list(_drill_condition_cache["rows"])


def drill_condition_for(diameter_mm: float, material_type: str) -> dict[str, Any] | None:
    """径の前後の表の行から回転数・送り量を線形補間する。表の範囲外・材質なしは None。"""
    rows = sorted(
        (row for row in load_drill_conditions() if row["app_material"] and row["app_material"] in material_type),
        key=lambda row: row["drill_diameter_mm"],
    )
    if not rows:
        return None
    lower = [row for row in rows if row["drill_diameter_mm"] <= diameter_mm + 1e-9]
    upper = [row for row in rows if row["drill_diameter_mm"] >= diameter_mm - 1e-9]
    if not lower or not upper:
        return None
    low, high = lower[-1], upper[0]
    span = high["drill_diameter_mm"] - low["drill_diameter_mm"]
    ratio = 0.0 if span <= 0 else (diameter_mm - low["drill_diameter_mm"]) / span

    def interpolate(key: str) -> float:
        return low[key] + (high[key] - low[key]) * ratio

    feed_per_rev = (interpolate("feed_per_rev_min") + interpolate("feed_per_rev_max")) / 2
    rpm = interpolate("spindle_rpm")
    return {
        "rpm": rpm,
        "feed_per_rev": feed_per_rev,
        "feed_mm_min": rpm * feed_per_rev,
        "max_depth_ratio": low["max_depth_ratio"],
        "source": f'{low["manufacturer"]} {low["series_code"]} {low["work_material"]} 出典 p.{low["source_page"]}',
        "source_url": low["source_url"],
    }


def _nearest_neighbour_length(points: list[list[float]]) -> float:
    """穴位置を近い順に巡回したときの移動距離（XY平面）。"""
    if len(points) < 2:
        return 0.0
    remaining = [tuple(p[:2]) for p in points]
    current = remaining.pop(0)
    total = 0.0
    while remaining:
        nearest = min(remaining, key=lambda p: math.dist(p, current))
        total += math.dist(nearest, current)
        remaining.remove(nearest)
        current = nearest
    return total


def nc_hole_plan(
    items: list[dict[str, Any]],
    material_type: str,
    machine: sqlite3.Row | dict[str, Any],
    estimate_mode: str,
) -> dict[str, Any]:
    """埋めたドリル穴を NC穴加工機で加工した場合の時間。"""
    drill_items = [item for item in items if item["category"] == "drill"]
    rapid_feed = max(1.0, float(machine["rapid_feed_mm_min"]))
    profile = SAFETY_PROFILES.get(estimate_mode, SAFETY_PROFILES["cautious"])
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in drill_items:
        key = (item["kind"], item["diameter_mm"], item["depth_mm"], item["axis"])
        group = groups.setdefault(key, {"item": item, "centers": []})
        group["centers"].append(item["center"])

    rows: list[dict[str, Any]] = []
    tools: set[float] = set()
    directions: set[str] = set()
    cutting_total = 0.0
    rapid_total = 0.0
    for (kind, diameter, depth, axis), group in sorted(groups.items(), key=lambda pair: (pair[0][1], pair[0][0])):
        count = len(group["centers"])
        condition = drill_condition_for(float(diameter), material_type)
        row: dict[str, Any] = {
            "kind": kind,
            "dimensions": f"φ{fmt_number(diameter)} / 深さ {float(depth):.1f} mm / 軸 {axis}",
            "count": count,
            "rpm": None,
            "feed_mm_min": None,
            "sec": None,
            "note": "",
        }
        if condition is None:
            if float(diameter) < 2.0:
                row["note"] = "φ2未満はドリルの公式条件が未登録のため時間未算出"
            elif float(diameter) > 20.0:
                row["note"] = "φ20超はドリルの公式条件が未登録のため時間未算出"
            else:
                row["note"] = f"{material_type}のドリル公式条件が未登録のため時間未算出"
            rows.append(row)
            continue
        feed = max(1.0, condition["feed_mm_min"])
        depth_ratio = float(depth) / max(float(diameter), 0.1)
        cut_length = float(depth) + float(diameter) * DRILL_TIP_LENGTH_RATIO + DRILL_APPROACH_MM
        cutting_sec = cut_length / feed * 60
        retract_sec = (float(depth) + DRILL_R_POINT_MM) / rapid_feed * 60
        notes = [condition["source"]]
        if kind == "座ぐり":
            notes.append("座ぐりカッターの公式条件が無いため同径ドリル条件で近似")
        if depth_ratio > condition["max_depth_ratio"]:
            steps = max(0, math.ceil(float(depth) / (float(diameter) * DRILL_STEP_RATIO)) - 1)
            # ステップごとに穴上まで戻って再進入する往復分を早送りで加算
            retract_sec += sum(2 * (float(diameter) * DRILL_STEP_RATIO * (i + 1)) for i in range(steps)) / rapid_feed * 60
            notes.append(f"L/D {depth_ratio:.1f} は条件表の範囲（{condition['max_depth_ratio']:g}D以下）外: 2Dステップ送りで概算")
        travel_sec = _nearest_neighbour_length(group["centers"]) / rapid_feed * 60
        group_cutting = cutting_sec * count
        group_rapid = retract_sec * count + travel_sec
        cutting_total += group_cutting
        rapid_total += group_rapid
        tools.add(round(float(diameter), 3))
        directions.add(axis)
        row.update(
            {
                "rpm": round(condition["rpm"]),
                "feed_mm_min": round(feed, 1),
                "sec": round(group_cutting + group_rapid, 1),
                "note": " / ".join(notes),
            }
        )
        rows.append(row)

    computed = [row for row in rows if row["sec"] is not None]
    if not computed:
        return {"rows": rows, "total_sec": None, "message": "時間を算出できる穴がありません。"}
    allowance_sec = cutting_total * float(profile["hole"])
    tool_change_sec = len(tools) * float(machine["atc_time_sec"])
    setup_sec = float(machine["setup_time_min"]) * 60 * max(1, len(directions))
    total = cutting_total + rapid_total + allowance_sec + tool_change_sec + setup_sec
    return {
        "rows": rows,
        "machine_name": machine["machine_name"],
        "cutting_sec": round(cutting_total, 1),
        "rapid_sec": round(rapid_total, 1),
        "allowance_sec": round(allowance_sec, 1),
        "tool_change_sec": round(tool_change_sec, 1),
        "tool_count": len(tools),
        "setup_sec": round(setup_sec, 1),
        "directions": sorted(directions),
        "total_sec": round(total, 1),
        "uncomputed_count": sum(row["count"] for row in rows if row["sec"] is None),
    }


def wire_params_from_form(form: Any) -> dict[str, Any]:
    """ワイヤ加工条件（利用者入力）。荒加工速度が空欄なら時間は算出しない。"""
    rough = input_number(form, "wire_rough_speed_mm2_min", "ワイヤ荒加工速度", default=0.0, minimum=0, maximum=10000)
    return {
        "rough_speed_mm2_min": float(rough),
        "skim_count": int(input_number(form, "wire_skim_count", "ワイヤ仕上げ回数", default=0, minimum=0, maximum=10, integer=True)),
        "skim_speed_mm_min": float(
            input_number(form, "wire_skim_speed_mm_min", "ワイヤ仕上げ送り", default=0.0, minimum=0, maximum=1000)
        ),
        "thread_sec": float(input_number(form, "wire_thread_sec", "結線時間", default=0.0, minimum=0, maximum=3600)),
        "setup_min": float(input_number(form, "wire_setup_min", "ワイヤ段取り", default=0.0, minimum=0, maximum=1440)),
    }


def wire_cut_plan(items: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    """埋めたワイヤカット形状をワイヤ放電加工機で加工した場合の時間。"""
    wire_items = [item for item in items if item["category"] == "wire"]
    rows: list[dict[str, Any]] = []
    rough_speed = params["rough_speed_mm2_min"]
    skim_count = params["skim_count"]
    skim_speed = params["skim_speed_mm_min"]
    missing: list[str] = []
    if rough_speed <= 0:
        missing.append("荒加工速度")
    if skim_count > 0 and skim_speed <= 0:
        missing.append("仕上げ送り")
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in wire_items:
        key = (item["kind"], item["width_mm"], item["length_mm"], item["thickness_mm"], item["perimeter_mm"])
        groups.setdefault(key, {"item": item, "count": 0})["count"] += 1
    total_cut = 0.0
    for (kind, width, length, thickness, perimeter), group in groups.items():
        item = group["item"]
        count = group["count"]
        size = f'φ{fmt_number(item["diameter_mm"])}' if kind == "丸穴" else f"{width:.1f} x {length:.1f} mm"
        row: dict[str, Any] = {
            "kind": kind,
            "dimensions": f"{size} / 板厚 {thickness:.1f} mm / 周長 {perimeter:.1f} mm",
            "count": count,
            "cut_area_mm2": round(float(item["cut_area_mm2"]) * count, 1),
            "sec": None,
        }
        if not missing:
            rough_sec = float(item["cut_area_mm2"]) / rough_speed * 60
            skim_sec = float(perimeter) * skim_count / skim_speed * 60 if skim_count > 0 else 0.0
            shape_sec = (rough_sec + skim_sec + params["thread_sec"]) * count
            row["sec"] = round(shape_sec, 1)
            total_cut += shape_sec
        rows.append(row)
    if not rows:
        return {"rows": [], "total_sec": None, "message": "ワイヤカット形状がありません。"}
    if missing:
        return {
            "rows": rows,
            "total_sec": None,
            "params": params,
            "message": f"ワイヤ加工条件（{'・'.join(missing)}）が未入力のため時間を算出していません。",
        }
    setup_sec = params["setup_min"] * 60
    return {
        "rows": rows,
        "params": params,
        "cutting_sec": round(total_cut, 1),
        "setup_sec": round(setup_sec, 1),
        "total_sec": round(total_cut + setup_sec, 1),
    }


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
        planar_wall_faces: list[dict[str, Any]] = []
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
                    face_bbox = face.BoundingBox()
                    bbox_lengths = {
                        "x": float(face_bbox.xlen),
                        "y": float(face_bbox.ylen),
                        "z": float(face_bbox.zlen),
                    }
                except Exception:
                    continue
                if radius <= 0:
                    continue
                estimated_depth = area / max(0.000001, 2 * math.pi * radius)
                axis_name = axis_label(axis)
                axis_extent = bbox_lengths[axis_name.lower()]
                # 全周円筒(穴)なら面積 ≒ 2πr×軸長。部分円筒(隅Rなど)は円弧率が下がる。
                full_area = 2 * math.pi * radius * max(axis_extent, 0.000001)
                arc_ratio = min(1.2, area / max(full_area, 0.000001))
                cylindrical_faces.append(
                    {
                        "radius": radius,
                        "diameter": radius * 2,
                        "area": area,
                        "estimated_depth": estimated_depth,
                        "axis": axis,
                        "axis_label": axis_name,
                        "axis_extent": axis_extent,
                        "arc_ratio": arc_ratio,
                        "center": center,
                    }
                )
            elif geom_type == "PLANE":
                if len(planar_wall_faces) >= 400:
                    continue
                try:
                    normal_vec = face.normalAt()
                    normal = (float(normal_vec.x), float(normal_vec.y), float(normal_vec.z))
                    center = tuple(float(value) for value in face.Center().toTuple())
                    area = float(face.Area())
                    face_bbox = face.BoundingBox()
                    face_bounds = {
                        "xmin": float(face_bbox.xmin),
                        "xmax": float(face_bbox.xmax),
                        "ymin": float(face_bbox.ymin),
                        "ymax": float(face_bbox.ymax),
                        "zmin": float(face_bbox.zmin),
                        "zmax": float(face_bbox.zmax),
                    }
                except Exception:
                    continue
                # 狭溝検出用に垂直壁（法線がほぼ水平）のみ保持。
                # 抜き勾配付きの壁も対象にするため、鉛直からの傾き（テーパ角）を記録する。
                if abs(normal[2]) <= 0.2 and area >= 1.0:
                    planar_wall_faces.append(
                        {
                            "normal": normal,
                            "center": center,
                            "area": area,
                            "bounds": face_bounds,
                            "tilt_deg": math.degrees(math.asin(min(1.0, abs(normal[2])))),
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
            planar_wall_faces=planar_wall_faces,
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


def feature_group_key(*parts: Any) -> str:
    """フィーチャグループ・工具パス表示・除外指定を紐付ける識別子。"""
    return "|".join(f"{part:g}" if isinstance(part, (int, float)) else str(part) for part in parts)


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
    planar_wall_faces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    vertical: list[dict[str, Any]] = []
    side: list[dict[str, Any]] = []
    fine_holes: list[dict[str, Any]] = []
    corner_fillets: list[dict[str, Any]] = []
    consumed: set[int] = set()

    for index, item in enumerate(cylindrical_faces):
        diameter = round(float(item["diameter"]), 2)
        depth = float(item["estimated_depth"])
        radius = float(item["radius"])
        label = str(item.get("axis_label") or axis_label(item["axis"]))
        arc_ratio = float(item.get("arc_ratio", 1.0))
        axis_extent = float(item.get("axis_extent", depth))

        # 微細形状: 従来は半径1.5mm未満を切り捨てていたが、小径穴と縦隅Rとして拾う
        if label == "Z" and radius < 1.5:
            fine_depth = max(depth, axis_extent)
            if fine_depth < 0.5:
                continue
            if arc_ratio >= 0.7:
                fine_holes.append(
                    {
                        "diameter": diameter,
                        "axis": "Z",
                        "depth": fine_depth,
                        "depth_ratio": fine_depth / max(diameter, 0.01),
                        "count": 1,
                        "volume": math.pi * (diameter / 2) ** 2 * fine_depth,
                        "center": item["center"],
                    }
                )
            else:
                corner_fillets.append(
                    {
                        "radius": round(radius, 2),
                        "width": diameter,
                        "depth": axis_extent if axis_extent >= 0.5 else fine_depth,
                        "arc_ratio": arc_ratio,
                        "count": 1,
                        "edge_length": max(0.1, arc_ratio * 2 * math.pi * radius),
                        "center": item["center"],
                    }
                )
            continue

        # ポケット縦壁の隅R: 1/4円弧程度の部分円筒（スロット端の半円 arc_ratio≒0.5 とは区別）
        if label == "Z" and radius <= 4.0 and arc_ratio < 0.4 and axis_extent >= 1.0:
            corner_fillets.append(
                {
                    "radius": round(radius, 2),
                    "width": diameter,
                    "depth": axis_extent,
                    "arc_ratio": arc_ratio,
                    "count": 1,
                    "edge_length": max(0.1, arc_ratio * 2 * math.pi * radius),
                    "center": item["center"],
                }
            )
            continue

        if not (1.5 <= radius <= 20.0 and depth >= 1.0):
            continue
        diameter = round(float(item["diameter"]), 1)
        center = item["center"]
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
                "z_top": round(float(large["center"][2]) + float(large["depth"]) / 2, 2),
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
            cx1, cy1, cz1 = row["center"]
            cx2, cy2, _cz2 = other["center"]
            slots.append(
                {
                    "width": diameter,
                    "length": round(length, 1),
                    "depth": depth,
                    "count": 1,
                    "volume": area * depth,
                    "seg": [round(float(cx1), 2), round(float(cy1), 2), round(float(cx2), 2), round(float(cy2), 2)],
                    "z_top": round(float(cz1) + depth / 2, 2),
                }
            )

    # 平面ペアからの狭溝（角スリット）検出: 対向する垂直壁の隙間を溝候補として拾う
    walls = list(planar_wall_faces or [])[:300]
    used_walls: set[int] = set()
    for i, wall in enumerate(walls):
        if i in used_walls:
            continue
        best: tuple[float, int, float, float] | None = None
        for j in range(i + 1, len(walls)):
            if j in used_walls:
                continue
            other = walls[j]
            dot = sum(a * b for a, b in zip(wall["normal"], other["normal"]))
            if dot > -0.98:
                continue
            delta = [other["center"][k] - wall["center"][k] for k in range(3)]
            gap = sum(d * n for d, n in zip(delta, wall["normal"]))
            # 法線が互いの面を向いている（間が空隙）かつ隙間が狭いペアのみ
            if not (0.1 <= gap <= 8.0):
                continue
            z_overlap = min(wall["bounds"]["zmax"], other["bounds"]["zmax"]) - max(
                wall["bounds"]["zmin"], other["bounds"]["zmin"]
            )
            if z_overlap < 1.0:
                continue
            x_overlap = min(wall["bounds"]["xmax"], other["bounds"]["xmax"]) - max(
                wall["bounds"]["xmin"], other["bounds"]["xmin"]
            )
            y_overlap = min(wall["bounds"]["ymax"], other["bounds"]["ymax"]) - max(
                wall["bounds"]["ymin"], other["bounds"]["ymin"]
            )
            lateral_overlap = max(x_overlap, y_overlap)
            if lateral_overlap < max(gap, 1.0):
                continue
            if best is None or gap < best[0]:
                best = (gap, j, z_overlap, lateral_overlap)
        if best is None:
            continue
        gap, j, z_overlap, lateral_overlap = best
        # 丸端スロット（円筒ペア検出済み）と同幅なら二重計上を避ける
        if any(abs(float(slot["width"]) - gap) <= 0.3 for slot in slots):
            continue
        if z_overlap / max(gap, 0.01) < 1.5:
            continue
        used_walls.add(i)
        used_walls.add(j)
        other = walls[j]
        taper_deg = (float(wall.get("tilt_deg", 0.0)) + float(other.get("tilt_deg", 0.0))) / 2.0
        mid_x = (float(wall["center"][0]) + float(other["center"][0])) / 2.0
        mid_y = (float(wall["center"][1]) + float(other["center"][1])) / 2.0
        # 溝の長手方向 = 壁法線と直交する水平方向
        dir_x, dir_y = -float(wall["normal"][1]), float(wall["normal"][0])
        norm = math.hypot(dir_x, dir_y) or 1.0
        dir_x, dir_y = dir_x / norm, dir_y / norm
        half = lateral_overlap / 2.0
        slots.append(
            {
                "width": round(gap, 2),
                "length": round(lateral_overlap, 1),
                "depth": z_overlap,
                "count": 1,
                "volume": gap * lateral_overlap * z_overlap,
                "taper_deg": round(taper_deg, 2),
                "source": "平面ペア",
                "seg": [
                    round(mid_x - dir_x * half, 2),
                    round(mid_y - dir_y * half, 2),
                    round(mid_x + dir_x * half, 2),
                    round(mid_y + dir_y * half, 2),
                ],
                "z_top": round(min(wall["bounds"]["zmax"], other["bounds"]["zmax"]), 2),
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
                "z_top": round(float(row["center"][2]) + float(row["depth"]) / 2, 2),
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
    tapered_walls: list[dict[str, Any]] = []
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
                    "center": center,
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
        elif axis == "Z" and math.degrees(float(item.get("semi_angle", 0.0))) <= 35.0 and depth >= 2.0:
            # 皿もみ・面取りに該当しない深い円錐面 = 抜き勾配付きのテーパ壁キャビティ
            semi_angle_rad = float(item.get("semi_angle", 0.0))
            top_radius = diameter / 2.0
            bottom_radius = max(0.0, top_radius - depth * math.tan(semi_angle_rad))
            frustum_volume = math.pi * depth / 3.0 * (
                top_radius ** 2 + top_radius * bottom_radius + bottom_radius ** 2
            )
            tapered_walls.append(
                {
                    "taper_deg": round(math.degrees(semi_angle_rad), 1),
                    "width": diameter,
                    "depth": depth,
                    "count": 1,
                    "volume": frustum_volume,
                    "area": float(item.get("area", 0.0)),
                    "center": center,
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
    fine_hole_groups = group_feature_rows(fine_holes, ("diameter", "axis"))
    corner_fillet_groups = group_surface_rows(corner_fillets, ("radius",))
    tapered_wall_groups = group_feature_rows(tapered_walls, ("taper_deg",))
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
    # 3Dプレビュー用の簡易工具パス。keyでフィーチャグループ・除外指定と紐付く。
    overlays: list[dict[str, Any]] = []

    def add_overlay(entry: dict[str, Any]) -> None:
        if len(overlays) < 400:
            overlays.append(entry)

    for row in holes:
        add_overlay(
            {
                "key": feature_group_key("hole", row["diameter"], "Z"),
                "kind": "drill",
                "x": round(float(row["center_x"]), 2),
                "y": round(float(row["center_y"]), 2),
                "z": float(row.get("z_top", 0.0)),
                "depth": round(float(row["depth"]), 2),
                "d": float(row["diameter"]),
            }
        )
    for row in fine_holes:
        cx, cy, cz = row["center"]
        add_overlay(
            {
                "key": feature_group_key("fine_hole", row["diameter"], "Z"),
                "kind": "helix",
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "z": round(float(cz) + float(row["depth"]) / 2, 2),
                "depth": round(float(row["depth"]), 2),
                "d": float(row["diameter"]),
            }
        )
    for row in corner_fillets:
        cx, cy, cz = row["center"]
        add_overlay(
            {
                "key": feature_group_key("corner_fillet", row["radius"]),
                "kind": "vline",
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "z": round(float(cz) + float(row["depth"]) / 2, 2),
                "depth": round(float(row["depth"]), 2),
                "d": float(row["radius"]) * 2,
            }
        )
    for row in slots:
        if "seg" not in row:
            continue
        add_overlay(
            {
                "key": feature_group_key("slot", row["width"], row["length"]),
                "kind": "slot",
                "seg": row["seg"],
                "z": float(row.get("z_top", 0.0)),
                "depth": round(float(row["depth"]), 2),
                "w": float(row["width"]),
            }
        )
    for row in counterbores:
        add_overlay(
            {
                "key": feature_group_key("counterbore", row["through_diameter"], row["counterbore_diameter"]),
                "kind": "circle",
                "x": round(float(row["center_x"]), 2),
                "y": round(float(row["center_y"]), 2),
                "z": float(row.get("z_top", 0.0)),
                "depth": round(float(row["counterbore_depth"]), 2),
                "d": float(row["counterbore_diameter"]),
            }
        )
    for row in countersinks:
        cx, cy, cz = row.get("center", (0.0, 0.0, 0.0))
        add_overlay(
            {
                "key": feature_group_key("countersink", row["hole_diameter"], row["sink_diameter"], row["axis"]),
                "kind": "circle",
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "z": round(float(cz) + float(row["depth"]) / 2, 2),
                "depth": round(float(row["depth"]), 2),
                "d": float(row["sink_diameter"]),
            }
        )
    for row in side:
        cx, cy, cz = row["center"]
        axis_vec = row["axis"]
        axis_len = math.sqrt(sum(a * a for a in axis_vec)) or 1.0
        ux, uy, uz = (a / axis_len for a in axis_vec)
        half = float(row["depth"]) / 2
        add_overlay(
            {
                "key": feature_group_key("side_hole", row["diameter"], row["axis_label"]),
                "kind": "hline",
                "seg3": [
                    round(float(cx) - ux * half, 2), round(float(cy) - uy * half, 2), round(float(cz) - uz * half, 2),
                    round(float(cx) + ux * half, 2), round(float(cy) + uy * half, 2), round(float(cz) + uz * half, 2),
                ],
                "d": float(row["diameter"]),
            }
        )
    for row in tapered_walls:
        cx, cy, cz = row["center"]
        add_overlay(
            {
                "key": feature_group_key("tapered_wall", row["taper_deg"]),
                "kind": "circle",
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "z": round(float(cz) + float(row["depth"]) / 2, 2),
                "depth": round(float(row["depth"]), 2),
                "d": float(row["width"]),
            }
        )

    classified_volume = sum(
        item.get("total_volume", 0.0)
        for group in (hole_groups, fine_hole_groups, side_hole_groups, counterbore_groups, countersink_groups, slot_groups)
        for item in group
    )
    roughing_volume = max(0.0, removal_volume - classified_volume)
    finishing_area = max(0.0, 2 * (raw_bbox["x"] + raw_bbox["y"]) * raw_bbox["z"])

    return {
        "holes": hole_groups,
        "fine_holes": fine_hole_groups,
        "corner_fillets": corner_fillet_groups,
        "tapered_walls": tapered_wall_groups,
        "overlays": overlays,
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
    max_fit_diameter_mm: float | None = None,
    required_depth_mm: float | None = None,
    prefer_largest_fit: bool = False,
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

    fit_rows = rows
    if max_fit_diameter_mm is not None and max_fit_diameter_mm > 0:
        diameter_fit_rows = [row for row in fit_rows if float(row["diameter_mm"]) <= max_fit_diameter_mm]
        if diameter_fit_rows:
            fit_rows = diameter_fit_rows
    if required_depth_mm is not None and required_depth_mm > 0:
        depth_fit_rows = [row for row in fit_rows if float(row["max_depth_mm"]) >= required_depth_mm]
        if depth_fit_rows:
            fit_rows = depth_fit_rows

    if target_diameter is None:
        return fit_rows[-1]
    if prefer_largest_fit:
        return max(fit_rows, key=lambda row: float(row["diameter_mm"]))
    return min(fit_rows, key=lambda row: abs(float(row["diameter_mm"]) - target_diameter))


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
    max_fit_diameter_mm: float | None = None,
    require_depth_fit: bool = False,
    candidates_out: list[dict[str, Any]] | None = None,
    exclude_long_neck: bool = False,
    exclude_deep_flank: bool = False,
) -> sqlite3.Row | None:
    rows = conn.execute("SELECT * FROM manufacturer_cutting_conditions").fetchall()
    if max_tool_diameter_mm is not None and max_tool_diameter_mm > 0:
        rows = [row for row in rows if float(row["outside_diameter_mm"]) <= max_tool_diameter_mm]
    if exclude_long_neck:
        # ロングネック以外（標準長・長刃工具）だけから選ぶ
        rows = [row for row in rows if not is_long_neck_row(row)]
    if exclude_deep_flank:
        rows = [row for row in rows if not is_deep_flank_row(row)]
    if not rows:
        return None
    if max_fit_diameter_mm is not None and max_fit_diameter_mm > 0:
        fit_rows = [row for row in rows if float(row["outside_diameter_mm"]) <= max_fit_diameter_mm]
        if fit_rows:
            rows = fit_rows
    if require_depth_fit and required_depth > 0:
        # 微細形状など、リーチが必須制約になる場合のみ有効長で絞り込む
        depth_rows = [row for row in rows if float(row["effective_length_mm"]) >= required_depth]
        if depth_rows:
            rows = depth_rows

    keywords = material_keywords(material_type)
    # 材質の合う標準長（L/D<3）工具で必要深さに届くものがあるか。
    # 届かない場合、ロングネックは「必要な選択」なので減点を軽くする。
    standard_reaches = any(
        float(row["effective_length_mm"]) >= required_depth
        and not is_long_neck_row(row)
        and matches_material_keywords(row, keywords)
        for row in rows
    )
    scored = [
        (
            row,
            score_manufacturer_condition(
                row,
                keywords=keywords,
                material_type=material_type,
                process_hint=process_hint,
                target_diameter=target_diameter,
                required_depth=required_depth,
                require_depth_fit=require_depth_fit,
                standard_reaches=standard_reaches,
            ),
        )
        for row in rows
    ]

    def sort_key(item: tuple[sqlite3.Row, list[tuple[str, float]]]) -> tuple[float, float, int]:
        row, components = item
        diameter = float(row["outside_diameter_mm"])
        return (sum(points for _, points in components), -abs(diameter - target_diameter), -int(row["condition_id"]))

    scored.sort(key=sort_key, reverse=True)
    if candidates_out is not None:
        candidates_out.extend(selection_candidates_summary(scored))
    return scored[0][0]


def planned_pocket_removal_rate(row: sqlite3.Row, stage_depth: float) -> float:
    """ap上限・ae上限を適用した後の荒取り除去能率（mm3/min）。"""
    diameter = float(row["outside_diameter_mm"])
    feed, ap, ae = condition_params(row, catalog=True)
    plan_ap = axial_depth_for_plan(
        ap, diameter, stage_depth, ratio=0.85, effective_length_mm=float(row["effective_length_mm"])
    )
    lane_pitch = roughing_width_for_plan(ae, diameter, ratio=0.3)
    return feed * plan_ap * lane_pitch


def deep_pocket_condition_for(
    conn: sqlite3.Connection,
    material_type: str,
    required_depth: float,
    stage_depth: float,
    max_tool_diameter_mm: float | None = None,
    candidates_out: list[dict[str, Any]] | None = None,
) -> sqlite3.Row | None:
    """深部ポケット用に、必要深さへ届く条件をap上限適用後の実除去能率の高い順に選ぶ。"""
    keywords = material_keywords(material_type)
    rows = []
    for row in conn.execute("SELECT * FROM manufacturer_cutting_conditions").fetchall():
        diameter = float(row["outside_diameter_mm"])
        if max_tool_diameter_mm and diameter > max_tool_diameter_mm:
            continue
        components = score_manufacturer_condition(
            row,
            keywords=keywords,
            material_type=material_type,
            process_hint="ポケット",
            target_diameter=diameter,
            required_depth=required_depth,
            require_depth_fit=True,
        )
        labels = {label for label, _ in components}
        # 材質が合い、焼入れ鋼・ステンレス専用条件の流用でないものだけ
        if "材質一致" not in labels or labels & {"焼入れ鋼向け条件", "ステンレス向け条件"}:
            continue
        rows.append(row)
    if not rows:
        return None
    reaching = [row for row in rows if float(row["effective_length_mm"]) >= required_depth]
    if reaching:
        rows = reaching
    else:
        longest = max(float(row["effective_length_mm"]) for row in rows)
        rows = [row for row in rows if float(row["effective_length_mm"]) >= longest - 0.01]
    scored = [
        (
            row,
            [
                ("ap上限後の除去能率 cm3/min", planned_pocket_removal_rate(row, stage_depth) / 1000.0),
                ("有効長過剰", -max(0.0, float(row["effective_length_mm"]) - required_depth) * 0.01),
            ],
        )
        for row in rows
    ]
    scored.sort(key=lambda item: (sum(points for _, points in item[1]), -int(item[0]["condition_id"])), reverse=True)
    if candidates_out is not None:
        candidates_out.extend(selection_candidates_summary(scored))
    return scored[0][0]


def condition_searchable_text(row: sqlite3.Row) -> str:
    return " ".join(
        str(row[key] or "")
        for key in ("work_material", "material_group", "product_name", "memo", "tool_type", "series_code")
    ).upper()


def matches_material_keywords(row: sqlite3.Row, keywords: list[str]) -> bool:
    searchable = condition_searchable_text(row)
    return any(keyword.upper() in searchable for keyword in keywords)


def score_manufacturer_condition(
    row: sqlite3.Row,
    *,
    keywords: list[str],
    material_type: str,
    process_hint: str,
    target_diameter: float,
    required_depth: float,
    require_depth_fit: bool,
    standard_reaches: bool = True,
) -> list[tuple[str, float]]:
    """工具選定スコアを内訳（ラベル, 点数）のリストで返す。合計が選定スコア。"""
    searchable = condition_searchable_text(row)
    process_text = process_hint.upper()
    components: list[tuple[str, float]] = []

    if any(keyword.upper() in searchable for keyword in keywords):
        components.append(("材質一致", 1000))
    if material_type in {"鉄", "鋼"} and any(
        word in searchable for word in ("HARDENED", "HRC", "SKD", "STAVAX", "NAK", "HAP")
    ):
        components.append(("焼入れ鋼向け条件", -420))
    if material_type in {"鉄", "鋼"} and "STAINLESS" in searchable:
        components.append(("ステンレス向け条件", -520))
    if "ポケット" in process_hint or "POCKET" in process_text:
        if any(word in searchable for word in ("POCKET", "TROCHOIDAL", "SLOTTING", "SIDE")):
            components.append(("ポケット向け", 120))
        removal_rate = (
            float(row["feed_rate_mm_min"])
            * float(row["axial_depth_mm"])
            * float(row["radial_depth_mm"])
        )
        components.append(("除去能率", min(removal_rate / 50000.0, 1.0) * 260))
    elif "側面" in process_hint or "SIDE" in process_text:
        if any(word in searchable for word in ("SIDE", "MILLING", "FINISHING")):
            components.append(("側面向け", 120))

    if row["tool_type"] in {"SQUARE", "RADIUS"}:
        components.append(("スクエア/ラジアス", 80))
    if row["manufacturer"] == "OSG":
        components.append(("OSG優先", 10))

    diameter = float(row["outside_diameter_mm"])
    effective_length = float(row["effective_length_mm"])
    if not require_depth_fit:
        # 汎用パス（側面・ポケット・仕上げ）では深リブ・微細用条件を避ける。
        # 微細条件のapは工具剛性由来で、汎用形状に適用すると非現実的な見積もりになる。
        axial_depth = float(row["axial_depth_mm"])
        radial_depth = float(row["radial_depth_mm"])
        if axial_depth < 0.3:
            components.append(("微細ap条件", -300))
        if diameter > 0 and radial_depth < diameter * 0.08:
            # 深壁仕上げ用の極小ae条件。汎用パスでは径方向切込みを増幅できない
            components.append(("極小ae条件", -250))
        if is_long_neck(diameter, effective_length, axial_depth):
            # ロングネック（深リブ用）条件。汎用の荒取り・側面には標準工具を優先する。
            # ただし標準長で届く工具が無いなら、有効長不足の工具より優先されるよう減点を軽くする
            if standard_reaches:
                components.append(("ロングネック", -350))
            else:
                components.append(("ロングネック（標準長では届かず）", -60))
    components.append(("径差", -abs(diameter - target_diameter) * 18))
    components.append(("有効長不足", -max(0.0, required_depth - effective_length) * 22))
    components.append(("有効長過剰", -max(0.0, effective_length - required_depth) * 0.3))
    components.append(("送り速度", min(float(row["feed_rate_mm_min"]), 3000.0) / 3000.0 * 25))
    return components


def selection_candidates_summary(
    scored: list[tuple[sqlite3.Row, list[tuple[str, float]]]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """スコア順の候補から、同一工具（メーカー・シリーズ・径・有効長）の重複を除いた上位を返す。"""
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for rank, (row, components) in enumerate(scored, start=1):
        tool_key = (
            row["manufacturer"],
            row["series_code"],
            float(row["outside_diameter_mm"]),
            float(row["effective_length_mm"]),
            row["corner_radius_label"],
        )
        if tool_key in seen:
            continue
        seen.add(tool_key)
        candidates.append(
            {
                "rank": len(candidates) + 1,
                "selected": rank == 1,
                "tool": (
                    f'{row["manufacturer"]} {row["series_code"]} '
                    f'φ{fmt_number(row["outside_diameter_mm"])} {row["corner_radius_label"] or ""}'
                ).strip(),
                "effective_length_mm": float(row["effective_length_mm"]),
                "condition": catalog_condition_summary(row),
                "score": round(sum(points for _, points in components), 1),
                "components": [
                    {"label": label, "points": round(points, 1)}
                    for label, points in components
                    if abs(points) >= 0.05
                ],
            }
        )
        if len(candidates) >= limit:
            break
    if candidates:
        candidates[0]["pool_size"] = len(scored)
    return candidates


def estimate(
    path: Path,
    file_name: str,
    material_type: str,
    blank_allowance_mm: float,
    machine_id: int,
    use_manufacturer_conditions: bool = True,
    estimate_mode: str = "cautious",
    edm_policy: dict[str, Any] | None = None,
    excluded_keys: set[str] | list[str] | None = None,
    save_history: bool = True,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analysis = parse_step_file(path, blank_allowance_mm)
    bbox = analysis["bbox"]
    policy = edm_policy or dict(DEFAULT_EDM_POLICY)
    edm_candidates: list[dict[str, Any]] = []
    # ユーザーが「穴埋め・加工対象外」に指定したフィーチャキー
    excluded_set = {str(key) for key in (excluded_keys or [])}
    excluded_features: list[dict[str, Any]] = []

    with db() as conn:
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
                feature_key="face_top",
            )
        )

        side_tool = pick_tool(conn, "EM", 16, max_tool_diameter)
        side_cond = condition_for(conn, side_tool["tool_id"], material_type, "ポケット")
        side_candidates: list[dict[str, Any]] = []
        side_catalog_cond = None
        side_target_diameter = wall_tool_target_diameter(bbox["z"], min(bbox["x"], bbox["y"]))
        if use_manufacturer_conditions:
            side_catalog_cond = auto_manufacturer_condition_for(
                conn,
                material_type,
                side_target_diameter,
                bbox["z"],
                "側面",
                max_tool_diameter,
                candidates_out=side_candidates,
            )
        side_area = 2 * (bbox["x"] + bbox["y"]) * bbox["z"]
        if side_catalog_cond is not None:
            side_diameter = float(side_catalog_cond["outside_diameter_mm"])
            side_effective_length = float(side_catalog_cond["effective_length_mm"])
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
            side_effective_length = float(side_tool["max_depth_mm"])
            side_feed, side_ap, side_ae = condition_params(side_cond, fallback_ae=max(1.0, side_diameter * 0.35))
            side_pick = max(1.0, side_ae)
            side_tool_name = side_tool["tool_name"]
            side_note = "外周側面として概算"
            side_condition_text = master_condition_summary(side_cond)
            side_tool_id = side_tool["tool_id"]
            side_selection_reason = internal_tool_selection_reason(side_tool, 16, max_tool_diameter, "側面加工")
        side_perimeter = 2 * (bbox["x"] + bbox["y"])
        side_plan_ap = axial_depth_for_plan(
            side_ap, side_diameter, bbox["z"], ratio=1.0, effective_length_mm=side_effective_length
        )
        side_plan_pick = roughing_width_for_plan(side_pick, side_diameter, ratio=0.22)
        side_axial_passes = max(1, math.ceil(bbox["z"] / max(0.001, side_plan_ap)))
        side_radial_stock = max(blank_allowance_mm, side_plan_pick)
        side_radial_passes = max(1, math.ceil(side_radial_stock / max(0.001, side_plan_pick)))
        side_passes = side_axial_passes * side_radial_passes
        side_cutting_length = side_perimeter * side_passes
        side_reachability, side_reachability_factor = reachability_assessment(
            tool_diameter_mm=side_diameter,
            required_depth_mm=bbox["z"],
            effective_length_mm=side_effective_length,
            context="側面",
        )
        side_sec = path_time_sec(
            side_cutting_length,
            side_feed,
            approach_count=side_passes * 2,
            rapid_feed_mm_min=rapid_feed,
            efficiency=0.78,
        ) * side_reachability_factor
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
                side_reachability,
                feature_key="side_walls",
                selection_candidates=side_candidates,
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
            hole_key = feature_group_key("hole", diameter, str(group.get("axis", "Z")))
            hole_depth = max(3.0, float(group.get("avg_depth", bbox["z"] * 0.75)))
            hole_edm_reason = edm_replacement_reason(diameter, hole_depth, policy)
            if hole_edm_reason:
                reference_sec = edm_reference_sec(
                    "細穴放電", depth_mm=hole_depth, count=count, material_type=material_type
                )
                edm_candidates.append(
                    {
                        "feature_key": hole_key,
                        "feature_type": "穴加工",
                        "edm_type": "細穴放電",
                        "dimensions": f"φ{diameter:.2f} / 深さ {hole_depth:.1f} mm",
                        "width_mm": diameter,
                        "depth_mm": hole_depth,
                        "count": count,
                        "volume_mm3": float(group.get("total_volume", 0.0)),
                        "reason": hole_edm_reason,
                        "reference_sec": reference_sec,
                    }
                )
                continue
            drill = pick_tool(conn, "DRILL", diameter, max_tool_diameter)
            cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            drill_selection_reason = internal_tool_selection_reason(drill, diameter, max_tool_diameter, "穴加工")
            depth = max(3.0, float(group.get("avg_depth", bbox["z"] * 0.75)))
            drill_feed, _drill_ap, _drill_ae = condition_params(cond)
            depth_ratio = float(group.get("depth_ratio", depth / max(diameter, 0.1)))
            deep_hole = depth_ratio >= 5.0 or depth >= 30.0
            peck_passes = max(1, math.ceil(depth / max(diameter * 3.0, 1.0)))
            peck_extra = 0.28 if deep_hole else 0.18
            approach_count = count * (peck_passes + (2 if deep_hole else 1))
            drill_cutting_length = depth * count * (1.0 + peck_extra * max(0, peck_passes - 1))
            drill_reachability, drill_reachability_factor = reachability_assessment(
                tool_diameter_mm=float(drill["diameter_mm"]),
                required_depth_mm=depth,
                effective_length_mm=float(drill["max_depth_mm"]),
                context="穴",
            )
            if drill["tool_type"] != "DRILL":
                drill_reachability = " / ".join(
                    item
                    for item in (
                        "穴: DRILL工具がなく代替工具で計算",
                        drill_reachability,
                    )
                    if item
                )
                drill_reachability_factor = max(drill_reachability_factor, 1.25)
            hole_sec = path_time_sec(
                drill_cutting_length,
                drill_feed,
                approach_count=approach_count,
                approach_mm=min(depth + 5.0, 60.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.82 if deep_hole else 0.9,
            ) * drill_reachability_factor
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
                    drill_reachability,
                    feature_key=hole_key,
                )
            )

        for group in machining_features.get("side_holes") or []:
            diameter = float(group["diameter"])
            count = int(group["count"])
            side_hole_key = feature_group_key("side_hole", diameter, str(group.get("axis", "-")))
            drill = pick_tool(conn, "DRILL", diameter, max_tool_diameter)
            cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            drill_selection_reason = internal_tool_selection_reason(drill, diameter, max_tool_diameter, "横穴加工")
            depth = max(3.0, float(group.get("avg_depth", bbox["x"] * 0.5)))
            drill_feed, _drill_ap, _drill_ae = condition_params(cond)
            depth_ratio = float(group.get("depth_ratio", depth / max(diameter, 0.1)))
            deep_hole = depth_ratio >= 5.0 or depth >= 30.0
            peck_passes = max(1, math.ceil(depth / max(diameter * 3.0, 1.0)))
            peck_extra = 0.3 if deep_hole else 0.18
            approach_count = count * (peck_passes + (2 if deep_hole else 1))
            side_hole_cutting_length = depth * count * (1.0 + peck_extra * max(0, peck_passes - 1))
            side_hole_reachability, side_hole_reachability_factor = reachability_assessment(
                tool_diameter_mm=float(drill["diameter_mm"]),
                required_depth_mm=depth,
                effective_length_mm=float(drill["max_depth_mm"]),
                context="横穴",
            )
            if drill["tool_type"] != "DRILL":
                side_hole_reachability = " / ".join(
                    item
                    for item in (
                        "横穴: DRILL工具がなく代替工具で計算",
                        side_hole_reachability,
                    )
                    if item
                )
                side_hole_reachability_factor = max(side_hole_reachability_factor, 1.25)
            side_hole_sec = path_time_sec(
                side_hole_cutting_length,
                drill_feed,
                approach_count=approach_count,
                approach_mm=min(depth + 5.0, 80.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.8 if deep_hole else 0.88,
            ) * side_hole_reachability_factor
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
                    side_hole_reachability,
                    feature_key=side_hole_key,
                )
            )

        for group in machining_features.get("counterbores") or []:
            through_diameter = float(group["through_diameter"])
            counterbore_diameter = float(group["counterbore_diameter"])
            count = int(group["count"])
            counterbore_key = feature_group_key("counterbore", through_diameter, counterbore_diameter)
            through_depth = max(3.0, float(group.get("through_depth", group.get("avg_depth", bbox["z"] * 0.75))))
            counterbore_depth = max(0.5, float(group.get("counterbore_depth", group.get("avg_depth", 2.0))))
            drill = pick_tool(conn, "DRILL", through_diameter, max_tool_diameter, required_depth_mm=through_depth)
            counterbore_tool = pick_tool(
                conn,
                "EM",
                min(counterbore_diameter * 0.55, counterbore_diameter - 0.2),
                max_tool_diameter,
                max_fit_diameter_mm=counterbore_diameter * 0.82,
                required_depth_mm=counterbore_depth,
                prefer_largest_fit=True,
            )
            drill_cond = condition_for(conn, drill["tool_id"], material_type, "穴")
            counterbore_cond = condition_for(conn, counterbore_tool["tool_id"], material_type, "ポケット")
            counterbore_selection_reason = (
                internal_tool_selection_reason(drill, through_diameter, max_tool_diameter, "座ぐり下穴")
                + " / "
                + internal_tool_selection_reason(counterbore_tool, counterbore_diameter, max_tool_diameter, "座ぐり加工")
            )
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
            drill_reachability, drill_reachability_factor = reachability_assessment(
                tool_diameter_mm=float(drill["diameter_mm"]),
                required_depth_mm=through_depth,
                effective_length_mm=float(drill["max_depth_mm"]),
                context="座ぐり下穴",
            )
            if drill["tool_type"] != "DRILL":
                drill_reachability = " / ".join(
                    item
                    for item in (
                        "座ぐり下穴: DRILL工具がなく代替工具で計算",
                        drill_reachability,
                    )
                    if item
                )
                drill_reachability_factor = max(drill_reachability_factor, 1.25)
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
            counterbore_reachability, counterbore_reachability_factor = reachability_assessment(
                tool_diameter_mm=counterbore_tool_diameter,
                available_width_mm=counterbore_diameter,
                required_depth_mm=counterbore_depth,
                effective_length_mm=float(counterbore_tool["max_depth_mm"]),
                context="座ぐり",
            )
            counterbore_reachability_text = " / ".join(
                item for item in (drill_reachability, counterbore_reachability) if item
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
                    drill_sec * drill_reachability_factor + counterbore_sec * counterbore_reachability_factor,
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
                    counterbore_reachability_text,
                    feature_key=counterbore_key,
                )
            )

        for group in machining_features.get("countersinks") or []:
            hole_diameter = float(group["hole_diameter"])
            sink_diameter = float(group["sink_diameter"])
            count = int(group["count"])
            countersink_key = feature_group_key(
                "countersink", hole_diameter, sink_diameter, str(group.get("axis", "Z"))
            )
            sink_depth = max(0.1, float(group.get("avg_depth", 0.8)))
            chamfer_tool = pick_tool(
                conn,
                "EM",
                min(max(sink_diameter * 0.35, 1.0), 8.0),
                max_tool_diameter,
                max_fit_diameter_mm=sink_diameter * 0.85,
                required_depth_mm=sink_depth,
            )
            chamfer_cond = condition_for(conn, chamfer_tool["tool_id"], material_type, "ポケット")
            chamfer_feed, _chamfer_ap, _chamfer_ae = condition_params(chamfer_cond)
            cutting_length = max(
                math.pi * sink_diameter * count,
                float(group.get("edge_length", math.pi * sink_diameter)) * count,
            )
            countersink_reachability, countersink_reachability_factor = reachability_assessment(
                tool_diameter_mm=float(chamfer_tool["diameter_mm"]),
                available_width_mm=sink_diameter,
                required_depth_mm=sink_depth,
                effective_length_mm=float(chamfer_tool["max_depth_mm"]),
                context="皿もみ",
            )
            countersink_sec = path_time_sec(
                cutting_length,
                chamfer_feed * 0.45,
                approach_count=count * 2,
                approach_mm=min(sink_depth + 4.0, 12.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.68,
            ) * countersink_reachability_factor
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
                    countersink_reachability,
                    feature_key=countersink_key,
                )
            )

        for group in machining_features.get("slots") or []:
            width = float(group["width"])
            length = float(group["length"])
            count = int(group["count"])
            slot_key = feature_group_key("slot", width, length)
            depth = max(0.5, float(group.get("avg_depth", bbox["z"] * 0.25)))
            slot_fit_diameter = max(0.1, width * 0.92)
            slot_volume = float(group.get("total_volume", max(0.0, length * width * depth * count)))
            slot_taper_deg = float(group.get("taper_deg", 0.0) or 0.0)
            slot_taper_label = f" / テーパ {slot_taper_deg:.1f}°" if slot_taper_deg >= 0.3 else ""
            slot_edm_reason = edm_replacement_reason(
                width,
                depth,
                policy,
                tool_available=milling_tool_available(conn, slot_fit_diameter, depth),
                taper_deg=slot_taper_deg,
            )
            if slot_edm_reason:
                edm_candidates.append(
                    {
                        "feature_key": slot_key,
                        "feature_type": "溝加工",
                        "edm_type": "型彫り放電",
                        "dimensions": f"幅 {width:.1f} / 長さ {length:.1f} / 深さ {depth:.1f} mm{slot_taper_label}",
                        "width_mm": width,
                        "depth_mm": depth,
                        "count": count,
                        "volume_mm3": slot_volume,
                        "reason": slot_edm_reason,
                        "reference_sec": edm_reference_sec(
                            "型彫り放電", volume_mm3=slot_volume, material_type=material_type
                        ),
                    }
                )
                continue
            slot_tool = pick_tool(
                conn,
                "EM",
                min(width * 0.75, width),
                max_tool_diameter,
                max_fit_diameter_mm=slot_fit_diameter,
                required_depth_mm=depth,
                prefer_largest_fit=True,
            )
            slot_cond = condition_for(conn, slot_tool["tool_id"], material_type, "ポケット")
            slot_candidates: list[dict[str, Any]] = []
            slot_catalog_cond = None
            if use_manufacturer_conditions:
                slot_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    width,
                    depth,
                    "ポケット",
                    max_tool_diameter,
                    max_fit_diameter_mm=slot_fit_diameter,
                    candidates_out=slot_candidates,
                )
            volume = float(group.get("total_volume", max(0.0, length * width * depth * count)))
            if slot_catalog_cond is not None:
                slot_diameter = float(slot_catalog_cond["outside_diameter_mm"])
                slot_effective_length = float(slot_catalog_cond["effective_length_mm"])
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
                slot_effective_length = float(slot_tool["max_depth_mm"])
                slot_feed, slot_ap, slot_ae = condition_params(slot_cond)
                slot_tool_name = slot_tool["tool_name"]
                slot_tool_id = slot_tool["tool_id"]
                slot_note = "B-Rep円筒端部ペアからスロット候補を抽出"
                slot_condition_text = master_condition_summary(slot_cond)
                slot_selection_reason = internal_tool_selection_reason(slot_tool, width, max_tool_diameter, "溝加工")
            slot_plan_ap = axial_depth_for_plan(
                slot_ap, slot_diameter, depth, ratio=0.8, effective_length_mm=slot_effective_length
            )
            slot_plan_ae = roughing_width_for_plan(slot_ae, slot_diameter, ratio=0.25)
            slot_depth_passes = max(1, math.ceil(depth / max(0.001, slot_plan_ap)))
            slot_radial_passes = max(1, math.ceil(width / max(0.001, slot_plan_ae)))
            slot_passes = slot_depth_passes * slot_radial_passes
            slot_cutting_length = max(
                volume / max(0.001, slot_plan_ap * slot_plan_ae),
                (length + math.pi * width / 2) * slot_passes * count,
            )
            slot_reachability, slot_reachability_factor = reachability_assessment(
                tool_diameter_mm=slot_diameter,
                available_width_mm=width,
                required_depth_mm=depth,
                effective_length_mm=slot_effective_length,
                context="溝",
            )
            slot_sec = path_time_sec(
                slot_cutting_length,
                slot_feed,
                approach_count=count * slot_passes * 2,
                approach_mm=min(depth + 5.0, 45.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.78,
            ) * slot_reachability_factor
            features.append(
                Feature(
                    "溝加工（スロット）",
                    f"幅 {width:.1f} / 長さ {length:.1f} / 深さ {depth:.1f} mm{slot_taper_label}",
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
                    slot_reachability,
                    feature_key=slot_key,
                    selection_candidates=slot_candidates,
                )
            )

        for group in machining_features.get("fine_holes") or []:
            diameter = float(group["diameter"])
            count = int(group["count"])
            fine_hole_key = feature_group_key("fine_hole", diameter, str(group.get("axis", "Z")))
            depth = max(0.5, float(group.get("avg_depth", bbox["z"] * 0.5)))
            fine_fit_diameter = max(0.05, diameter * 0.85)
            fine_edm_reason = edm_replacement_reason(
                diameter,
                depth,
                policy,
                tool_available=milling_tool_available(conn, fine_fit_diameter, depth),
            )
            if fine_edm_reason:
                edm_candidates.append(
                    {
                        "feature_key": fine_hole_key,
                        "feature_type": "微細穴加工",
                        "edm_type": "細穴放電",
                        "dimensions": f"φ{diameter:.2f} / 深さ {depth:.1f} mm",
                        "width_mm": diameter,
                        "depth_mm": depth,
                        "count": count,
                        "volume_mm3": float(group.get("total_volume", 0.0)),
                        "reason": fine_edm_reason,
                        "reference_sec": edm_reference_sec(
                            "細穴放電", depth_mm=depth, count=count, material_type=material_type
                        ),
                    }
                )
                continue
            fine_candidates: list[dict[str, Any]] = []
            fine_catalog_cond = None
            if use_manufacturer_conditions:
                fine_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    fine_fit_diameter,
                    depth,
                    "ポケット",
                    max_tool_diameter,
                    max_fit_diameter_mm=fine_fit_diameter,
                    require_depth_fit=True,
                    candidates_out=fine_candidates,
                )
            if fine_catalog_cond is not None:
                fine_tool_diameter = float(fine_catalog_cond["outside_diameter_mm"])
                fine_effective_length = float(fine_catalog_cond["effective_length_mm"])
                fine_feed, fine_ap, _fine_ae = condition_params(fine_catalog_cond, catalog=True)
                fine_tool_name = (
                    f'{fine_catalog_cond["manufacturer"]} {fine_catalog_cond["series_code"]} '
                    f'φ{fine_tool_diameter:g} {fine_catalog_cond["corner_radius_label"]}'
                )
                fine_tool_id = None
                fine_condition_text = catalog_condition_summary(fine_catalog_cond)
                fine_selection_reason = catalog_tool_selection_reason(
                    fine_catalog_cond, fine_fit_diameter, depth, max_tool_diameter, "微細穴加工"
                )
            else:
                fine_tool = pick_tool(
                    conn, "EM", fine_fit_diameter, max_tool_diameter,
                    max_fit_diameter_mm=fine_fit_diameter, required_depth_mm=depth,
                )
                fine_cond = condition_for(conn, fine_tool["tool_id"], material_type, "ポケット")
                fine_tool_diameter = float(fine_tool["diameter_mm"])
                fine_effective_length = float(fine_tool["max_depth_mm"])
                fine_feed, fine_ap, _fine_ae = condition_params(fine_cond)
                fine_tool_name = fine_tool["tool_name"]
                fine_tool_id = fine_tool["tool_id"]
                fine_condition_text = master_condition_summary(fine_cond)
                fine_selection_reason = internal_tool_selection_reason(
                    fine_tool, fine_fit_diameter, max_tool_diameter, "微細穴加工"
                )
            helical_pitch = max(0.02, min(fine_ap, fine_tool_diameter * 0.3, 0.5))
            helical_revolutions = max(1, math.ceil(depth / helical_pitch))
            helical_circle = math.pi * max(0.1, diameter - fine_tool_diameter)
            fine_cutting_length = helical_circle * helical_revolutions * count + helical_circle * count
            fine_reachability, fine_reachability_factor = reachability_assessment(
                tool_diameter_mm=fine_tool_diameter,
                available_width_mm=diameter,
                required_depth_mm=depth,
                effective_length_mm=fine_effective_length,
                context="微細穴",
            )
            fine_sec = path_time_sec(
                fine_cutting_length,
                fine_feed,
                approach_count=count * 2,
                approach_mm=min(depth + 3.0, 20.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.7,
            ) * fine_reachability_factor
            features.append(
                Feature(
                    "微細穴加工（ヘリカル）",
                    f"φ{diameter:.2f} / 深さ {depth:.1f} mm",
                    count,
                    fine_tool_id,
                    fine_tool_name,
                    "穴",
                    fine_sec,
                    "B-Rep微細円筒面（φ3未満）から小径穴を抽出し、小径EMヘリカル加工として算出",
                    fine_condition_text,
                    path_plan_summary(
                        fine_cutting_length,
                        helical_revolutions,
                        count * 2,
                        method="ヘリカル補間",
                        extra=f"{count}穴 / ピッチ {fmt_number(helical_pitch, 2)} mm",
                    ),
                    fine_selection_reason,
                    fine_reachability,
                    feature_key=fine_hole_key,
                    selection_candidates=fine_candidates,
                )
            )

        for group in machining_features.get("corner_fillets") or []:
            radius = float(group["radius"])
            count = int(group["count"])
            corner_key = feature_group_key("corner_fillet", radius)
            depth = max(0.5, float(group.get("max_depth", group.get("avg_depth", bbox["z"] * 0.5))))
            required_diameter = max(0.1, radius * 2.0)
            corner_edm_reason = edm_replacement_reason(
                required_diameter,
                depth,
                policy,
                tool_available=milling_tool_available(conn, required_diameter, depth),
            )
            fillet_contour_length = max(
                float(group.get("total_length", 0.0)),
                (math.pi / 2.0) * radius * count,
            )
            if corner_edm_reason:
                # 隅Rを残す場合の除去体積: (1 - π/4)r^2 × 深さ を放電で立てる想定
                fillet_volume = max(0.0, (1 - math.pi / 4.0) * radius * radius * depth * count)
                edm_candidates.append(
                    {
                        "feature_key": corner_key,
                        "feature_type": "縦隅R（ポケットコーナー）",
                        "edm_type": "型彫り放電",
                        "dimensions": f"R{radius:.2f} / 深さ {depth:.1f} mm",
                        "width_mm": required_diameter,
                        "depth_mm": depth,
                        "count": count,
                        "volume_mm3": fillet_volume,
                        "reason": corner_edm_reason,
                        "reference_sec": edm_reference_sec(
                            "型彫り放電", volume_mm3=fillet_volume, material_type=material_type
                        ),
                    }
                )
                continue
            corner_candidates: list[dict[str, Any]] = []
            corner_catalog_cond = None
            if use_manufacturer_conditions:
                corner_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    required_diameter,
                    depth,
                    "側面",
                    max_tool_diameter,
                    max_fit_diameter_mm=required_diameter,
                    require_depth_fit=True,
                    candidates_out=corner_candidates,
                )
            if corner_catalog_cond is not None:
                fillet_tool_diameter = float(corner_catalog_cond["outside_diameter_mm"])
                fillet_effective_length = float(corner_catalog_cond["effective_length_mm"])
                fillet_feed, fillet_ap, _fillet_ae = condition_params(corner_catalog_cond, catalog=True)
                fillet_tool_name = (
                    f'{corner_catalog_cond["manufacturer"]} {corner_catalog_cond["series_code"]} '
                    f'φ{fillet_tool_diameter:g} {corner_catalog_cond["corner_radius_label"]}'
                )
                fillet_tool_id = None
                fillet_condition_text = catalog_condition_summary(corner_catalog_cond)
                fillet_selection_reason = catalog_tool_selection_reason(
                    corner_catalog_cond, required_diameter, depth, max_tool_diameter, "縦隅R仕上げ"
                )
            else:
                fillet_tool = pick_tool(
                    conn, "EM", required_diameter, max_tool_diameter,
                    max_fit_diameter_mm=required_diameter, required_depth_mm=depth,
                )
                fillet_cond = condition_for(conn, fillet_tool["tool_id"], material_type, "ポケット")
                fillet_tool_diameter = float(fillet_tool["diameter_mm"])
                fillet_effective_length = float(fillet_tool["max_depth_mm"])
                fillet_feed, fillet_ap, _fillet_ae = condition_params(fillet_cond)
                fillet_tool_name = fillet_tool["tool_name"]
                fillet_tool_id = fillet_tool["tool_id"]
                fillet_condition_text = master_condition_summary(fillet_cond)
                fillet_selection_reason = internal_tool_selection_reason(
                    fillet_tool, required_diameter, max_tool_diameter, "縦隅R仕上げ"
                )
            fillet_step = max(0.01, min(fillet_ap, 2.0))
            fillet_z_passes = min(2000, max(1, math.ceil(depth / fillet_step)))
            fillet_cutting_length = fillet_contour_length * fillet_z_passes
            fillet_reachability, fillet_reachability_factor = reachability_assessment(
                tool_diameter_mm=fillet_tool_diameter,
                corner_radius_mm=radius,
                required_depth_mm=depth,
                effective_length_mm=fillet_effective_length,
                context="縦隅R",
            )
            fillet_sec = path_time_sec(
                fillet_cutting_length,
                fillet_feed,
                approach_count=count * 2,
                approach_mm=min(depth + 3.0, 25.0),
                rapid_feed_mm_min=rapid_feed,
                efficiency=0.68,
            ) * fillet_reachability_factor
            features.append(
                Feature(
                    "縦隅R仕上げ（小径EM）",
                    f"R{radius:.2f} / 深さ {depth:.1f} mm / {count}か所",
                    count,
                    fillet_tool_id,
                    fillet_tool_name,
                    "仕上げ",
                    fillet_sec,
                    "B-Rep部分円筒面からポケット縦壁の隅Rを抽出し、小径EMの等高線仕上げとして算出",
                    fillet_condition_text,
                    path_plan_summary(
                        fillet_cutting_length,
                        fillet_z_passes,
                        count * 2,
                        method="等高線仕上げ",
                        extra=f"Z {fillet_z_passes}段 x {count}か所",
                    ),
                    fillet_selection_reason,
                    fillet_reachability,
                    feature_key=corner_key,
                    selection_candidates=corner_candidates,
                )
            )

        for group in machining_features.get("tapered_walls") or []:
            taper_deg = float(group.get("taper_deg", 0.0) or 0.0)
            count = int(group["count"])
            depth = max(0.5, float(group.get("max_depth", group.get("avg_depth", bbox["z"] * 0.5))))
            wall_width = float(group.get("width", 0.0) or 0.0)
            taper_edm_reason = edm_replacement_reason(
                wall_width,
                depth,
                policy,
                taper_deg=taper_deg,
            )
            if taper_edm_reason:
                taper_volume = float(group.get("total_volume", 0.0))
                edm_candidates.append(
                    {
                        "feature_key": feature_group_key("tapered_wall", taper_deg),
                        "feature_type": "テーパ壁（抜き勾配キャビティ）",
                        "edm_type": "型彫り放電",
                        "dimensions": f"テーパ {taper_deg:.1f}° / 上部φ{wall_width:.1f} / 深さ {depth:.1f} mm",
                        "width_mm": wall_width,
                        "depth_mm": depth,
                        "count": count,
                        "volume_mm3": taper_volume,
                        "reason": taper_edm_reason,
                        "reference_sec": edm_reference_sec(
                            "型彫り放電", volume_mm3=taper_volume, material_type=material_type
                        ),
                    }
                )

        if machining_features.get("roughing_volume_mm3") is not None:
            pocket_volume = max(0.0, float(machining_features["roughing_volume_mm3"]))
        elif analysis.get("removal_volume_mm3") is not None:
            pocket_volume = max(0.0, float(analysis["removal_volume_mm3"]))
        else:
            complexity = min(0.22, max(0.06, analysis["face_count"] / 500))
            pocket_volume = bbox["x"] * bbox["y"] * bbox["z"] * complexity

        # B-Rep解析できた場合は面数ではなく除去体積で判断する（穴を埋めたモデルは面数が少なくなるため）
        has_internal_machining = pocket_volume > significant_volume_threshold(bbox) and (
            bool(analysis.get("brep_available")) or analysis["face_count"] >= 18
        )
        if has_internal_machining:
            pocket_tool = pick_tool(conn, "EM", 10, max_tool_diameter)
            pocket_cond = condition_for(conn, pocket_tool["tool_id"], material_type, "ポケット")
            pocket_candidates: list[dict[str, Any]] = []
            pocket_catalog_cond = None
            pocket_target_diameter = 6.0 if bbox["x"] * bbox["y"] >= 2500 else 3.0
            if use_manufacturer_conditions:
                pocket_required_depth = min(bbox["z"], max(3.0, bbox["z"] * 0.3))
                pocket_catalog_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    pocket_target_diameter,
                    pocket_required_depth,
                    "ポケット",
                    max_tool_diameter,
                    candidates_out=pocket_candidates,
                )
            volume = pocket_volume
            if pocket_catalog_cond is not None:
                pocket_diameter = float(pocket_catalog_cond["outside_diameter_mm"])
                pocket_effective_length = float(pocket_catalog_cond["effective_length_mm"])
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
                pocket_effective_length = float(pocket_tool["max_depth_mm"])
                pocket_feed, pocket_ap, pocket_ae = condition_params(pocket_cond)
                pocket_tool_name = pocket_tool["tool_name"]
                pocket_note = "B-Rep内部体積差から除去量を算出" if analysis.get("brep_available") else "面数からポケット相当の除去量を概算"
                pocket_condition_text = master_condition_summary(pocket_cond)
                pocket_tool_id = pocket_tool["tool_id"]
                pocket_selection_reason = internal_tool_selection_reason(pocket_tool, 10, max_tool_diameter, "荒取り・ポケット加工")
            pocket_depth = min(bbox["z"], max(1.0, volume / max(1.0, bbox["x"] * bbox["y"])))

            def pocket_stage_feature(
                *,
                feature_type: str,
                stage_volume: float,
                stage_depth: float,
                reach_depth: float,
                diameter: float,
                effective_length: float,
                feed: float,
                ap: float,
                ae: float,
                tool_id: int | None,
                tool_name: str,
                note: str,
                condition_text: str,
                selection_reason: str,
                candidates: list[dict[str, Any]],
                method_extra: str = "",
            ) -> Feature:
                """ポケット荒取り1段分（stage_depthの厚みをstage_volumeだけ除去）の時間を算出する。"""
                plan_ap = axial_depth_for_plan(
                    ap, diameter, stage_depth, ratio=0.85, effective_length_mm=effective_length
                )
                lane_pitch = roughing_width_for_plan(ae, diameter, ratio=0.3)
                depth_passes = max(1, math.ceil(stage_depth / max(0.001, plan_ap)))
                lanes = max(1, math.ceil(min(bbox["x"], bbox["y"]) / lane_pitch))
                volume_path = stage_volume / max(0.001, plan_ap * lane_pitch)
                scan_path = max(bbox["x"], bbox["y"]) * lanes * depth_passes
                cutting_length = max(volume_path, scan_path)
                reachability, reachability_factor = reachability_assessment(
                    tool_diameter_mm=diameter,
                    required_depth_mm=reach_depth,
                    effective_length_mm=effective_length,
                    context="ポケット",
                )
                stage_sec = path_time_sec(
                    cutting_length,
                    feed,
                    approach_count=depth_passes * 2,
                    approach_mm=min(reach_depth + 5.0, 60.0),
                    rapid_feed_mm_min=rapid_feed,
                    efficiency=0.74,
                ) * reachability_factor
                extra = f"Z {depth_passes}段 x レーン {lanes}" + (f" / {method_extra}" if method_extra else "")
                return Feature(
                    feature_type,
                    f"推定除去体積 {stage_volume:.0f} mm3",
                    1,
                    tool_id,
                    tool_name,
                    "ポケット",
                    stage_sec,
                    note,
                    condition_text,
                    path_plan_summary(cutting_length, depth_passes * lanes, depth_passes * 2, method="等間隔走査", extra=extra),
                    selection_reason,
                    reachability,
                    feature_key="pocket_rough",
                    selection_candidates=candidates,
                )

            def catalog_pocket_stage_args(cond: sqlite3.Row, required_depth: float, context: str) -> dict[str, Any]:
                diameter = float(cond["outside_diameter_mm"])
                feed, ap, ae = condition_params(cond, catalog=True)
                return {
                    "diameter": diameter,
                    "effective_length": float(cond["effective_length_mm"]),
                    "feed": feed,
                    "ap": ap,
                    "ae": ae,
                    "tool_id": None,
                    "tool_name": f'{cond["manufacturer"]} {cond["series_code"]} φ{diameter:g} {cond["corner_radius_label"]}',
                    "note": (
                        f'STP形状から自動選定: {cond["work_material"]} '
                        f'{cond["hardness"]}, {cond["model_family"]}, rpm {cond["spindle_rpm"]}, '
                        f'ap {cond["axial_depth_mm"]}, ae {cond["radial_depth_mm"]}, '
                        f'出典 p.{cond["source_page"]}'
                    ),
                    "condition_text": catalog_condition_summary(cond),
                    "selection_reason": catalog_tool_selection_reason(
                        cond, pocket_target_diameter, required_depth, max_tool_diameter, context
                    ),
                }

            primary_args = {
                "diameter": pocket_diameter,
                "effective_length": pocket_effective_length,
                "feed": pocket_feed,
                "ap": pocket_ap,
                "ae": pocket_ae,
                "tool_id": pocket_tool_id,
                "tool_name": pocket_tool_name,
                "note": pocket_note,
                "condition_text": pocket_condition_text,
                "selection_reason": pocket_selection_reason,
                "candidates": pocket_candidates,
            }

            # ロングネックが選ばれた場合、標準長工具で届く上部は標準工具で荒取りし、
            # ロングネック（ap上限付き）は届かない深部だけに使う
            shallow_cond = None
            shallow_candidates: list[dict[str, Any]] = []
            if (
                pocket_catalog_cond is not None
                and is_long_neck(pocket_diameter, pocket_effective_length, pocket_ap)
            ):
                shallow_cond = auto_manufacturer_condition_for(
                    conn,
                    material_type,
                    pocket_target_diameter,
                    pocket_depth,
                    "ポケット",
                    max_tool_diameter,
                    candidates_out=shallow_candidates,
                    exclude_long_neck=True,
                )

            if shallow_cond is not None:
                shallow_args = catalog_pocket_stage_args(shallow_cond, pocket_depth, "荒取り・ポケット加工（上部）")
                shallow_reach = min(pocket_depth, shallow_args["effective_length"])
                if shallow_reach >= pocket_depth - 0.01:
                    # 平均深さまで標準工具で届くならロングネックは不要
                    features.append(
                        pocket_stage_feature(
                            feature_type="荒取り・ポケット加工",
                            stage_volume=volume,
                            stage_depth=pocket_depth,
                            reach_depth=pocket_depth,
                            candidates=shallow_candidates,
                            method_extra="標準長工具で全深さに到達",
                            **shallow_args,
                        )
                    )
                else:
                    # 除去体積は深さ方向に均等と仮定して上部/深部に按分する
                    shallow_volume = volume * shallow_reach / pocket_depth
                    deep_candidates: list[dict[str, Any]] = []
                    deep_cond = deep_pocket_condition_for(
                        conn,
                        material_type,
                        pocket_depth,
                        pocket_depth - shallow_reach,
                        max_tool_diameter,
                        candidates_out=deep_candidates,
                    )
                    deep_args = (
                        {
                            **catalog_pocket_stage_args(deep_cond, pocket_depth, "荒取り・ポケット加工（深部）"),
                            "candidates": deep_candidates,
                        }
                        if deep_cond is not None
                        else primary_args
                    )
                    features.append(
                        pocket_stage_feature(
                            feature_type="荒取り・ポケット加工（上部・標準工具）",
                            stage_volume=shallow_volume,
                            stage_depth=shallow_reach,
                            reach_depth=shallow_reach,
                            candidates=shallow_candidates,
                            method_extra=f"深さ 0〜{shallow_reach:.1f} mm",
                            **shallow_args,
                        )
                    )
                    features.append(
                        pocket_stage_feature(
                            feature_type="荒取り・ポケット加工（深部・ロングネック）",
                            stage_volume=max(0.0, volume - shallow_volume),
                            stage_depth=pocket_depth - shallow_reach,
                            reach_depth=pocket_depth,
                            method_extra=f"深さ {shallow_reach:.1f}〜{pocket_depth:.1f} mm",
                            **deep_args,
                        )
                    )
            else:
                features.append(
                    pocket_stage_feature(
                        feature_type="荒取り・ポケット加工",
                        stage_volume=volume,
                        stage_depth=pocket_depth,
                        reach_depth=pocket_depth,
                        **primary_args,
                    )
                )

        if analysis["face_count"] >= 18 or has_internal_machining:
            finish_tool = pick_tool(conn, "EM", 10, max_tool_diameter)
            finish_cond = condition_for(conn, finish_tool["tool_id"], material_type, "ポケット")
            finish_candidates: list[dict[str, Any]] = []
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
                    candidates_out=finish_candidates,
                    # 底面仕上げのピッチをaeから決めるため、ae極小の長刃側面条件は使わない
                    exclude_deep_flank=True,
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
                    feature_key="floor_finish",
                    selection_candidates=finish_candidates,
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
                    feature_key="wall_finish",
                    selection_candidates=finish_candidates,
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
                    feature_key="chamfer_deburr",
                )
            )

        corner_radius_groups = machining_features.get("corner_radii") or []
        if corner_radius_groups:
            total_corner_area = sum(float(group.get("total_area", 0.0)) for group in corner_radius_groups)
            total_corner_length = sum(float(group.get("total_length", 0.0)) for group in corner_radius_groups)
            total_corner_count = sum(int(group.get("count", 0)) for group in corner_radius_groups)
            smallest_radius = min(float(group.get("radius", 0.0)) for group in corner_radius_groups)
            if total_corner_area > 0 and smallest_radius > 0:
                corner_tool_target = min(6.0, max(0.5, smallest_radius * 1.6))
                corner_tool = pick_tool(
                    conn,
                    "EM",
                    corner_tool_target,
                    max_tool_diameter,
                    max_fit_diameter_mm=smallest_radius * 2.0,
                )
                corner_cond = condition_for(conn, corner_tool["tool_id"], material_type, "ポケット")
                corner_feed, _corner_ap, corner_ae = condition_params(corner_cond)
                corner_stepover = max(0.08, min(max(corner_ae * 0.35, smallest_radius * 0.35), 0.6))
                corner_cutting_length = max(total_corner_length, total_corner_area / corner_stepover)
                corner_approaches = max(total_corner_count, math.ceil(corner_cutting_length / 120.0))
                corner_reachability, corner_reachability_factor = reachability_assessment(
                    tool_diameter_mm=float(corner_tool["diameter_mm"]),
                    corner_radius_mm=smallest_radius,
                    effective_length_mm=float(corner_tool["max_depth_mm"]),
                    context="小R",
                )
                corner_sec = path_time_sec(
                    corner_cutting_length,
                    corner_feed * 0.55,
                    approach_count=corner_approaches,
                    approach_mm=6.0,
                    rapid_feed_mm_min=rapid_feed,
                    efficiency=0.66,
                ) * corner_reachability_factor
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
                        corner_reachability,
                        feature_key="fillet_finish",
                    )
                )

        # 「穴埋め・加工対象外」指定されたフィーチャをMC時間・放電候補から除外する
        if excluded_set:
            kept_features: list[Feature] = []
            for feature in features:
                if feature.feature_key and feature.feature_key in excluded_set:
                    excluded_features.append(
                        {
                            "feature_key": feature.feature_key,
                            "label": f"{feature.feature_type} / {feature.dimensions}",
                            "count": feature.quantity,
                            "kind": "feature",
                        }
                    )
                else:
                    kept_features.append(feature)
            features = kept_features
            kept_candidates: list[dict[str, Any]] = []
            for candidate in edm_candidates:
                if candidate.get("feature_key") in excluded_set:
                    excluded_features.append(
                        {
                            "feature_key": candidate.get("feature_key"),
                            "label": f'{candidate["feature_type"]} / {candidate["dimensions"]}',
                            "count": candidate.get("count", 1),
                            "kind": "edm",
                        }
                    )
                else:
                    kept_candidates.append(candidate)
            edm_candidates = kept_candidates

        safety_feature = safety_allowance_feature(features, estimate_mode, machining_features, max_tool_diameter)
        if safety_feature is not None:
            features.append(safety_feature)

        reachability_issues = [
            {
                "feature_type": feature.feature_type,
                "tool_name": feature.tool_name,
                "message": feature.reachability,
            }
            for feature in features
            if feature.reachability
        ]
        analysis["reachability_issues"] = reachability_issues

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
        if reachability_issues:
            confidence -= min(0.18, 0.04 * len(reachability_issues))
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

        # 3Dプレビュー用の工具パス: フィーチャ由来 + 外形基準のパス（上面走査・外周輪郭）
        overlay_list = list(machining_features.pop("overlays", []) or []) if machining_features else []
        raw_bounds = analysis.get("raw_bounds")
        if raw_bounds:
            overlay_list.append(
                {
                    "key": "face_top",
                    "kind": "raster",
                    "xmin": raw_bounds["xmin"],
                    "xmax": raw_bounds["xmax"],
                    "ymin": raw_bounds["ymin"],
                    "ymax": raw_bounds["ymax"],
                    "z": raw_bounds["zmax"] + 0.5,
                    "pitch": max(face_pick, (raw_bounds["ymax"] - raw_bounds["ymin"]) / 40, 1.0),
                }
            )
            overlay_list.append(
                {
                    "key": "side_walls",
                    "kind": "loops",
                    "xmin": raw_bounds["xmin"] - 1.0,
                    "xmax": raw_bounds["xmax"] + 1.0,
                    "ymin": raw_bounds["ymin"] - 1.0,
                    "ymax": raw_bounds["ymax"] + 1.0,
                    "z_top": raw_bounds["zmax"],
                    "z_bottom": raw_bounds["zmin"],
                    "loops": int(min(10, side_axial_passes)),
                }
            )
            try:
                finish_loop_count = int(min(6, wall_finish_z_passes))
            except NameError:
                finish_loop_count = 0
            if finish_loop_count:
                overlay_list.append(
                    {
                        "key": "wall_finish",
                        "kind": "loops",
                        "xmin": raw_bounds["xmin"] - 0.2,
                        "xmax": raw_bounds["xmax"] + 0.2,
                        "ymin": raw_bounds["ymin"] - 0.2,
                        "ymax": raw_bounds["ymax"] + 0.2,
                        "z_top": raw_bounds["zmax"],
                        "z_bottom": raw_bounds["zmin"],
                        "loops": finish_loop_count,
                    }
                )
        analysis["toolpath_overlays"] = overlay_list

        edm_reference_total_sec = sum(float(item["reference_sec"]) for item in edm_candidates)
        result = {
            "file_name": file_name,
            "material_type": material_type,
            "machine": dict(machine),
            "blank_allowance_mm": blank_allowance_mm,
            "analysis": analysis,
            "features": [asdict(feature) for feature in features],
            "tool_usage": tool_usage_rows,
            "edm_candidates": edm_candidates,
            "edm_policy": policy,
            "excluded_features": excluded_features,
            "excluded_keys": sorted(excluded_set),
            "breakdown": {
                "setup_sec": setup_sec,
                "machining_sec": machining_sec,
                "tool_change_sec": tool_change_sec,
                "rapid_sec": rapid_sec,
                "total_sec": total_sec,
                "edm_reference_sec": edm_reference_total_sec,
            },
            "confidence": confidence,
            "estimate_mode": estimate_mode if estimate_mode in SAFETY_PROFILES else "cautious",
            "estimate_mode_label": SAFETY_PROFILES.get(estimate_mode, SAFETY_PROFILES["cautious"])["label"],
            "condition_source": "STP形状からメーカー条件を自動選定" if use_manufacturer_conditions else "社内マスタ条件",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if extra_payload:
            result.update(extra_payload)
        if not save_history:
            return result
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


@app.errorhandler(InputError)
def handle_input_error(exc: InputError) -> tuple[Response, int]:
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def handle_too_large(_exc: Exception) -> tuple[Response, int]:
    return jsonify({"error": f"ファイルサイズが上限（{MAX_UPLOAD_MB} MB）を超えています。"}), 413


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception) -> Any:
    # APIはHTMLのエラーページではなく必ずJSONで返し、画面側でメッセージを表示できるようにする
    if isinstance(exc, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description or exc.name}), exc.code or 500
        return exc
    app.logger.exception("Unhandled error: %s", request.path)
    return jsonify({"error": f"サーバー内部でエラーが発生しました: {exc}"}), 500


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
    if material_type not in MATERIAL_TYPES:
        raise InputError(f"材質は {' / '.join(MATERIAL_TYPES)} から選択してください。")
    blank_allowance_mm = float(
        input_number(request.form, "blank_allowance_mm", "ブランク代", default=5.0, minimum=0, maximum=100)
    )
    machine_id = int(input_number(request.form, "machine_id", "使用機械", default=1, minimum=1, integer=True))
    use_manufacturer_conditions = request.form.get("use_manufacturer_conditions", "on") == "on"
    estimate_mode = request.form.get("estimate_mode", "cautious")
    edm_policy = edm_policy_from_form(request.form)
    excluded_raw = request.form.get("excluded_features", "")
    try:
        excluded_keys = {str(key) for key in json.loads(excluded_raw)} if excluded_raw else set()
    except (ValueError, TypeError):
        excluded_keys = set()

    fill_options = fill_options_from_form(request.form)
    wire_params = wire_params_from_form(request.form)
    nc_machine_id = int(
        input_number(request.form, "nc_machine_id", "NC穴加工機", default=machine_id, minimum=1, integer=True)
    )

    cleanup_old_uploads()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", upload.filename)
    path = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    upload.save(path)
    estimate_args = dict(
        use_manufacturer_conditions=use_manufacturer_conditions,
        estimate_mode=estimate_mode,
        edm_policy=edm_policy,
        excluded_keys=excluded_keys,
    )
    try:
        analyze_path = path
        fill_payload: dict[str, Any] | None = None
        if fill_options["drill_holes"] or fill_options["wire_shapes"]:
            fill_payload, filled_path, fill_items = prepare_filled_model(path, fill_options)
            if filled_path is not None:
                # 比較用に元モデルの時間も算出する（履歴には保存しない）
                original = estimate(
                    path, upload.filename, material_type, blank_allowance_mm, machine_id,
                    save_history=False, **estimate_args,
                )
                fill_payload["original_total_sec"] = original["breakdown"]["total_sec"]
                fill_payload["original_machining_sec"] = original["breakdown"]["machining_sec"]
                fill_payload["original_time_label"] = seconds_label(original["breakdown"]["total_sec"])
                analyze_path = filled_path
            if fill_items:
                fill_payload["processes"] = separate_process_plans(
                    fill_items, material_type, nc_machine_id, estimate_mode, wire_params
                )
        result = estimate(
            analyze_path,
            upload.filename,
            material_type,
            blank_allowance_mm,
            machine_id,
            extra_payload={"fill": fill_payload} if fill_payload else None,
            **estimate_args,
        )
        result["time_label"] = seconds_label(result["breakdown"]["total_sec"])
        return jsonify(result)
    except InputError:
        raise
    except Exception as exc:
        app.logger.exception("解析に失敗しました: %s", upload.filename)
        return jsonify({"error": f"解析に失敗しました: {exc}"}), 500


def prepare_filled_model(
    path: Path, fill_options: dict[str, Any]
) -> tuple[dict[str, Any], Path | None, list[dict[str, Any]]]:
    """埋めたモデルを作成し、画面表示用のペイロードと解析対象パスを返す。失敗時は元モデルで続行する。"""
    payload: dict[str, Any] = {"enabled": True, "options": fill_options}
    try:
        fill = build_filled_model(path, fill_options, path.with_suffix(""))
    except InputError as exc:
        payload["error"] = str(exc)
        return payload, None, []
    except Exception as exc:  # noqa: BLE001 - 埋めに失敗しても通常の見積もりは返す
        app.logger.exception("モデル埋めに失敗しました: %s", path.name)
        payload["error"] = f"形状を埋められませんでした（元モデルで算出しています）: {exc}"
        return payload, None, []
    rows = summarize_fill_items(fill["items"])
    payload.update(
        {
            "items": rows,
            "drill_count": sum(row["count"] for row in rows if row["category"] == "drill"),
            "wire_count": sum(row["count"] for row in rows if row["category"] == "wire"),
            "wire_cut_area_mm2": round(sum(row["cut_area_mm2"] for row in rows if row["category"] == "wire"), 1),
            "added_volume_mm3": round(fill["added_volume_mm3"], 1),
            "token": path.stem if fill["filled_path"] else None,
        }
    )
    if not rows:
        payload["message"] = "埋める対象の形状が見つかりませんでした。元モデルのまま算出しています。"
    return payload, fill["filled_path"], fill["items"]


def separate_process_plans(
    items: list[dict[str, Any]],
    material_type: str,
    nc_machine_id: int,
    estimate_mode: str,
    wire_params: dict[str, Any],
) -> dict[str, Any]:
    """埋めた形状を別工程（NC穴加工・ワイヤカット）で加工する時間。"""
    plans: dict[str, Any] = {}
    if any(item["category"] == "drill" for item in items):
        with db() as conn:
            machine = conn.execute("SELECT * FROM machines WHERE machine_id = ?", (nc_machine_id,)).fetchone()
            if machine is None:
                machine = conn.execute("SELECT * FROM machines ORDER BY machine_id LIMIT 1").fetchone()
        plans["nc_holes"] = nc_hole_plan(items, material_type, machine, estimate_mode) if machine else {
            "rows": [], "total_sec": None, "message": "機械マスタが未登録です。",
        }
    if any(item["category"] == "wire" for item in items):
        plans["wire"] = wire_cut_plan(items, wire_params)
    return plans


FILL_MODEL_KINDS = {"filled": "__filled.step", "bodies": "__fillbodies.step"}


@app.get("/api/fill-models/<token>/<kind>")
def api_fill_model(token: str, kind: str) -> Any:
    """埋めたモデル（filled）と埋めた部分（bodies）のSTEPを返す。3D比較表示用。"""
    if kind not in FILL_MODEL_KINDS or not re.fullmatch(r"[A-Za-z0-9_.-]+", token) or ".." in token:
        return jsonify({"error": "指定が不正です。"}), 400
    file_path = UPLOAD_DIR / f"{token}{FILL_MODEL_KINDS[kind]}"
    if not file_path.is_file():
        return jsonify({"error": "埋めたモデルが見つかりません（保存期間を過ぎた可能性があります）。"}), 404
    return send_file(file_path, mimetype="application/step", download_name=f"{kind}.step")


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
    fill = payload.get("fill") or {}
    if fill.get("items"):
        writer.writerow(["元モデルの合計時間", fill.get("original_time_label", "-")])
        writer.writerow([])
        writer.writerow(["埋めた形状（MC時間から除外）", "種別", "寸法", "数量", "ワイヤ切断面積mm2"])
        for row in fill["items"]:
            writer.writerow(
                [
                    "ドリル穴" if row["category"] == "drill" else "ワイヤカット",
                    row["kind"],
                    row["dimensions"],
                    row["count"],
                    row["cut_area_mm2"] if row["category"] == "wire" else "",
                ]
            )
        processes = fill.get("processes") or {}
        nc = processes.get("nc_holes")
        if nc:
            writer.writerow([])
            writer.writerow(["NC穴加工", "合計", seconds_label(nc["total_sec"]) if nc.get("total_sec") is not None else "未算出"])
            writer.writerow(["種別", "寸法", "数量", "回転数", "送りmm/min", "時間秒", "条件・備考"])
            for row in nc.get("rows", []):
                writer.writerow(
                    [row["kind"], row["dimensions"], row["count"], row["rpm"] or "", row["feed_mm_min"] or "",
                     row["sec"] if row["sec"] is not None else "未算出", row["note"]]
                )
        wire = processes.get("wire")
        if wire:
            writer.writerow([])
            writer.writerow(["ワイヤカット", "合計", seconds_label(wire["total_sec"]) if wire.get("total_sec") is not None else "未算出", wire.get("message", "")])
            writer.writerow(["種別", "寸法", "数量", "切断面積mm2", "時間秒"])
            for row in wire.get("rows", []):
                writer.writerow(
                    [row["kind"], row["dimensions"], row["count"], row["cut_area_mm2"],
                     row["sec"] if row["sec"] is not None else "未算出"]
                )
    writer.writerow([])
    writer.writerow(["フィーチャ", "寸法", "数量", "工具", "工程", "切削条件", "工具選定理由", "到達性", "加工パス", "加工時間秒", "備考"])
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
                feature.get("reachability", ""),
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
    data = request_json_object()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tools
            (tool_name, tool_type, diameter_mm, flute_count, max_depth_mm, material, roughing, finishing, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input_text(data, "tool_name", "工具名"),
                input_text(data, "tool_type", "工具種別", max_length=20),
                input_number(data, "diameter_mm", "工具径", minimum=0.01, maximum=200),
                input_number(data, "flute_count", "刃数", minimum=1, maximum=20, integer=True),
                input_number(data, "max_depth_mm", "最大深さ", minimum=0.01, maximum=1000),
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
    data = request_json_object()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cutting_conditions
            (tool_id, material_type, process_type, spindle_rpm, feed_rate_mm_min,
             depth_of_cut_mm, width_of_cut_mm, tool_change_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input_number(data, "tool_id", "工具", minimum=1, integer=True),
                input_text(data, "material_type", "材質", max_length=40),
                input_text(data, "process_type", "工程", max_length=40),
                input_number(data, "spindle_rpm", "回転数", minimum=1, maximum=200000, integer=True),
                input_number(data, "feed_rate_mm_min", "送り速度", minimum=0.1, maximum=100000),
                input_number(data, "depth_of_cut_mm", "切込み深さ", minimum=0.001, maximum=100),
                input_number(data, "width_of_cut_mm", "切込み幅", minimum=0.001, maximum=200),
                input_number(data, "tool_change_sec", "工具交換秒", minimum=0, maximum=600, integer=True),
            ),
        )
    return jsonify({"condition_id": cur.lastrowid})


@app.delete("/api/conditions/<int:condition_id>")
def api_delete_condition(condition_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM cutting_conditions WHERE condition_id = ?", (condition_id,))
        ensure_operational_master(conn)
    return jsonify({"ok": True})


def machine_values_from_json(data: dict[str, Any]) -> tuple[Any, ...]:
    max_tool_diameter = input_number(data, "max_tool_diameter_mm", "最大工具径", default=0.0, minimum=0, maximum=200)
    return (
        input_text(data, "machine_name", "機械名"),
        input_number(data, "axis_count", "軸数", minimum=3, maximum=9, integer=True),
        input_number(data, "rapid_feed_mm_min", "早送り", minimum=1, maximum=200000),
        input_number(data, "atc_time_sec", "ATC秒", minimum=0, maximum=600, integer=True),
        input_number(data, "max_spindle_rpm", "最大回転数", minimum=1, maximum=200000, integer=True),
        max_tool_diameter or None,
        input_number(data, "setup_time_min", "段取り分", minimum=0, maximum=1440, integer=True),
        input_text(data, "memo", "メモ", max_length=500, required=False),
    )


@app.post("/api/machines")
def api_create_machine() -> Response:
    values = machine_values_from_json(request_json_object())
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO machines
            (machine_name, axis_count, rapid_feed_mm_min, atc_time_sec,
             max_spindle_rpm, max_tool_diameter_mm, setup_time_min, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return jsonify({"machine_id": cur.lastrowid})


@app.put("/api/machines/<int:machine_id>")
def api_update_machine(machine_id: int) -> Response:
    values = machine_values_from_json(request_json_object())
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
            (*values, machine_id),
        )
    if cur.rowcount == 0:
        return jsonify({"error": "機械マスタが見つかりません。"}), 404
    return jsonify({"ok": True})


@app.delete("/api/machines/<int:machine_id>")
def api_delete_machine(machine_id: int) -> Response:
    with db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM machines WHERE machine_id != ?", (machine_id,)).fetchone()[0]
        if remaining == 0:
            raise InputError("機械マスタが1件のみのため削除できません。先に別の機械を登録してください。")
        conn.execute("DELETE FROM machines WHERE machine_id = ?", (machine_id,))
        ensure_operational_master(conn)
    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
