# -*- coding: utf-8 -*-
"""微細形状・放電置き換え検証用のサンプルSTPを生成する。

含まれる形状:
- 幅2mm x 深さ15mmの狭い深溝（型彫り放電候補: 幅・深さしきい値）
- φ1.0 x 深さ12mmの微細深穴（細穴放電候補: 深さ/幅比）
- φ2.4 x 深さ5mmの微細穴（小径EMでヘリカル加工可能）
- 深さ18mm・隅R1.0のポケット（縦隅R: 工具リーチ次第で切削/放電）
- 深さ6mm・隅R2.0のポケット（縦隅R: 小径EMで切削可能）
- 幅4mm x 深さ8mm・テーパ3°の溝（型彫り放電候補: テーパ角しきい値）
- 上部φ12 x 深さ8mm・テーパ5°の円形キャビティ（型彫り放電候補: テーパ角しきい値）
"""
from pathlib import Path

import cadquery as cq

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "samples" / "fine_feature_test_block.stp"


def build_model() -> cq.Workplane:
    part = cq.Workplane("XY").box(120.0, 80.0, 30.0)

    # 幅2mm・深さ15mmの狭い深溝
    # 注意: .center() は後続チェーンのworkplane原点に持ち越されるため使わない
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(-30.0, 25.0)])
        .rect(40.0, 2.0)
        .cutBlind(-15.0)
    )

    # φ1.0 x 深さ12mm 微細深穴（L/D=12 → 細穴放電候補）
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(30.0, 25.0), (40.0, 25.0)])
        .circle(0.5)
        .cutBlind(-12.0)
    )

    # φ2.4 x 深さ5mm 微細穴（ヘリカル加工可能）
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(30.0, 10.0), (40.0, 10.0), (50.0, 10.0)])
        .hole(2.4, 5.0)
    )

    # 深さ18mm・隅R1.0の深ポケット（上面 z=15 から下へ18mm）
    pocket_deep = (
        cq.Workplane("XY")
        .center(-25.0, -15.0)
        .rect(30.0, 20.0)
        .extrude(18.0)
        .edges("|Z")
        .fillet(1.0)
        .translate((0.0, 0.0, 15.0 - 18.0))
    )
    part = part.cut(pocket_deep)

    # 深さ6mm・隅R2.0の浅ポケット（上面 z=15 から下へ6mm）
    pocket_shallow = (
        cq.Workplane("XY")
        .center(25.0, -15.0)
        .rect(30.0, 20.0)
        .extrude(6.0)
        .edges("|Z")
        .fillet(2.0)
        .translate((0.0, 0.0, 15.0 - 6.0))
    )
    part = part.cut(pocket_shallow)

    # 幅4mm・深さ8mm・テーパ3°（抜き勾配）の溝
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(0.0, -30.0)])
        .rect(30.0, 4.0)
        .cutBlind(-8.0, taper=3.0)
    )

    # 上部φ12・深さ8mm・テーパ5°の円形キャビティ
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(0.0, 25.0)])
        .circle(6.0)
        .cutBlind(-8.0, taper=5.0)
    )

    return part


def main() -> None:
    model = build_model()
    cq.exporters.export(model, str(OUTPUT), exportType=cq.exporters.ExportTypes.STEP)
    print(f"written: {OUTPUT}")


if __name__ == "__main__":
    main()
