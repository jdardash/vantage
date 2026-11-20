"""Build static/data/sky.json, the star and constellation data behind the sky view.

The source is d3-celestial (Olaf Frohn, BSD-3-Clause), data/ at a pinned commit:
stars.6.json (Hipparcos stars to magnitude 6), starnames.json, constellations.json
and constellations.lines.json. Coordinates are J2000 right ascension and
declination in degrees; d3-celestial stores right ascension as a longitude in
-180..180, which this script turns back into 0..360.

    python scripts/build_sky_data.py --src DIR_WITH_THE_FOUR_FILES

writes src/sda/templates/static/data/sky.json. Output is deterministic (sorted,
rounded), so a rebuild from the same commit changes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

D3_CELESTIAL_REPO = "ofrohn/d3-celestial"
D3_CELESTIAL_COMMIT = "7e720a3de062059d4c5400a379146a601d9010e0"
MAX_STAR_MAG = 5.5
MAX_NAMED_MAG = 3.0
OUT = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "data" / "sky.json"
)


def _ra(lon: float) -> float:
    return round(lon % 360.0, 3)


def _num(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def build(src: Path) -> dict:
    stars_fc = json.loads((src / "stars.6.json").read_text(encoding="utf-8"))
    names = json.loads((src / "starnames.json").read_text(encoding="utf-8"))
    cons = json.loads((src / "constellations.json").read_text(encoding="utf-8"))
    lines_fc = json.loads((src / "constellations.lines.json").read_text(encoding="utf-8"))

    stars: list[list[float]] = []
    named: list[list[object]] = []
    for feat in stars_fc["features"]:
        mag = _num(feat["properties"].get("mag"))
        lon, lat = feat["geometry"]["coordinates"][:2]
        if mag is None or mag > MAX_STAR_MAG:
            continue
        bv = _num(feat["properties"].get("bv"))
        ra, dec = _ra(lon), round(float(lat), 3)
        stars.append([ra, dec, round(mag, 2), round(bv if bv is not None else 0.6, 2)])
        entry = names.get(str(feat["id"]))
        if entry and entry.get("name") and mag <= MAX_NAMED_MAG:
            named.append([ra, dec, round(mag, 2), str(entry["name"])])
    stars.sort(key=lambda s: (s[2], s[0], s[1]))
    named.sort(key=lambda s: (s[2], s[3]))

    lines: list[list[float]] = []
    for feat in lines_fc["features"]:
        for strip in feat["geometry"]["coordinates"]:
            flat: list[float] = []
            for lon, lat in strip:
                flat += [_ra(lon), round(float(lat), 3)]
            if len(flat) >= 4:
                lines.append(flat)

    labels: list[list[object]] = []
    for feat in cons["features"]:
        props = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"][:2]
        rank = int(_num(props.get("rank")) or 3)
        labels.append([_ra(lon), round(float(lat), 3), str(props["name"]), rank])
    labels.sort(key=lambda c: (c[3], c[2]))

    return {
        "source": f"https://github.com/{D3_CELESTIAL_REPO}/tree/{D3_CELESTIAL_COMMIT}/data",
        "license": "BSD-3-Clause, Copyright (c) 2015, Olaf Frohn (d3-celestial)",
        "frame": "J2000 equatorial, degrees: [ra, dec, ...]",
        "stars_fields": ["ra", "dec", "mag", "bv"],
        "stars": stars,
        "named_fields": ["ra", "dec", "mag", "name"],
        "named": named,
        "lines": lines,
        "constellations_fields": ["ra", "dec", "name", "rank"],
        "constellations": labels,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, required=True,
                    help="Folder holding the four d3-celestial data files")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    data = build(args.src)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(f"{len(data['stars'])} stars, {len(data['named'])} named, "
          f"{len(data['lines'])} line strips, {len(data['constellations'])} constellations "
          f"-> {args.out} ({len(text) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
