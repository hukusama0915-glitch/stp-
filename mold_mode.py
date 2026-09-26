"""金型モード: 自由曲面・微細形状の多い金型部品を、残り取り＋多段仕上げとして見積もる。

角物モード（平面・ポケット・溝・穴の組み合わせ）では、金型の表面形状加工のように
「大径から小径へ工具を段階的に落とし、各工具で残り取りと等高線仕上げを繰り返す」
加工を表現できない。ここでは B-Rep を上から見た高さマップにして、工具形状ごとに
到達できる面（グレー・モルフォロジーの opening）を計算し、工具段階ごとの
残り取り体積・加工範囲・面積を求める。

係数（ピッチ等）は 301_DJ30-RC6-2701（スキャナカバー）の実績NC / CAM-TOOL加工情報シート
（87工程・140.2h）から逆算した値。1件の実績による校正なので、実績が増えたら見直す。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 工具の段階（mm）。金型の残り取りで一般的な径の並び
MOLD_TOOL_LADDER: tuple[float, ...] = (16.0, 10.0, 6.0, 3.0, 1.5, 1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2)
# この径以上の工具は加工面全体を残り取り・仕上げする（実績: φ16/φ10/φ6 は全面）
WHOLE_SURFACE_MIN_DIAMETER = 6.0
# ボール仕上げの狙いスカラップ高さ mm（実績 φ0.8/φ0.6 ボールの距離から逆算）
BALL_SCALLOP_MM = 0.003
# ラジアス/フラット工具の等高線Zピッチ = 係数 x 工具径（実績 φ6/φ3/φ1.5 R0.1 から逆算）
BULL_Z_PITCH_PER_DIAMETER = 0.008
# 残り取り（等高荒取り・中仕上げ）の実効ピッチ = 係数 x 仕上げZピッチ
REST_PITCH_FACTOR = 2.0
# ボール工具を使う径（これ以下はボール、超えるとコーナR付きラジアス）
BALL_MAX_DIAMETER = 1.0
# 小径工具の加工範囲をまとめる距離 mm（離れた形状を1つの加工範囲に束ねる）
REGION_LINK_MM = 20.0
# 首下長の判定で見る工具周りの逃げ mm（首・ホルダが当たらない範囲）
NECK_CLEARANCE_MM = 2.0
# 高さマップのセル数上限（Render無料プランのメモリを考慮）
MAX_HEIGHTMAP_CELLS = 2_600_000


@dataclass
class MoldStage:
    diameter: float
    corner_radius: float
    tool_shape: str  # "ball" | "radius"
    whole_surface: bool
    region_rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    region_projected_area: float = 0.0
    steep_area: float = 0.0
    flat_area: float = 0.0
    rest_volume: float = 0.0
    required_depth: float = 0.0
    reason: str = ""
    # 仕上げ: "final"=所定回数の仕上げ / "semi"=中仕上げ1回 / "none"=仕上げなし
    finish_role: str = "final"
    finish_rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    finish_projected_area: float = 0.0
    finish_steep_area: float = 0.0
    finish_flat_area: float = 0.0
    finish_reason: str = ""


@dataclass
class MoldPlan:
    resolution: float
    bounds: dict[str, float]
    machined_floor_z: float
    through_area: float
    surface_area: float
    roughing_volume: float
    min_concave_radius: float | None
    concave_radius_counts: dict[str, int]
    stages: list[MoldStage]
    notes: list[str]


# ---------------------------------------------------------------- 高さマップ

def build_heightmap(shape: Any) -> tuple[np.ndarray, float, float, float]:
    """B-Rep をテッセレーションし、上から見た最高点の高さマップ（Z-buffer）を作る。"""
    bb = shape.BoundingBox()
    area = max(1.0, float(bb.xlen) * float(bb.ylen))
    res = max(0.1, math.sqrt(area / MAX_HEIGHTMAP_CELLS))
    vertices, triangles = shape.tessellate(0.01, 0.2)
    points = np.array([vertex.toTuple() for vertex in vertices], dtype=np.float64)
    tris = points[np.array(triangles, dtype=np.int64)]
    nx = int(math.ceil(float(bb.xlen) / res)) + 1
    ny = int(math.ceil(float(bb.ylen) / res)) + 1
    heights = np.full((ny, nx), -np.inf, dtype=np.float32)
    x0, y0 = float(bb.xmin), float(bb.ymin)
    for tri in tris:
        xs = (tri[:, 0] - x0) / res
        ys = (tri[:, 1] - y0) / res
        i0 = max(0, int(math.ceil(xs.min())))
        i1 = min(nx - 1, int(math.floor(xs.max())))
        j0 = max(0, int(math.ceil(ys.min())))
        j1 = min(ny - 1, int(math.floor(ys.max())))
        if i1 < i0 or j1 < j0:
            continue
        ax, bx, cx = xs
        ay, by, cy = ys
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue  # 垂直な三角形は上から見えない
        gi, gj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
        l1 = ((by - cy) * (gi - cx) + (cx - bx) * (gj - cy)) / det
        l2 = ((cy - ay) * (gi - cx) + (ax - cx) * (gj - cy)) / det
        l3 = 1.0 - l1 - l2
        inside = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not inside.any():
            continue
        z = l1 * tri[0, 2] + l2 * tri[1, 2] + l3 * tri[2, 2]
        view = heights[j0 : j1 + 1, i0 : i1 + 1]
        np.maximum(view, np.where(inside, z, -np.inf).astype(np.float32), out=view)
    heights[~np.isfinite(heights)] = float(bb.zmin)
    return heights, res, x0, y0


def _max_pool(values: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return values
    ny, nx = values.shape
    padded = np.pad(values, ((0, (-ny) % factor), (0, (-nx) % factor)), constant_values=-1e9)
    return padded.reshape(padded.shape[0] // factor, factor, padded.shape[1] // factor, factor).max(axis=(1, 3))


def _upsample(values: np.ndarray, factor: int, shape: tuple[int, int]) -> np.ndarray:
    if factor <= 1:
        return values
    return np.repeat(np.repeat(values, factor, axis=0), factor, axis=1)[: shape[0], : shape[1]]


def _tool_kernel(diameter: float, corner_radius: float, res: float) -> tuple[list[tuple[int, int, float]], int]:
    """工具先端からの高さプロファイル（中心からの距離 → 刃先より上がる量）。"""
    radius = diameter / 2
    corner = min(corner_radius, radius)
    flat = radius - corner
    reach = int(math.floor(radius / res))
    kernel: list[tuple[int, int, float]] = []
    for j in range(-reach, reach + 1):
        for i in range(-reach, reach + 1):
            dist = math.hypot(i, j) * res
            if dist > radius + 1e-9:
                continue
            lift = 0.0 if dist <= flat else corner - math.sqrt(max(0.0, corner * corner - (dist - flat) ** 2))
            kernel.append((j, i, lift))
    return kernel, reach


def machined_surface(heights: np.ndarray, res: float, diameter: float, corner_radius: float, outside_z: float) -> np.ndarray:
    """工具で上から削ったときに残る面（opening）を元の格子で返す。大径は粗い格子で計算する。"""
    factor = max(1, int(round(max(res, diameter / 12.0) / res)))
    coarse = _max_pool(heights, factor)
    cres = res * factor
    kernel, reach = _tool_kernel(diameter, corner_radius, cres)
    ny, nx = coarse.shape
    padded = np.pad(coarse, reach, constant_values=outside_z)
    tip = np.full(coarse.shape, -1e9, dtype=np.float32)
    for j, i, lift in kernel:
        np.maximum(tip, padded[reach + j : reach + j + ny, reach + i : reach + i + nx] - lift, out=tip)
    padded_tip = np.pad(tip, reach, constant_values=1e9)
    surface = np.full(coarse.shape, 1e9, dtype=np.float32)
    for j, i, lift in kernel:
        np.minimum(surface, padded_tip[reach - j : reach - j + ny, reach - i : reach - i + nx] + lift, out=surface)
    return _upsample(np.maximum(surface, coarse), factor, heights.shape)


def local_top(heights: np.ndarray, res: float, radius: float) -> np.ndarray:
    """半径 radius 内の最高点（工具首の逃げを考えた局所的な上端）。"""
    factor = max(1, int(round(max(res, radius / 4.0) / res)))
    coarse = _max_pool(heights, factor)
    cres = res * factor
    reach = max(1, int(math.ceil(radius / cres)))
    ny, nx = coarse.shape
    padded = np.pad(coarse, reach, constant_values=-1e9)
    out = coarse.copy()
    for j in range(-reach, reach + 1):
        for i in range(-reach, reach + 1):
            if math.hypot(i, j) * cres > radius + cres:
                continue
            np.maximum(out, padded[reach + j : reach + j + ny, reach + i : reach + i + nx], out=out)
    return _upsample(out, factor, heights.shape)


# ---------------------------------------------------------------- 凹R面

def concave_curved_faces(shape: Any) -> list[dict[str, float]]:
    """凹の円筒・球・トーラス面（隅R・フィレット）の半径と範囲。工具径の下限と小径工具の範囲に使う。"""
    import cadquery as cq  # type: ignore

    faces: list[dict[str, float]] = []
    for face in shape.Faces():
        try:
            geom_type = face.geomType()
            if geom_type not in {"CYLINDER", "SPHERE", "TORUS"}:
                continue
            center = face.Center()
            normal = face.normalAt(center)
            adaptor = face._geomAdaptor()
            if geom_type == "CYLINDER":
                surface = adaptor.Cylinder()
                radius = float(surface.Radius())
                loc, direction = surface.Axis().Location(), surface.Axis().Direction()
                rel = cq.Vector(center.x - loc.X(), center.y - loc.Y(), center.z - loc.Z())
                axis = cq.Vector(direction.X(), direction.Y(), direction.Z())
                outward = rel - axis * rel.dot(axis)
            elif geom_type == "SPHERE":
                surface = adaptor.Sphere()
                radius = float(surface.Radius())
                loc = surface.Location()
                outward = cq.Vector(center.x - loc.X(), center.y - loc.Y(), center.z - loc.Z())
            else:
                surface = adaptor.Torus()
                radius = float(surface.MinorRadius())
                loc, direction = surface.Axis().Location(), surface.Axis().Direction()
                rel = cq.Vector(center.x - loc.X(), center.y - loc.Y(), center.z - loc.Z())
                axis = cq.Vector(direction.X(), direction.Y(), direction.Z())
                radial = rel - axis * rel.dot(axis)
                if radial.Length < 1e-9:
                    continue
                tube_center = cq.Vector(loc.X(), loc.Y(), loc.Z()) + radial.normalized() * float(surface.MajorRadius())
                outward = cq.Vector(center.x, center.y, center.z) - tube_center
            # 法線が曲率中心側を向く = 材料が外側 = 凹R
            if normal.dot(outward) >= 0:
                continue
            bbox = face.BoundingBox()
            faces.append(
                {
                    "radius": radius,
                    "xmin": float(bbox.xmin),
                    "xmax": float(bbox.xmax),
                    "ymin": float(bbox.ymin),
                    "ymax": float(bbox.ymax),
                    "zmin": float(bbox.zmin),
                    "zmax": float(bbox.zmax),
                    "area": float(face.Area()),
                }
            )
        except Exception:  # noqa: BLE001 - 解析できない面は無視する
            continue
    return faces


# ---------------------------------------------------------------- 加工範囲

def _cluster_rects(mask: np.ndarray, cell: float, x0: float, y0: float, link_cells: int, margin: float) -> list[tuple[float, float, float, float]]:
    """粗い格子のマスクを近いもの同士で束ね、各まとまりの外接矩形（mm）を返す。"""
    ny, nx = mask.shape
    labels = np.full(mask.shape, -1, dtype=np.int32)
    rects: list[tuple[float, float, float, float]] = []
    cells = list(zip(*np.nonzero(mask)))
    for sj, si in cells:
        if labels[sj, si] >= 0:
            continue
        label = len(rects)
        labels[sj, si] = label
        queue = deque([(sj, si)])
        jmin = jmax = sj
        imin = imax = si
        while queue:
            j, i = queue.popleft()
            jmin, jmax, imin, imax = min(jmin, j), max(jmax, j), min(imin, i), max(imax, i)
            for jj in range(max(0, j - link_cells), min(ny, j + link_cells + 1)):
                for ii in range(max(0, i - link_cells), min(nx, i + link_cells + 1)):
                    if mask[jj, ii] and labels[jj, ii] < 0:
                        labels[jj, ii] = label
                        queue.append((jj, ii))
        rects.append(
            (
                x0 + imin * cell - margin,
                x0 + (imax + 1) * cell + margin,
                y0 + jmin * cell - margin,
                y0 + (jmax + 1) * cell + margin,
            )
        )
    return rects


def _rects_mask(rects: list[tuple[float, float, float, float]], shape: tuple[int, int], res: float, x0: float, y0: float) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for xmin, xmax, ymin, ymax in rects:
        i0 = max(0, int(math.floor((xmin - x0) / res)))
        i1 = min(shape[1], int(math.ceil((xmax - x0) / res)) + 1)
        j0 = max(0, int(math.floor((ymin - y0) / res)))
        j1 = min(shape[0], int(math.ceil((ymax - y0) / res)) + 1)
        if i1 > i0 and j1 > j0:
            mask[j0:j1, i0:i1] = True
    return mask


def ladder_for(min_concave_radius: float | None) -> list[float]:
    """凹Rの最小半径から、使う工具の段階（最小工具まで）を決める。"""
    if min_concave_radius is None or min_concave_radius <= 0:
        smallest = WHOLE_SURFACE_MIN_DIAMETER
    else:
        limit = 2.0 * min_concave_radius + 0.01  # ボール半径 ≤ 凹R
        fitting = [d for d in MOLD_TOOL_LADDER if d <= limit]
        smallest = max(fitting) if fitting else MOLD_TOOL_LADDER[-1]
    return [d for d in MOLD_TOOL_LADDER if d >= smallest - 1e-9]


def tool_corner_radius(diameter: float) -> tuple[float, str]:
    if diameter <= BALL_MAX_DIAMETER + 1e-9:
        return diameter / 2, "ball"
    if diameter >= 16:
        return 1.0, "radius"  # 高送り荒取りカッタ相当
    return min(0.3, diameter / 2), "radius"


def finish_z_pitch(diameter: float, corner_radius: float, tool_shape: str) -> float:
    """急斜面（壁）の等高線仕上げピッチ mm。"""
    if tool_shape == "ball":
        return max(0.01, 2.0 * math.sqrt(max(1e-9, 2.0 * corner_radius * BALL_SCALLOP_MM - BALL_SCALLOP_MM**2)))
    return min(0.15, max(0.01, BULL_Z_PITCH_PER_DIAMETER * diameter))


def finish_flat_pitch(diameter: float, corner_radius: float, tool_shape: str) -> float:
    """緩斜面（床）の走査ピッチ mm。フラット部のある工具は刃幅の35%。"""
    z_pitch = finish_z_pitch(diameter, corner_radius, tool_shape)
    if tool_shape == "ball":
        return z_pitch
    return max(z_pitch, 0.35 * max(0.0, diameter - 2 * corner_radius))


def load_step_shape(path: Any) -> Any:
    import cadquery as cq  # type: ignore

    shape = cq.importers.importStep(str(path)).val()
    if shape is None or not hasattr(shape, "Faces"):
        raise ValueError("STPからB-Repソリッドを取得できませんでした")
    return shape


def plan_mold_machining(shape: Any, max_tool_diameter: float | None = None) -> MoldPlan:
    heights, res, x0, y0 = build_heightmap(shape)
    bb = shape.BoundingBox()
    zmin, zmax = float(heights.min()), float(heights.max())
    # 底まで抜けているセルは上からの切削対象外（ワイヤ・放電や入れ子の穴）
    through = heights <= zmin + 0.05
    notes: list[str] = []
    if through.all():
        raise ValueError("上から加工できる面が見つかりませんでした")
    floor_z = float(heights[~through].min())
    through_area = float(through.sum()) * res * res
    if through_area > 0:
        notes.append(f"底まで抜けた範囲 {through_area:.0f} mm2 は切削対象外（ワイヤ・放電を想定）")
    work = np.where(through, floor_z, heights).astype(np.float32)

    grad_y, grad_x = np.gradient(work, res)
    slope = np.hypot(grad_x, grad_y)
    cell_area = (res * res * np.sqrt(1.0 + slope * slope)).astype(np.float32)
    cell_area[through] = 0.0
    steep = slope >= 1.0  # 45°以上を壁（等高線）とみなす
    surface_area = float(cell_area.sum())

    concave = [face for face in concave_curved_faces(shape) if face["zmax"] > floor_z - 1e-6]
    radius_counts: dict[str, int] = {}
    for face in concave:
        key = f"R{face['radius']:.2f}"
        radius_counts[key] = radius_counts.get(key, 0) + 1
    # 面積が極小の面（テッセレーション誤差・微小な面）は工具径の下限決めに使わない
    meaningful = [face for face in concave if face["area"] >= 0.05]
    min_radius = min((face["radius"] for face in meaningful), default=None)
    ladder = ladder_for(min_radius)
    if max_tool_diameter:
        limited = [d for d in ladder if d <= max_tool_diameter + 1e-9]
        if len(limited) < len(ladder):
            notes.append(f"機械の最大工具径 φ{max_tool_diameter:g} で工具段階を制限")
        ladder = limited or [min(ladder)]

    outside_z = zmin - 50.0
    previous_surface = np.full(work.shape, zmax, dtype=np.float32)
    previous_diameter: float | None = None
    stages: list[MoldStage] = []
    roughing_volume = 0.0
    coarse_factor = max(1, int(round(2.0 / res)))
    link_cells = max(1, int(round(REGION_LINK_MM / (res * coarse_factor))))
    whole_diameters = [d for d in ladder if d >= WHOLE_SURFACE_MIN_DIAMETER]
    last_whole = min(whole_diameters) if len(whole_diameters) > 1 else None

    def face_region(faces: list[dict[str, float]], margin: float) -> tuple[np.ndarray, list[tuple[float, ...]]]:
        need = _rects_mask([(f["xmin"], f["xmax"], f["ymin"], f["ymax"]) for f in faces], work.shape, res, x0, y0)
        need &= ~through
        if not need.any():
            return need, []
        coarse_need = _max_pool(need.astype(np.float32), coarse_factor) > 0
        rects = _cluster_rects(coarse_need, res * coarse_factor, x0, y0, link_cells=link_cells, margin=margin)
        return _rects_mask(rects, work.shape, res, x0, y0) & ~through, [tuple(round(float(v), 1) for v in r) for r in rects]

    def radii_label(faces: list[dict[str, float]]) -> str:
        radii = sorted({round(face["radius"], 2) for face in faces})
        suffix = " など" if len(radii) > 4 else ""
        return f"{', '.join(f'R{r:g}' for r in radii[:4])}{suffix} {len(faces)}面"

    for index, diameter in enumerate(ladder):
        corner, tool_shape = tool_corner_radius(diameter)
        surface = np.minimum(machined_surface(work, res, diameter, corner, outside_z), previous_surface)
        removed = np.maximum(0.0, previous_surface - surface)
        removed[through] = 0.0
        whole = diameter >= WHOLE_SURFACE_MIN_DIAMETER
        stage = MoldStage(diameter=diameter, corner_radius=corner, tool_shape=tool_shape, whole_surface=whole)
        if index == 0:
            # 最初の工具は荒取り専用
            roughing_volume = float(removed.sum()) * res * res
            region = ~through
            stage.reason = "全面の荒取り"
            stage.finish_role = "none"
        elif whole:
            region = ~through
            stage.reason = "全面の残り取り"
            stage.finish_role = "final" if (last_whole is None or diameter == last_whole) else "semi"
            finish_region = region
        else:
            # 残り取り: 前の工具では入らない凹R面（隅R・フィレット）のまとまり。
            # 削り残しマップは格子の粗さによる誤差が大きいので範囲決めには使わない
            limit = previous_diameter / 2 - 1e-3
            needing = [face for face in concave if face["radius"] < limit]
            region, stage.region_rects = face_region(needing, margin=max(1.0, previous_diameter))
            stage.reason = f"φ{previous_diameter:g}で入らない凹R {radii_label(needing)}の範囲"
            # 仕上げ: この工具が仕上げを受け持つ凹R（この工具の半径以上、前の工具の半径未満）
            finishing = [face for face in needing if face["radius"] >= diameter / 2 - 0.01]
            finish_region, stage.finish_rects = face_region(finishing, margin=max(1.0, diameter))
            stage.finish_role = "final" if finish_region.any() else "none"
            if finishing:
                stage.finish_reason = f"凹R {radii_label(finishing)}を仕上げる範囲"
        if region.any():
            stage.region_projected_area = float(region.sum()) * res * res
            stage.steep_area = float(cell_area[region & steep].sum())
            stage.flat_area = float(cell_area[region & ~steep].sum())
            stage.rest_volume = float(removed[region].sum()) * res * res
            if stage.finish_role != "none":
                stage.finish_projected_area = float(finish_region.sum()) * res * res
                stage.finish_steep_area = float(cell_area[finish_region & steep].sum())
                stage.finish_flat_area = float(cell_area[finish_region & ~steep].sum())
            # 必要な首下長: 工具が働く面の、周囲（工具首の逃げ）の上端からの深さ
            tops = local_top(work, res, diameter / 2 + NECK_CLEARANCE_MM)
            depth = (tops - work)[region & (cell_area > 0)]
            stage.required_depth = float(np.percentile(depth, 98)) if depth.size else 0.0
            stages.append(stage)
        previous_surface = surface
        previous_diameter = diameter

    return MoldPlan(
        resolution=res,
        bounds={"xmin": x0, "ymin": y0, "zmin": zmin, "zmax": zmax},
        machined_floor_z=floor_z,
        through_area=through_area,
        surface_area=surface_area,
        roughing_volume=roughing_volume,
        min_concave_radius=min_radius,
        concave_radius_counts=dict(sorted(radius_counts.items(), key=lambda item: float(item[0][1:]))),
        stages=stages,
        notes=notes,
    )


# ---------------------------------------------------------------- 工程と時間

# 金型の等高荒取りでは、形状に沿って段を刻むため1段の切込みは工具径の10%程度に抑える
ROUGH_AP_PER_DIAMETER = 0.1
ROUGH_AE_PER_DIAMETER = 0.5


def _cutting_minutes(length_mm: float, feed_mm_min: float, efficiency: float) -> float:
    return length_mm / max(1.0, feed_mm_min) / max(0.1, efficiency)


def build_mold_operations(
    plan: MoldPlan,
    select_condition: Any,
    finish_passes: int = 2,
) -> list[dict[str, Any]]:
    """工具段階ごとに 荒取り／残り取り／仕上げ の工程と切削距離・時間を返す。

    select_condition(diameter, required_depth, tool_shape) は
    {"tool_name", "feed", "ap", "ae", "condition_text", "selection_reason", "candidates", "tool_id",
     "effective_length"} を返す。
    """
    operations: list[dict[str, Any]] = []
    finish_passes = max(1, int(finish_passes))
    for index, stage in enumerate(plan.stages):
        cond = select_condition(stage.diameter, stage.required_depth, stage.tool_shape)
        feed = float(cond["feed"])
        z_pitch = finish_z_pitch(stage.diameter, stage.corner_radius, stage.tool_shape)
        flat_pitch = finish_flat_pitch(stage.diameter, stage.corner_radius, stage.tool_shape)
        region_label = (
            f"全面 {stage.region_projected_area:.0f} mm2"
            if stage.whole_surface
            else f"範囲 {len(stage.region_rects)}か所 {stage.region_projected_area:.0f} mm2"
        )
        area_label = f"壁 {stage.steep_area:.0f} / 床 {stage.flat_area:.0f} mm2"
        reach_note = ""
        effective_length = cond.get("effective_length")
        if effective_length and stage.required_depth > float(effective_length) + 0.05:
            reach_note = f"金型: 必要首下長 {stage.required_depth:.1f} mm に対し有効長 {float(effective_length):g} mm"
        key_base = f"mold_d{stage.diameter:g}"
        tool_label = "ボール" if stage.tool_shape == "ball" else f"R{stage.corner_radius:g}"

        if index == 0:
            ap = min(float(cond["ap"]), ROUGH_AP_PER_DIAMETER * stage.diameter)
            ae = min(float(cond["ae"]), ROUGH_AE_PER_DIAMETER * stage.diameter)
            length = stage.rest_volume / max(0.01, ap * ae)
            levels = max(1, math.ceil((plan.bounds["zmax"] - plan.machined_floor_z) / max(0.01, ap)))
            operations.append(
                {
                    "feature_type": "金型 荒取り（等高）",
                    "dimensions": f"除去体積 {stage.rest_volume:.0f} mm3 / {region_label}",
                    "stage": stage,
                    "cond": cond,
                    "minutes": _cutting_minutes(length, feed, 0.75),
                    "length": length,
                    "passes": levels,
                    "method": "等高荒取り",
                    "extra": f"Z {levels}段 / ap {ap:.2f} x ae {ae:.2f} mm",
                    "note": f"φ{stage.diameter:g} {tool_label}で全面を等高荒取り（段の切込みは工具径の{ROUGH_AP_PER_DIAMETER:.0%}まで）",
                    "reachability": reach_note,
                    "feature_key": f"{key_base}_rough",
                }
            )
        else:
            rest_pitch = REST_PITCH_FACTOR * z_pitch
            rest_length = stage.steep_area / rest_pitch + stage.flat_area / max(rest_pitch, flat_pitch)
            ap = float(cond["ap"])
            ae = float(cond["ae"])
            rest_length += stage.rest_volume / max(0.0005, ap * ae)
            operations.append(
                {
                    "feature_type": "金型 残り取り（等高）",
                    "dimensions": f"{region_label} / {area_label} / 残り {stage.rest_volume:.0f} mm3",
                    "stage": stage,
                    "cond": cond,
                    "minutes": _cutting_minutes(rest_length, feed, 0.7),
                    "length": rest_length,
                    "passes": 1,
                    "method": "等高残り取り",
                    "extra": f"ピッチ {rest_pitch:.3f} mm",
                    "note": f"φ{stage.diameter:g} {tool_label}: {stage.reason}",
                    "reachability": reach_note,
                    "feature_key": f"{key_base}_rest",
                }
            )
        if stage.finish_role == "none":
            continue
        passes = finish_passes if stage.finish_role == "final" else 1
        finish_label = (
            f"全面 {stage.finish_projected_area:.0f} mm2"
            if stage.whole_surface
            else f"範囲 {len(stage.finish_rects)}か所 {stage.finish_projected_area:.0f} mm2"
        )
        finish_length = (stage.finish_steep_area / z_pitch + stage.finish_flat_area / flat_pitch) * passes
        operations.append(
            {
                "feature_type": "金型 仕上げ（等高線＋走査）" if stage.finish_role == "final" else "金型 中仕上げ（等高線＋走査）",
                "dimensions": (
                    f"{finish_label} / 壁 {stage.finish_steep_area:.0f} / 床 {stage.finish_flat_area:.0f} mm2 / {passes}回"
                ),
                "stage": stage,
                "cond": cond,
                "minutes": _cutting_minutes(finish_length, feed, 0.7),
                "length": finish_length,
                "passes": passes,
                "method": "等高線仕上げ",
                "extra": f"Zピッチ {z_pitch:.3f} / 床ピッチ {flat_pitch:.3f} mm x {passes}回",
                "note": (
                    f"φ{stage.diameter:g} {tool_label}: "
                    f"{stage.finish_reason or ('全面' if stage.whole_surface else '')}"
                    f"（壁は等高線、床は走査。{'ボールのスカラップ' if stage.tool_shape == 'ball' else '工具径比'}からピッチを算出）"
                ),
                "reachability": reach_note,
                "feature_key": f"{key_base}_finish",
            }
        )
    return operations
