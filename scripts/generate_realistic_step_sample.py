from pathlib import Path

import cadquery as cq


BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT = BASE_DIR / "samples" / "realistic_machining_bracket.stp"


def build_model() -> cq.Workplane:
    length = 180.0
    width = 120.0
    height = 34.0

    part = cq.Workplane("XY").box(length, width, height)
    part = part.edges("|Z").fillet(4.0)
    part = part.edges(">Z").fillet(1.2)

    # Main roughing pocket.
    part = (
        part.faces(">Z")
        .workplane()
        .rect(128.0, 76.0)
        .cutBlind(-13.0)
    )

    # Two deeper internal pockets that leave a center rib.
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(-36.0, 0.0), (36.0, 0.0)])
        .rect(38.0, 52.0)
        .cutBlind(-21.0)
    )

    # Mounting holes with counterbores.
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(-72.0, -45.0), (72.0, -45.0), (-72.0, 45.0), (72.0, 45.0)])
        .cboreHole(8.0, 15.0, 5.5)
    )

    # Drilled hole pattern in the pocket floor area.
    grid_points = []
    for x in (-48.0, -24.0, 0.0, 24.0, 48.0):
        for y in (-24.0, 24.0):
            grid_points.append((x, y))
    part = part.faces(">Z").workplane().pushPoints(grid_points).hole(5.0)

    # Two long slots.
    part = (
        part.faces(">Z")
        .workplane()
        .pushPoints([(0.0, -45.0), (0.0, 45.0)])
        .slot2D(58.0, 8.0, 0.0)
        .cutBlind(-9.0)
    )

    # Side cross holes.
    part = (
        part.faces(">Y")
        .workplane(centerOption="CenterOfBoundBox")
        .pushPoints([(-48.0, 0.0), (0.0, 0.0), (48.0, 0.0)])
        .hole(6.0)
    )

    # Local chamfering to make the model look like a production part.
    part = part.edges(">Z").chamfer(0.8)
    return part


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)
    model = build_model()
    cq.exporters.export(model, str(OUTPUT), exportType="STEP")
    print(OUTPUT)


if __name__ == "__main__":
    main()
