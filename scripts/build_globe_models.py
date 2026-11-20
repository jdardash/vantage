"""Build the procedural 3D models the Vantage globe draws when zoomed in.

The space models (ISS, Hubble, a generic satellite) and the ship classes are
our own low-poly work, written here as boxes, extrusions, cylinders and cones
and saved as binary glTF 2.0 (GLB). Every model is one mesh with one material
and per-vertex colours, so each costs the globe a single draw call, and the
whole set stays under 200 KB. The aircraft models are third-party GLBs
copied verbatim (static/models/NOTICE.txt); this script does not touch them.

Frame: parts are written in Cesium's model frame (X forward, Y left, Z up,
metres) and stored in glTF's Y-up, Z-forward frame, which Cesium rotates back
to X forward when it loads the file. A ship's waterline is z = 0.

Run: python scripts/build_globe_models.py            (writes the GLBs)
     python scripts/build_globe_models.py --check    (exit 1 if any differ)

The output is byte-for-byte deterministic, so tests/test_globe_models.py can
rebuild the set and compare it with the committed files.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "src" / "sda" / "templates" / "static" / "models"

Vec = tuple[float, float, float]
Face = tuple[list[Vec], str]  # polygon (convex, any winding) and its colour


# ---------------------------------------------------------------------------
# Geometry helpers. Every part is a convex solid, which lets the writer orient
# each face outward from the part's centroid without trusting the winding.
# ---------------------------------------------------------------------------


class Part:
    def __init__(self, faces: list[Face]):
        self.faces = faces

    def centroid(self) -> Vec:
        pts = [p for poly, _ in self.faces for p in poly]
        n = float(len(pts))
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n)

    def transformed(self, fn) -> Part:
        return Part([([fn(p) for p in poly], color) for poly, color in self.faces])


def box(cx: float, cy: float, cz: float, sx: float, sy: float, sz: float, color: str) -> Part:
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz - sz / 2, cz + sz / 2
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return Part([([c[i] for i in q], color) for q in quads])


def extrude(poly: list[tuple[float, float]], z0: float, z1: float, color: str,
            bottom_scale: float = 1.0, top_color: str | None = None) -> Part:
    """A convex XY outline from z0 to z1; the bottom ring shrinks about its centroid."""
    mx = sum(p[0] for p in poly) / len(poly)
    my = sum(p[1] for p in poly) / len(poly)
    bottom = [(mx + (x - mx) * bottom_scale, my + (y - my) * bottom_scale, z0) for x, y in poly]
    top = [(x, y, z1) for x, y in poly]
    faces: list[Face] = [(top, top_color or color), (bottom, color)]
    n = len(poly)
    for i in range(n):
        j = (i + 1) % n
        faces.append(([bottom[i], bottom[j], top[j], top[i]], color))
    return Part(faces)


def frustum(axis: str, center: Vec, r0: float, r1: float, length: float, color: str,
            segments: int = 12, cap_color: str | None = None) -> Part:
    """Cylinder (r0 == r1) or cone frustum along x, y or z, r0 at the low end."""
    ring0: list[Vec] = []
    ring1: list[Vec] = []
    for k in range(segments):
        a = 2.0 * math.pi * k / segments
        u, v = math.cos(a), math.sin(a)
        lo = (-length / 2, r0 * u, r0 * v)
        hi = (length / 2, r1 * u, r1 * v)
        ring0.append(lo)
        ring1.append(hi)

    def place(p: Vec) -> Vec:
        a, b, c = p
        if axis == "x":
            q = (a, b, c)
        elif axis == "y":
            q = (c, a, b)
        else:
            q = (b, c, a)
        return (q[0] + center[0], q[1] + center[1], q[2] + center[2])

    faces: list[Face] = []
    for k in range(segments):
        m = (k + 1) % segments
        faces.append(([place(ring0[k]), place(ring0[m]), place(ring1[m]), place(ring1[k])], color))
    if r0 > 0.001:
        faces.append(([place(p) for p in ring0], cap_color or color))
    if r1 > 0.001:
        faces.append(([place(p) for p in ring1], cap_color or color))
    else:
        # Cone tip: the side quads collapse to triangles, which the writer accepts.
        pass
    return Part(faces)


def rotate_x(part: Part, deg: float, pivot: Vec = (0.0, 0.0, 0.0)) -> Part:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)

    def fn(p: Vec) -> Vec:
        x, y, z = p[0] - pivot[0], p[1] - pivot[1], p[2] - pivot[2]
        return (x + pivot[0], y * ca - z * sa + pivot[1], y * sa + z * ca + pivot[2])

    return part.transformed(fn)


def rotate_y(part: Part, deg: float, pivot: Vec = (0.0, 0.0, 0.0)) -> Part:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)

    def fn(p: Vec) -> Vec:
        x, y, z = p[0] - pivot[0], p[1] - pivot[1], p[2] - pivot[2]
        return (x * ca + z * sa + pivot[0], y + pivot[1], -x * sa + z * ca + pivot[2])

    return part.transformed(fn)


def hull_outline(length: float, beam: float, bow: float, stern_round: float = 0.0
                 ) -> list[tuple[float, float]]:
    """Deck outline of a monohull: parallel sides, a pointed bow of length `bow`."""
    h = length / 2
    b = beam / 2
    pts = [(-h, -b * (1 - stern_round)), (-h + stern_round * b, -b), (h - bow, -b),
           (h - bow * 0.35, -b * 0.62), (h, 0.0), (h - bow * 0.35, b * 0.62), (h - bow, b),
           (-h + stern_round * b, b), (-h, b * (1 - stern_round))]
    out: list[tuple[float, float]] = []
    for p in pts:
        if not out or (abs(out[-1][0] - p[0]) > 1e-6 or abs(out[-1][1] - p[1]) > 1e-6):
            out.append(p)
    return out


def triangle_sail(x0: float, x1: float, z0: float, z1: float, y: float, color: str,
                  thick: float = 0.08) -> Part:
    """A right-triangle sail standing in the XZ plane: luff at x1 from z0 to z1."""
    tri = [(x0, z0), (x1, z0), (x1, z1)]
    a = [(x, y - thick / 2, z) for x, z in tri]
    b = [(x, y + thick / 2, z) for x, z in tri]
    faces: list[Face] = [(a, color), (b, color)]
    for i in range(3):
        j = (i + 1) % 3
        faces.append(([a[i], a[j], b[j], b[i]], color))
    return Part(faces)


# ---------------------------------------------------------------------------
# Models. Colours are sRGB hex; the writer converts them to linear.
# ---------------------------------------------------------------------------

SOLAR = "#27324d"
SOLAR_EDGE = "#3c4f78"
GOLD = "#c9a227"
WHITE = "#eeeeec"
SILVER = "#c4c7cc"
TRUSS = "#a9adb3"


def model_iss() -> list[Part]:
    parts: list[Part] = []
    # Integrated truss along Y, 109 m, sitting on the zenith side of the lab.
    parts.append(box(0, 0, 3.2, 4.5, 104, 4.5, TRUSS))
    # US segment modules along the velocity vector, Russian segment aft.
    parts.append(frustum("x", (7, 0, -0.5), 2.2, 2.2, 28, WHITE, 12))
    parts.append(frustum("x", (-17, 0, -0.5), 2.1, 2.1, 22, "#dcdcd6", 12))
    parts.append(frustum("x", (-31, 0, -0.5), 1.6, 1.6, 8, "#d4d4cc", 10))
    parts.append(frustum("y", (19, 7.5, -0.5), 2.2, 2.2, 11, WHITE, 12))   # Kibo
    parts.append(frustum("y", (19, -5.5, -0.5), 2.2, 2.2, 7, WHITE, 12))   # Columbus
    parts.append(box(19, 15.5, -0.5, 4.5, 5, 1.2, "#b9b9b0"))              # exposed facility
    parts.append(frustum("z", (2, 0, -5.5), 2.0, 2.0, 7, WHITE, 10))       # nadir node
    # Russian segment arrays.
    for side in (1, -1):
        parts.append(box(-22, side * 9.5, -0.5, 3.2, 12, 0.12, SOLAR))
    # Main solar array wings: four pairs at the truss ends, each wing 35 x 12 m.
    for y in (34.0, 46.0, -34.0, -46.0):
        parts.append(box(0, y, 3.2, 3.0, 2.0, 2.0, GOLD))  # rotary joint and mast canister
        for fwd in (1, -1):
            parts.append(box(fwd * 20.0, y, 3.2, 35, 11.0, 0.14, SOLAR))
            parts.append(box(fwd * 20.0, y, 3.25, 35, 0.5, 0.2, SOLAR_EDGE))
    # Heat rejection radiators hang below the truss inboard of the arrays.
    for side in (1, -1):
        for k in range(3):
            parts.append(box(-5.5 + k * 3.4, side * 22, -6.5, 3.0, 0.2, 13.5, "#f4f4f2"))
    # Station radiators on the S0/P1 truss.
    for side in (1, -1):
        parts.append(box(0, side * 12, -3.5, 10, 0.2, 7, "#f0f0ee"))
    return parts


def model_hubble() -> list[Part]:
    parts: list[Part] = []
    parts.append(frustum("x", (-3.0, 0, 0), 2.1, 2.1, 6.0, SILVER, 16))       # aft shroud
    parts.append(frustum("x", (2.6, 0, 0), 1.6, 1.6, 5.4, "#d9dbde", 16))     # light shield
    parts.append(frustum("x", (0.0, 0, 0), 2.1, 1.6, 0.8, "#9ea2a8", 16))     # step ring
    parts.append(box(5.45, 0, 1.55, 0.1, 3.2, 3.2, "#e2e2e2"))                # aperture door
    parts.append(box(-6.2, 0, 0, 0.4, 3.6, 3.6, "#808388"))                  # aft bulkhead
    for side in (1, -1):
        parts.append(box(-1.2, side * 2.6, 0, 0.2, 1.4, 0.2, "#777"))         # boom root
        parts.append(box(-1.2, side * 6.4, 0, 2.5, 7.1, 0.06, "#38406a"))    # array blanket
        parts.append(box(-1.2, side * 6.4, 0.04, 0.12, 7.1, 0.1, GOLD))       # spreader bar
    for side in (1, -1):
        parts.append(box(0.8, 0, side * 2.6, 0.15, 0.15, 1.6, "#777"))        # antenna booms
        parts.append(frustum("z", (0.8, 0, side * 3.5), 0.05, 0.6, 0.35, "#e8e8e8", 12))
    return parts


def model_satellite() -> list[Part]:
    parts: list[Part] = [box(0, 0, 0, 2.0, 2.0, 2.0, GOLD)]
    parts.append(box(0, 0, 1.05, 1.6, 1.6, 0.1, "#d9d9d9"))
    for side in (1, -1):
        parts.append(box(0, side * 1.6, 0, 0.1, 1.2, 0.1, "#888"))
        parts.append(box(0, side * 4.6, 0, 1.6, 5.0, 0.05, SOLAR))
    parts.append(frustum("z", (0.4, 0, -1.3), 0.08, 0.75, 0.5, "#f0f0f0", 12))
    return parts


def hull(length: float, beam: float, bow: float, lower: str, upper: str,
         freeboard: float, deck: str | None = None, stern_round: float = 0.0) -> list[Part]:
    outline = hull_outline(length, beam, bow, stern_round)
    return [
        extrude(outline, -2.0, 1.0, lower, bottom_scale=0.86),
        extrude(outline, 1.0, freeboard, upper, top_color=deck or upper),
    ]


def model_cargo() -> list[Part]:
    L, B = 190.0, 32.0
    parts = hull(L, B, 26, "#7a2a22", "#23272d", 8.0, "#5b4a3a")
    for k in range(5):
        parts.append(box(55 - k * 27, 0, 9.0, 18, 22, 2.0, "#2f5d3a"))  # hatch covers
    for k in range(4):
        parts.append(box(41.5 - k * 27, 8, 16, 1.2, 1.2, 16, "#d4a017"))  # crane posts
        parts.append(rotate_y(box(41.5 - k * 27 + 9, 8, 21, 20, 0.8, 0.8, "#d4a017"),
                              -20, (41.5 - k * 27, 8, 21)))
    parts.append(box(-80, 0, 18, 16, 26, 20, WHITE))       # accommodation block
    parts.append(box(-78, 0, 29, 10, 32, 2.5, WHITE))     # bridge wings
    parts.append(box(-89, 0, 31, 6, 6, 10, "#b03a2e"))     # funnel
    return parts


def model_container() -> list[Part]:
    L, B = 300.0, 46.0
    parts = hull(L, B, 34, "#8b2f24", "#1d3557", 10.0)
    colors = ["#c0532a", "#2e6da4", "#a93226", "#7f8c8d", "#2e8b57", "#d4ac0d", "#e9e9e9",
              "#6c3483", "#b9770e"]
    x = 115.0
    k = 0
    while x > -100:
        if -62 < x < -38:
            x -= 13.5
            continue
        stack = 6 + (k * 7) % 4
        color = colors[k % len(colors)]
        parts.append(box(x, 0, 10 + stack * 1.3, 12.5, B - 4, stack * 2.6, color))
        x -= 13.5
        k += 1
    parts.append(box(-50, 0, 25, 12, 34, 30, WHITE))       # bridge tower
    parts.append(box(-50, 0, 41, 8, B, 2.5, WHITE))
    parts.append(box(-128, 0, 22, 8, 10, 16, "#222831"))   # funnel casing
    return parts


def model_tanker() -> list[Part]:
    L, B = 250.0, 44.0
    parts = hull(L, B, 30, "#8b2f24", "#1b1e22", 9.0, "#7b3b2a")
    parts.append(box(10, 0, 10.0, 170, 1.6, 1.6, "#c6c6c6"))   # cargo pipeline
    parts.append(box(10, 0, 10.6, 6, 30, 1.2, "#d0d0d0"))      # manifold
    for k in range(6):
        parts.append(box(85 - k * 30, 0, 9.6, 1.0, 36, 0.8, "#c6c6c6"))
    parts.append(box(-105, 0, 19, 18, 30, 20, WHITE))
    parts.append(box(-103, 0, 30, 10, 38, 2.5, WHITE))
    parts.append(box(-116, 0, 32, 6, 7, 12, "#1b1e22"))
    return parts


def model_passenger() -> list[Part]:
    L, B = 290.0, 36.0
    parts = hull(L, B, 40, "#1b2a49", WHITE, 12.0, stern_round=0.3)
    deck = hull_outline(L - 40, B - 2, 30, 0.3)
    for level in range(5):
        z0 = 12 + level * 6.0
        shrink = 1.0 - level * 0.05
        poly = [(x * shrink - 8, y) for x, y in deck]
        parts.append(extrude(poly, z0, z0 + 5.2, WHITE))
        # Window band, a little proud of the block so the two never z-fight.
        band = [(x * shrink * 1.004 - 8, y * 1.025) for x, y in deck]
        parts.append(extrude(band, z0 + 1.4, z0 + 3.0, "#1f3b5a"))
    parts.append(box(-70, 0, 47, 14, 9, 12, "#c0392b"))      # funnel
    parts.append(box(-70, 0, 54, 14, 9, 2.5, "#111111"))
    parts.append(box(70, 0, 44, 6, 26, 2, WHITE))            # bridge roof
    return parts


def model_fishing() -> list[Part]:
    L, B = 34.0, 9.0
    parts = hull(L, B, 6, "#9b2226", "#1f4e79", 3.5)
    parts.append(box(7, 0, 6.0, 7, 6.5, 5.0, WHITE))
    parts.append(box(7, 0, 8.8, 7.5, 7, 0.4, "#1f4e79"))
    parts.append(box(9, 0, 14, 0.4, 0.4, 12, "#cccccc"))     # mast
    parts.append(rotate_y(box(-1, 0, 13, 16, 0.3, 0.3, "#cccccc"), 25, (9, 0, 13)))  # boom
    parts.append(box(-14, 0, 8, 0.6, 8, 0.6, "#e67e22"))     # stern gantry
    for side in (1, -1):
        parts.append(box(-14, side * 3.8, 5.8, 0.6, 0.6, 4.6, "#e67e22"))
    return parts


def model_tug() -> list[Part]:
    L, B = 32.0, 12.0
    parts = hull(L, B, 7, "#c0392b", "#1b1e22", 3.6, stern_round=0.4)
    parts.append(extrude(hull_outline(L + 0.8, B + 0.8, 7.5, 0.4), 2.4, 3.2, "#2b2b2b"))  # fender
    parts.append(box(3, 0, 6.5, 9, 8, 6, WHITE))
    parts.append(box(4, 0, 11, 6, 7, 3.0, WHITE))
    parts.append(box(4, 0, 12.7, 6.5, 7.5, 0.4, "#1b1e22"))
    parts.append(box(-3, 0, 9, 2.2, 3.5, 6, "#f1c40f"))      # funnel
    parts.append(box(-10, 0, 5, 2, 2, 2.6, "#7f8c8d"))      # towing winch
    return parts


def model_military() -> list[Part]:
    L, B = 150.0, 19.0
    grey = "#6b7378"
    parts = hull(L, B, 34, "#3d4347", grey, 7.0, "#555c61")
    parts.append(extrude(hull_outline(52, 15, 6), 7.0, 16.0, "#73797e"))           # superstructure
    parts.append(box(14, 0, 20, 12, 11, 8, "#7a8085"))                              # bridge
    parts.append(box(10, 0, 30, 1.6, 1.6, 20, "#5f666b"))                           # mast
    parts.append(box(10, 0, 34, 1.0, 7, 0.6, "#5f666b"))
    parts.append(frustum("z", (48, 0, 8.5), 2.4, 2.0, 3.0, grey, 12))             # gun mount
    parts.append(frustum("x", (54, 0, 9.0), 0.25, 0.2, 9.0, "#4b5155", 8))        # barrel
    parts.append(box(-24, 0, 12, 10, 5, 8, "#52585c"))                              # funnel
    parts.append(box(-60, 0, 7.15, 26, 15, 0.3, "#4a5054"))                         # flight deck
    return parts


def model_sailing() -> list[Part]:
    L, B = 14.0, 4.4
    parts = hull(L, B, 4, "#1b2a49", WHITE, 1.3, "#d8c9a3", stern_round=0.4)
    parts.append(box(-1.0, 0, 2.2, 4, 2.6, 1.4, WHITE))     # coachroof
    parts.append(box(1.0, 0, 10.5, 0.25, 0.25, 18.5, "#c8c8c8"))  # mast
    parts.append(triangle_sail(-5.0, 0.8, 2.6, 18.5, 0.0, "#f7f7f2"))   # main
    parts.append(triangle_sail(6.4, 1.3, 1.6, 17.0, 0.0, "#f2f2ec"))    # jib
    return parts


def model_pleasure() -> list[Part]:
    L, B = 22.0, 5.6
    parts = hull(L, B, 5.5, "#1b2a49", WHITE, 2.0, stern_round=0.4)
    parts.append(extrude(hull_outline(12, 4.6, 3.0, 0.4), 2.0, 4.2, WHITE))
    parts.append(extrude(hull_outline(12.2, 4.7, 3.0, 0.4), 2.8, 3.6, "#1c2833"))  # glazing
    parts.append(extrude(hull_outline(7, 4.0, 2.0, 0.4), 4.2, 5.6, WHITE))
    parts.append(box(-1.0, 0, 6.4, 0.3, 0.3, 1.6, "#c8c8c8"))
    return parts


MODELS = {
    "iss": (model_iss, 0.35, 0.45),
    "hubble": (model_hubble, 0.4, 0.4),
    "satellite": (model_satellite, 0.35, 0.45),
    "ship_cargo": (model_cargo, 0.0, 0.8),
    "ship_container": (model_container, 0.0, 0.8),
    "ship_tanker": (model_tanker, 0.0, 0.8),
    "ship_passenger": (model_passenger, 0.0, 0.75),
    "ship_fishing": (model_fishing, 0.0, 0.8),
    "ship_tug": (model_tug, 0.0, 0.8),
    "ship_military": (model_military, 0.1, 0.65),
    "ship_sailing": (model_sailing, 0.0, 0.8),
    "ship_pleasure": (model_pleasure, 0.0, 0.7),
}


# ---------------------------------------------------------------------------
# GLB writer
# ---------------------------------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _color_bytes(hexcolor: str) -> tuple[int, int, int, int]:
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [max(0, min(255, round(_srgb_to_linear(v) * 255))) for v in rgb]
    return lin[0], lin[1], lin[2], 255


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 1.0)


def _to_gltf(p: Vec) -> Vec:
    # Cesium model frame (x fwd, y left, z up) -> glTF (y up, z fwd): (y, z, x).
    return (p[1], p[2], p[0])


def _r(v: float) -> float:
    # Rounded so the output does not depend on float noise across platforms.
    return float(struct.unpack("<f", struct.pack("<f", round(v, 4)))[0])


def mesh_arrays(parts: list[Part]):
    positions: list[Vec] = []
    normals: list[Vec] = []
    colors: list[tuple[int, int, int, int]] = []
    indices: list[int] = []
    for part in parts:
        center = part.centroid()
        for poly, color in part.faces:
            pts = []
            for p in poly:
                if not pts or max(abs(p[i] - pts[-1][i]) for i in range(3)) > 1e-9:
                    pts.append(p)
            if len(pts) > 1 and max(abs(pts[0][i] - pts[-1][i]) for i in range(3)) <= 1e-9:
                pts.pop()
            if len(pts) < 3:
                continue
            # Newell normal: robust for any planar convex polygon.
            nx = ny = nz = 0.0
            for i, a in enumerate(pts):
                b = pts[(i + 1) % len(pts)]
                nx += (a[1] - b[1]) * (a[2] + b[2])
                ny += (a[2] - b[2]) * (a[0] + b[0])
                nz += (a[0] - b[0]) * (a[1] + b[1])
            n = _norm((nx, ny, nz))
            fc = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts),
                  sum(p[2] for p in pts) / len(pts))
            if _dot(n, _sub(fc, center)) < 0:
                pts.reverse()
                n = (-n[0], -n[1], -n[2])
            base = len(positions)
            rgba = _color_bytes(color)
            for p in pts:
                positions.append(_to_gltf(p))
                normals.append(_to_gltf(n))
                colors.append(rgba)
            for i in range(1, len(pts) - 1):
                indices.extend((base, base + i, base + i + 1))
    return positions, normals, colors, indices


def build_glb(name: str, parts: list[Part], metallic: float, roughness: float) -> bytes:
    positions, normals, colors, indices = mesh_arrays(parts)
    if len(positions) >= 65535:
        raise ValueError(f"{name}: {len(positions)} vertices do not fit 16-bit indices")
    pos_bytes = b"".join(struct.pack("<3f", *(_r(c) for c in p)) for p in positions)
    nor_bytes = b"".join(struct.pack("<3f", *(_r(c) for c in n)) for n in normals)
    col_bytes = b"".join(struct.pack("<4B", *c) for c in colors)
    idx_bytes = struct.pack(f"<{len(indices)}H", *indices)
    if len(idx_bytes) % 4:
        idx_bytes += b"\x00" * (4 - len(idx_bytes) % 4)
    chunks = [pos_bytes, nor_bytes, col_bytes, idx_bytes]
    offsets = []
    off = 0
    for c in chunks:
        offsets.append(off)
        off += len(c)
    binary = b"".join(chunks)
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    doc = {
        "asset": {"version": "2.0", "generator": "sda scripts/build_globe_models.py",
                  "copyright": "space-collision-predictor contributors; procedural, own work"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "COLOR_0": 2},
            "indices": 3, "material": 0, "mode": 4}]}],
        "materials": [{"name": name, "doubleSided": True, "pbrMetallicRoughness": {
            "baseColorFactor": [1, 1, 1, 1], "metallicFactor": metallic,
            "roughnessFactor": roughness}}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": offsets[0], "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": offsets[1], "byteLength": len(nor_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": offsets[2], "byteLength": len(col_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": offsets[3], "byteLength": len(indices) * 2,
             "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
             "min": [_r(min(xs)), _r(min(ys)), _r(min(zs))],
             "max": [_r(max(xs)), _r(max(ys)), _r(max(zs))]},
            {"bufferView": 1, "componentType": 5126, "count": len(normals), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5121, "normalized": True, "count": len(colors),
             "type": "VEC4"},
            {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
    }
    js = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(js) % 4:
        js += b" " * (4 - len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    out = struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(js), b"JSON") + js
    out += struct.pack("<I4s", len(binary), b"BIN\x00") + binary
    return out


def build_all() -> dict[str, bytes]:
    return {f"{name}.glb": build_glb(name, fn(), metal, rough)
            for name, (fn, metal, rough) in MODELS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="compare with the committed files instead of writing them")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    built = build_all()
    if args.check:
        stale = [n for n, data in built.items()
                 if not (args.out / n).exists() or (args.out / n).read_bytes() != data]
        for n in stale:
            print(f"stale: {n}")
        return 1 if stale else 0
    args.out.mkdir(parents=True, exist_ok=True)
    total = 0
    for n, data in sorted(built.items()):
        (args.out / n).write_bytes(data)
        total += len(data)
        print(f"{n:22s} {len(data):7d} bytes")
    print(f"{'total':22s} {total:7d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
