# -*- coding: utf-8 -*-
"""入れ子（別ソリッド）が貫通抜き窓に収まったプレートの「埋めて計算」検証用サンプルSTPを生成する。

金型の実データ（301 スキャナカバー）と同じ構成を、顧客データを使わずに再現する。
モデル全体で貫通を調べると入れ子に当たって「貫通していない」と誤判定するため、
開口を持つソリッド（本体）だけで判定できているかを確かめる。

含まれる形状（本体: 120 x 80 x 30 プレート、z[-30, 0]）:
- 本体の底面から上面まで貫通した 30 x 16 の抜き窓（ワイヤカット対象）
- 抜き窓に収まる入れ子ソリッド 30 x 16 x 高さ20（底面から z=-10 まで。上側10mmは空いたまま）
- 本体の φ6.6 貫通穴 x2（ドリル穴）
- 本体上面の 24 x 14 x 深さ5 の止まりポケット（MC加工として残る）
"""
from pathlib import Path

import cadquery as cq

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "samples" / "insert_plate_test.stp"


def build_model() -> cq.Assembly | cq.Compound:
    plate = cq.Workplane("XY").box(120.0, 80.0, 30.0).translate((0.0, 0.0, -15.0))
    window = cq.Workplane("XY").center(20.0, 0.0).rect(30.0, 16.0).extrude(40.0).translate((0.0, 0.0, -35.0))
    plate = plate.cut(window)
    for x in (-45.0, -30.0):
        hole = cq.Workplane("XY").center(x, 25.0).circle(3.3).extrude(40.0).translate((0.0, 0.0, -35.0))
        plate = plate.cut(hole)
    pocket = cq.Workplane("XY").center(-30.0, -15.0).rect(24.0, 14.0).extrude(5.0).translate((0.0, 0.0, -5.0))
    plate = plate.cut(pocket)
    insert = cq.Workplane("XY").center(20.0, 0.0).rect(30.0, 16.0).extrude(20.0).translate((0.0, 0.0, -30.0))
    return cq.Compound.makeCompound([plate.val(), insert.val()])


def main() -> None:
    model = build_model()
    cq.exporters.export(model, str(OUTPUT), "STEP")
    print(f"written: {OUTPUT}")


if __name__ == "__main__":
    main()
