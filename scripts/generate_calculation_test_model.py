# -*- coding: utf-8 -*-
"""加工時間算出ツールの総合テスト用STEPモデルを生成する。

形状構成:
- ベース: 120 x 80 x 30 mm
- φ6.6 貫通穴 x4
- φ6.6 / φ11 深さ6 mm の座ぐり穴 x2
- φ10 深さ18 mm の止まり穴 x1
- 38 x 24 x 深さ10 mm、隅R4のポケット x1
- 幅8 x 長さ36 x 深さ8 mm の丸端溝 x1
- 幅2.5 x 長さ24 x 深さ15 mm の狭い深溝 x1（放電候補）
- φ8 深さ20 mm の横穴 x1
- φ18 高さ6 mm の円形ボス x1
"""

from pathlib import Path

import cadquery as cq


BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "samples" / "calculation_test_model.stp"


def build_model() -> cq.Workplane:
    length = 120.0
    width = 80.0
    height = 30.0
    top_z = height / 2.0

    part = cq.Workplane("XY").box(length, width, height)

    # 四隅の取付用貫通穴。
    part = (
        part.faces(">Z")
        .workplane(centerOption="CenterOfBoundBox")
        .pushPoints([(-50.0, -30.0), (50.0, -30.0), (-50.0, 30.0), (50.0, 30.0)])
        .hole(6.6)
    )

    # 座ぐり穴。下穴は貫通、座ぐり部は深さ6 mm。
    part = (
        part.faces(">Z")
        .workplane(centerOption="CenterOfBoundBox")
        .pushPoints([(-22.0, -28.0), (0.0, -28.0)])
        .cboreHole(6.6, 11.0, 6.0)
    )

    # φ10、深さ18 mmの止まり穴。
    part = (
        part.faces(">Z")
        .workplane(centerOption="CenterOfBoundBox")
        .pushPoints([(25.0, -24.0)])
        .hole(10.0, 18.0)
    )

    # 隅R4の標準ポケット。
    pocket = (
        cq.Workplane("XY")
        .center(-25.0, 12.0)
        .rect(38.0, 24.0)
        .extrude(10.0)
        .edges("|Z")
        .fillet(4.0)
        .translate((0.0, 0.0, top_z - 10.0))
    )
    part = part.cut(pocket)

    # 通常の丸端溝。
    standard_slot = (
        cq.Workplane("XY")
        .center(18.0, -3.0)
        .slot2D(36.0, 8.0, 0.0)
        .extrude(8.0)
        .translate((0.0, 0.0, top_z - 8.0))
    )
    part = part.cut(standard_slot)

    # 幅2.5 mm、深さ15 mmの狭い深溝。既定条件では放電候補になる。
    deep_narrow_slot = (
        cq.Workplane("XY")
        .center(14.0, 30.0)
        .rect(24.0, 2.5)
        .extrude(15.0)
        .translate((0.0, 0.0, top_z - 15.0))
    )
    part = part.cut(deep_narrow_slot)

    # +X側面からの止まり横穴。
    part = (
        part.faces(">X")
        .workplane(centerOption="CenterOfBoundBox")
        .hole(8.0, 20.0)
    )

    # 上面の円形ボス。上端にC1面取りを付ける。
    boss = (
        cq.Workplane("XY", origin=(40.0, 18.0, top_z))
        .circle(9.0)
        .extrude(6.0)
        .edges(">Z")
        .chamfer(1.0)
    )
    return part.union(boss)


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)
    model = build_model()
    cq.exporters.export(model, str(OUTPUT), exportType=cq.exporters.ExportTypes.STEP)
    print(f"written: {OUTPUT}")


if __name__ == "__main__":
    main()
