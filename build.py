# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools>=4.53"]
# ///
"""Build a static font family, monospaced unless the config says otherwise, from variable or static fonts.

For every style in the config:
  1. pin every variation axis (fontTools instancer), if the style's source is variable;
  2. optionally taper the free ends of the source's stems (`taper`);
  3. optionally slant the source's glyphs into an oblique (`oblique`), in the styles it lists;
  4. optionally merge another font's glyphs (e.g. Thai, or cursive letters for the Italic)
     fitted into the source's cells, in the styles the merge lists;
  5. bake `rvrn` plus the `freeze` features into the character map, so the chosen
     glyphs appear even in apps that never apply OpenType features;
  6. copy the `always_on` features into `calt`, so they are on by default wherever
     contextual alternates are on (most editors and terminals);
  7. rename the family and set the style-linking flags, plus the monospace flags unless
     the config sets `monospace = false`.

Usage: uv run tools/fontbuild/build.py [CONFIG] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import copy
import io
import math
import sys
import tomllib
from pathlib import Path

from fontTools import subset
from fontTools.merge import Merger
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables import otTables, ttProgram
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
from fontTools.varLib.instancer import instantiateVariableFont

STYLES = ("Regular", "Italic", "Bold", "Bold Italic")
DEFAULT_CONFIG = Path(__file__).with_name("rec-mono-fancy.toml")

# OS/2.fsSelection bits
FS_ITALIC = 1 << 0
FS_BOLD = 1 << 5
FS_REGULAR = 1 << 6
PANOSE_MONOSPACED = 9
GDEF_MARK = 3
# Merged layout features that keep one character per cell: no kerning, no ligatures.
MERGE_FEATURES = ("ccmp", "locl", "abvm", "blwm", "mark", "mkmk", "dist")
# Vertical metrics that set line height and the clipping box.
VERTICAL_METRICS = {
    "hhea": ("ascent", "descent", "lineGap"),
    "OS/2": ("sTypoAscender", "sTypoDescender", "sTypoLineGap", "usWinAscent", "usWinDescent"),
}
# Stem ends for `taper`: a horizontal edge at most `max_end` units wide (TAPER_MAX_END by default)
# between two sides at least TAPER_MIN_SIDE long that lean less than TAPER_MAX_SLANT degrees from
# vertical. Each side gives at most TAPER_MAX_REACH of its length to the taper, so the tapers at
# both ends of a stem never meet.
TAPER_MIN_SIDE = 150
TAPER_MAX_END = 140
TAPER_MAX_SLANT = 35
TAPER_MAX_REACH = 0.45


class BuildError(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out-dir", type=Path, help="default: dist/<FamilyNameWithoutSpaces>")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        out_dir = args.out_dir or Path("dist") / ps_family(config["family"])
        out_dir.mkdir(parents=True, exist_ok=True)
        for style, axes in config["styles"].items():
            path = out_dir / f"{ps_family(config['family'])}-{style.replace(' ', '')}.ttf"
            build_style(config, style, axes, path)
            print(f"wrote {path}")
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        config = tomllib.load(f)
    source = config["source"]
    if isinstance(source, str):
        source = dict.fromkeys(config["styles"], source)
    elif set(source) != set(config["styles"]):
        raise BuildError(f"`source` has fonts for {sorted(source)}; give one for each style in `styles`")
    config["source"] = {style: resolve_font_path(value, path) for style, value in source.items()}
    config.setdefault("monospace", True)
    if "merge" in config:
        merge = config["merge"]
        if not config["monospace"]:
            raise BuildError("`merge` fits glyphs into the source's cells, so it needs `monospace = true`")
        merge["source"] = resolve_font_path(merge["source"], path)
        if not merge.get("styles"):
            raise BuildError("`merge.styles` must list the styles that get the merge, with the merged font's axes")
        unknown = set(merge["styles"]) - set(config["styles"])
        if unknown:
            raise BuildError(f"`merge.styles` has {sorted(unknown)}, which `styles` does not build")
    if "oblique" in config:
        unknown = set(config["oblique"]["styles"]) - set(config["styles"])
        if unknown:
            raise BuildError(f"`oblique.styles` has {sorted(unknown)}, which `styles` does not build")
    config.setdefault("freeze", [])
    config.setdefault("always_on", [])
    unknown = set(config["styles"]) - set(STYLES)
    if unknown:
        raise BuildError(f"unsupported styles {sorted(unknown)}; use any of {list(STYLES)}")
    return config


def resolve_font_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    if not path.exists():
        raise BuildError(f"font not found: {path} (see the download note next to it in {config_path})")
    return path


def ps_family(family: str) -> str:
    return family.replace(" ", "")


def build_style(config: dict, style: str, axes: dict[str, float], path: Path) -> None:
    font = TTFont(config["source"][style])
    pin_axes(font, axes, style)
    # STAT names the removed axes; DSIG no longer matches the bytes; PfEd is FontForge data fontTools cannot merge.
    for table in ("STAT", "DSIG", "PfEd"):
        if table in font:
            del font[table]
    if "taper" in config:
        taper_stems(font, config["taper"])
    slant = axes.get("slnt")
    if "oblique" in config and style in config["oblique"]["styles"]:
        slant_glyphs(font, config["oblique"])
        slant = config["oblique"]["angle"]
    if "merge" in config and style in config["merge"]["styles"]:
        font = merge_script(font, config["merge"], style)

    if "GSUB" in font:
        gsub = font["GSUB"].table
        # rvrn runs before every other feature, so it is frozen first, as its own stage.
        has_rvrn = any(rec.FeatureTag == "rvrn" for rec in gsub.FeatureList.FeatureRecord)
        freeze_into_cmap(font, ([["rvrn"]] if has_rvrn else []) + [config["freeze"]])
        for tag in config["always_on"]:
            enable_by_default(gsub, tag)
    elif config["freeze"] or config["always_on"]:
        raise BuildError("the source font has no GSUB table, so `freeze` and `always_on` must be empty")

    if "ascent" in config or "descent" in config:
        set_line_metrics(font, config.get("ascent"), config.get("descent"))
    if config["monospace"]:
        cell, dropped = enforce_monospace(font)
        if dropped:
            print(f"{style}: unmapped off-width characters {' '.join(f'U+{cp:04X}' for cp in dropped)}")
        set_monospace_flags(font, cell)
    set_names(font, config["family"], style)
    set_style_flags(font, style, slant)
    font.save(path)


def set_line_metrics(font: TTFont, ascent: int | None, descent: int | None) -> None:
    """Set the line box in all three metric sets: hhea (macOS), typo, and win (Windows clips to it)."""
    hhea, os2 = font["hhea"], font["OS/2"]
    if ascent is not None:
        hhea.ascent = os2.sTypoAscender = os2.usWinAscent = ascent
    if descent is not None:
        hhea.descent = os2.sTypoDescender = -descent
        os2.usWinDescent = descent


def pin_axes(font: TTFont, axes: dict[str, float], what: str) -> None:
    axis_tags = {a.axisTag for a in font["fvar"].axes} if "fvar" in font else set()
    if set(axes) != axis_tags:
        raise BuildError(
            f"{what}: pin every axis exactly once; font has {sorted(axis_tags)}, config has {sorted(axes)}"
        )
    if axis_tags:
        instantiateVariableFont(font, axes, inplace=True)


def taper_stems(font: TTFont, config: dict) -> None:
    """Narrow the free ends of upright and slanted stems, the way Syne Mono's strokes thin out.

    Each stem end (see TAPER_MAX_SLANT) loses `amount` of its width, and the stem's sides curve in
    to meet it over their last `length` units. Slabs, bowls, dots and curved terminals stay as
    they are. Tapered glyphs lose their hinting instructions, which no longer fit the outline.
    With `substitutes`, the glyphs GSUB can put in place of the characters' glyphs (ligatures,
    contextual and stylistic alternates) are tapered too.
    """
    glyf, hmtx = font["glyf"], font["hmtx"]
    cmap = font.getBestCmap()
    names = {cmap[cp] for cp in parse_unicodes(config["unicodes"]) if cp in cmap}
    if config.get("substitutes", False) and "GSUB" in font:
        names = substituted_glyphs(font["GSUB"].table, names)
    pending = list(names)
    simple = set()
    while pending:  # composites draw their components, so tapering the components covers them
        name = pending.pop()
        glyph = glyf[name]
        if glyph.isComposite():
            pending.extend(c.glyphName for c in glyph.components)
        elif glyph.numberOfContours > 0:
            simple.add(name)
    for name in sorted(simple):
        glyph = glyf[name]
        if taper_glyph(glyph, glyf, config["amount"], config["length"], config.get("max_end", TAPER_MAX_END)):
            glyph.program = ttProgram.Program()
            glyph.program.fromBytecode(b"")
            glyph.recalcBounds(glyf)
            hmtx[name] = (hmtx[name][0], glyph.xMin)


def substituted_glyphs(gsub, names: set[str]) -> set[str]:
    """`names` plus every glyph a GSUB lookup can substitute for them, followed through chains.

    Context is ignored: a glyph counts if any lookup can produce it from these glyphs. A ligature
    counts only when all its components are among them.
    """
    subtables = []
    for lookup in gsub.LookupList.Lookup:
        for subtable in lookup.SubTable:
            kind = lookup.LookupType
            if kind == 7:
                kind, subtable = subtable.ExtensionLookupType, subtable.ExtSubTable
            subtables.append((kind, subtable))  # types 5 and 6 only point at other lookups
    reached = set(names)
    while True:
        found = set()
        for kind, subtable in subtables:
            if kind == 1:
                found.update(out for g, out in subtable.mapping.items() if g in reached)
            elif kind in (2, 3):
                outputs = subtable.mapping if kind == 2 else subtable.alternates
                for g, out in outputs.items():
                    if g in reached:
                        found.update(out)
            elif kind == 4:
                for g, ligatures in subtable.ligatures.items():
                    if g in reached:
                        found.update(lig.LigGlyph for lig in ligatures if reached.issuperset(lig.Component))
            elif kind == 8:
                found.update(out for g, out in zip(subtable.Coverage.glyphs, subtable.Substitute) if g in reached)
        if found <= reached:
            return reached
        reached |= found


def taper_glyph(glyph, glyf, amount: float, length: float, max_end: float) -> bool:
    coords, ends, flags = glyph.getCoordinates(glyf)
    contours, start = [], 0
    for end in ends:
        contours.append((list(coords[start : end + 1]), flags[start : end + 1]))
        start = end + 1
    # TrueType draws outer contours clockwise, with the ink right of the direction of travel.
    ink = 1 if sum(signed_area(points) for points, _ in contours) < 0 else -1

    new_coords, new_flags, new_ends, changed = [], bytearray(), [], False
    for points, on_curve in contours:
        n = len(points)
        ends_at = {i for i in range(n) if is_stem_end(points, on_curve, i, ink, max_end)}
        for j, point in enumerate(points):
            if j in ends_at:  # first corner of a stem end: side A->B, then the end B->C
                side, corner, other = points[j - 1], point, points[(j + 1) % n]
            elif (j - 1) % n in ends_at:  # second corner: the end B->C, then side C->D
                side, corner, other = points[(j + 1) % n], point, points[j - 1]
            else:
                new_coords.append(point)
                new_flags.append(on_curve[j])
                continue
            changed = True
            reach = min(length, TAPER_MAX_REACH * math.dist(side, corner))
            start_at = lerp(corner, side, reach / math.dist(side, corner))  # where the side starts to bend in
            bend = lerp(corner, start_at, 0.5)  # off-curve point on the side keeps the bend tangent there
            tip = lerp(corner, other, amount / 2)
            curve = [start_at, bend, tip] if j in ends_at else [tip, bend, start_at]
            new_coords.extend((round(x), round(y)) for x, y in curve)
            new_flags.extend((on_curve[j], on_curve[j] & ~1, on_curve[j]))
        new_ends.append(len(new_coords) - 1)
    if changed:
        glyph.coordinates = GlyphCoordinates(new_coords)
        glyph.flags = new_flags
        glyph.endPtsOfContours = new_ends
    return changed


def is_stem_end(points: list, on_curve, i: int, ink: int, max_end: float) -> bool:
    """Is edge i -> i+1 the end of a stem: short, horizontal, between two long near-upright sides?"""
    n = len(points)
    a, b, c, d = (points[(i + k) % n] for k in (-1, 0, 1, 2))
    if n < 4 or not all(on_curve[(i + k) % n] & 1 for k in (-1, 0, 1, 2)):
        return False
    if abs(c[1] - b[1]) > 1 or not 0 < abs(c[0] - b[0]) <= max_end:
        return False
    for p, q in ((a, b), (d, c)):
        if math.dist(p, q) < TAPER_MIN_SIDE:
            return False
        if math.degrees(math.atan2(abs(p[0] - q[0]), abs(p[1] - q[1]))) > TAPER_MAX_SLANT:
            return False
    # Both sides leave on the inked side of the end, so this is a stem's end, not a counter's floor.
    ink_up = ink * (b[0] - c[0])  # > 0 when the ink lies above the edge
    return ink_up * (a[1] - b[1]) > 0 and ink_up * (d[1] - c[1]) > 0


def signed_area(points: list) -> float:
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1])) / 2


def lerp(p: tuple[float, float], q: tuple[float, float], t: float) -> tuple[float, float]:
    return p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t


def slant_glyphs(font: TTFont, config: dict) -> None:
    """Shear the glyphs for `unicodes` by `angle` degrees: an oblique made from upright glyphs.

    The shear pivots on half the x-height, as type designers' slant tools do, so lowercase stays
    centred in its cell. Advance widths stay; composites are decomposed; slanted glyphs lose
    their hinting instructions, which no longer fit the outline.
    """
    glyf, hmtx = font["glyf"], font["hmtx"]
    cmap = font.getBestCmap()
    names = sorted({cmap[cp] for cp in parse_unicodes(config["unicodes"]) if cp in cmap})
    x_height = getattr(font["OS/2"], "sxHeight", 0)
    if not x_height:
        raise BuildError("`oblique` pivots on half the x-height, but the source's OS/2 table has none")
    pivot = x_height / 2
    lean = math.tan(math.radians(-config["angle"]))  # angle < 0 leans right, as on the slnt axis

    glyph_set = font.getGlyphSet()
    recordings = {}
    for name in names:  # record everything first: composites must read unslanted components
        recordings[name] = DecomposingRecordingPen(glyph_set)
        glyph_set[name].draw(recordings[name])
    for name in names:
        pen = TTGlyphPen(None)
        recordings[name].replay(TransformPen(pen, (1, 0, lean, 1, -lean * pivot, 0)))
        glyph = pen.glyph()
        glyph.recalcBounds(glyf)
        glyf[name] = glyph
        hmtx[name] = (hmtx[name][0], getattr(glyph, "xMin", 0))
    # The baseline moved left by lean * pivot, so the slanted caret moves with it.
    font["hhea"].caretOffset = round(-lean * pivot)


def merge_script(font: TTFont, config: dict, style: str) -> TTFont:
    """Add another font's glyphs for `unicodes`, one cell each, keeping this font's line height.

    Characters this font already has keep their own glyphs, unless `replace` is set.
    """
    cell = font["hmtx"][font.getBestCmap()[ord(" ")]][0]
    donor = TTFont(config["source"])
    pin_axes(donor, config["styles"][style], f"{style}: merge {config['source'].name}")

    options = subset.Options()
    options.layout_features = list(MERGE_FEATURES)
    options.glyph_names = True
    options.hinting = False  # the instructions call the donor's fpgm, which the merged font does not have
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=parse_unicodes(config["unicodes"]))
    subsetter.subset(donor)
    if config.get("replace", False):
        # The merger keeps the first font's glyph for a shared character, so unmap this font's first.
        replaced = set(donor.getBestCmap())
        for table in font["cmap"].tables:
            if hasattr(table, "cmap"):
                table.cmap = {cp: g for cp, g in table.cmap.items() if cp not in replaced}

    # scale_upem scales by upm / head.unitsPerEm, so fake the donor's em to fold in `scale`.
    donor["head"].unitsPerEm = round(donor["head"].unitsPerEm / config.get("scale", 1.0))
    scale_upem(donor, font["head"].unitsPerEm)
    fit_to_cells(donor, cell)

    metrics = {tag: {f: getattr(font[tag], f) for f in fields} for tag, fields in VERTICAL_METRICS.items()}
    copyright_notices = [font["name"].getDebugName(0), donor["name"].getDebugName(0)]
    buffers = []
    for part in (font, donor):
        buffer = io.BytesIO()
        part.save(buffer)
        buffer.seek(0)
        buffers.append(buffer)
    merged = Merger().merge(buffers)

    for tag, values in metrics.items():  # the merger takes the taller of the two fonts
        for field, value in values.items():
            setattr(merged[tag], field, value)
    name = merged["name"]
    name.removeNames(nameID=0)
    notice = "\n".join(n for n in copyright_notices if n)
    name.setName(notice, 0, 3, 1, 0x409)
    name.setName(notice, 0, 1, 0, 0)
    merged["OS/2"].recalcUnicodeRanges(merged)
    merged["OS/2"].recalcCodePageRanges(merged)
    return merged


def parse_unicodes(ranges: list[str]) -> list[int]:
    """["0E01-0E3A", "25CC"] -> code points."""
    unicodes = []
    for item in ranges:
        first, _, last = item.removeprefix("U+").partition("-")
        unicodes.extend(range(int(first, 16), int(last or first, 16) + 1))
    return unicodes


def fit_to_cells(font: TTFont, cell: int) -> None:
    """Give every spacing glyph exactly one cell: condense the wider ones, centre the rest.

    Zero-width marks shift by the offset a typical glyph gets, so text drawn without shaping still
    puts them over the previous character. GPOS anchors move with their glyph, so shaped mark
    positions stay as the donor designed them.
    """
    hmtx, glyf = font["hmtx"], font["glyf"]
    classes = {}
    if "GDEF" in font and font["GDEF"].table.GlyphClassDef:
        classes = font["GDEF"].table.GlyphClassDef.classDefs
    order = font.getGlyphOrder()
    marks = {g for g in order if classes.get(g) == GDEF_MARK or hmtx[g][0] == 0}
    widths = sorted(hmtx[g][0] for g in order if g not in marks)
    typical = min(widths[len(widths) // 2], cell)

    transforms: dict[str, tuple[float, float]] = {}
    for g in order:
        width = hmtx[g][0]
        if g in marks:
            transforms[g] = (1.0, -(cell - typical) / 2)
        elif width > cell:
            transforms[g] = (cell / width, 0.0)
        else:
            transforms[g] = (1.0, (cell - width) / 2)

    glyph_set = font.getGlyphSet()
    recordings = {}
    for g in order:  # record everything first: composites must read unmoved components
        recordings[g] = DecomposingRecordingPen(glyph_set)
        glyph_set[g].draw(recordings[g])
    for g, (x_scale, dx) in transforms.items():
        pen = TTGlyphPen(None)
        recordings[g].replay(TransformPen(pen, (x_scale, 0, 0, 1, dx, 0)))
        glyph = pen.glyph()
        glyph.recalcBounds(glyf)
        glyf[g] = glyph
        hmtx[g] = (0 if g in marks else cell, getattr(glyph, "xMin", 0))
    if "GPOS" in font:
        move_anchors(font["GPOS"].table, transforms)


def move_anchors(gpos, transforms: dict[str, tuple[float, float]]) -> None:
    def moved(anchor, glyph):
        if anchor is None:
            return None
        x_scale, dx = transforms[glyph]
        anchor = copy.copy(anchor)  # anchors can be shared between glyphs
        anchor.XCoordinate = round(x_scale * anchor.XCoordinate + dx)
        return anchor

    def move_marks(coverage, mark_array):
        for g, record in zip(coverage.glyphs, mark_array.MarkRecord):
            record.MarkAnchor = moved(record.MarkAnchor, g)

    for lookup in gpos.LookupList.Lookup:
        for subtable in lookup.SubTable:
            kind = lookup.LookupType
            if kind == 9:
                kind, subtable = subtable.ExtensionLookupType, subtable.ExtSubTable
            if kind in (1, 2):
                for value in value_records(kind, subtable):
                    if getattr(value, "XAdvance", 0):
                        raise BuildError("a merged positioning lookup changes advance widths; drop its feature")
            elif kind == 3:
                raise BuildError("cursive attachment (GPOS type 3) is not supported in merged glyphs")
            elif kind == 4:
                move_marks(subtable.MarkCoverage, subtable.MarkArray)
                for g, record in zip(subtable.BaseCoverage.glyphs, subtable.BaseArray.BaseRecord):
                    record.BaseAnchor = [moved(a, g) for a in record.BaseAnchor]
            elif kind == 5:
                move_marks(subtable.MarkCoverage, subtable.MarkArray)
                for g, attach in zip(subtable.LigatureCoverage.glyphs, subtable.LigatureArray.LigatureAttach):
                    for component in attach.ComponentRecord:
                        component.LigatureAnchor = [moved(a, g) for a in component.LigatureAnchor]
            elif kind == 6:
                move_marks(subtable.Mark1Coverage, subtable.Mark1Array)
                for g, record in zip(subtable.Mark2Coverage.glyphs, subtable.Mark2Array.Mark2Record):
                    record.Mark2Anchor = [moved(a, g) for a in record.Mark2Anchor]


def value_records(kind: int, subtable):
    if kind == 1:
        yield from (subtable.Value if subtable.Format == 2 else [subtable.Value])
    elif subtable.Format == 1:
        for pair_set in subtable.PairSet:
            for record in pair_set.PairValueRecord:
                yield from (record.Value1, record.Value2)
    else:
        for class1 in subtable.Class1Record:
            for record in class1.Class2Record:
                yield from (record.Value1, record.Value2)


def feature_lookups(gsub, tag: str) -> list[int]:
    records = [rec for rec in gsub.FeatureList.FeatureRecord if rec.FeatureTag == tag]
    if not records:
        raise BuildError(f"feature {tag!r} is not in the source font")
    return sorted({i for rec in records for i in rec.Feature.LookupListIndex})


def single_substitutions(lookup, tag: str) -> list[dict[str, str]]:
    """Mappings of a type-1 lookup, one per subtable, in application order."""
    mappings = []
    for subtable in lookup.SubTable:
        lookup_type = lookup.LookupType
        if lookup_type == 7:
            lookup_type, subtable = subtable.ExtensionLookupType, subtable.ExtSubTable
        if lookup_type != 1:
            raise BuildError(
                f"feature {tag!r} uses GSUB lookup type {lookup_type}, which cannot be baked into "
                "the character map; list it under `always_on` instead"
            )
        mappings.append(subtable.mapping)
    return mappings


def freeze_into_cmap(font: TTFont, stages: list[list[str]]) -> None:
    """Point the cmap at the glyphs the shaper would pick with these features on.

    Within a stage, lookups apply in lookup-list order like a shaper does; stages run in sequence.
    """
    gsub = font["GSUB"].table
    plan = []
    for stage in stages:
        owner: dict[int, str] = {}
        for tag in stage:
            for i in feature_lookups(gsub, tag):
                owner.setdefault(i, tag)
        for i in sorted(owner):
            plan.append(single_substitutions(gsub.LookupList.Lookup[i], owner[i]))

    def resolve(glyph: str) -> str:
        for lookup in plan:
            for mapping in lookup:  # first subtable that covers the glyph wins
                if glyph in mapping:
                    glyph = mapping[glyph]
                    break
        return glyph

    for table in font["cmap"].tables:
        if hasattr(table, "cmap"):
            table.cmap = {cp: resolve(g) for cp, g in table.cmap.items()}


def enable_by_default(gsub, tag: str) -> None:
    """Add the lookups of `tag` to `calt` in every language system that has `tag`."""
    records = gsub.FeatureList.FeatureRecord
    lookups = feature_lookups(gsub, tag)
    created: dict[tuple[int, ...], int] = {}
    for lang_sys in iter_lang_sys(gsub):
        if not any(records[i].FeatureTag == tag for i in lang_sys.FeatureIndex):
            continue
        calt = [i for i in lang_sys.FeatureIndex if records[i].FeatureTag == "calt"]
        if calt:
            for i in calt:
                feature = records[i].Feature
                feature.LookupListIndex = sorted(set(feature.LookupListIndex) | set(lookups))
                feature.LookupCount = len(feature.LookupListIndex)
            continue
        key = tuple(lookups)
        if key not in created:
            feature = otTables.Feature()
            feature.FeatureParams = None
            feature.LookupListIndex = list(lookups)
            feature.LookupCount = len(lookups)
            record = otTables.FeatureRecord()
            record.FeatureTag = "calt"
            record.Feature = feature
            records.append(record)
            created[key] = len(records) - 1
        lang_sys.FeatureIndex.append(created[key])
    sort_feature_list(gsub)


def iter_lang_sys(gsub):
    for script in gsub.ScriptList.ScriptRecord:
        if script.Script.DefaultLangSys is not None:
            yield script.Script.DefaultLangSys
        for record in script.Script.LangSysRecord:
            yield record.LangSys


def sort_feature_list(gsub) -> None:
    """The spec requires FeatureList sorted by tag; remap every index that points into it."""
    records = gsub.FeatureList.FeatureRecord
    order = sorted(range(len(records)), key=lambda i: records[i].FeatureTag)
    new_index = {old: new for new, old in enumerate(order)}
    gsub.FeatureList.FeatureRecord = [records[i] for i in order]
    gsub.FeatureList.FeatureCount = len(records)
    for lang_sys in iter_lang_sys(gsub):
        lang_sys.FeatureIndex = sorted(new_index[i] for i in lang_sys.FeatureIndex)
        lang_sys.FeatureCount = len(lang_sys.FeatureIndex)
        if lang_sys.ReqFeatureIndex != 0xFFFF:
            lang_sys.ReqFeatureIndex = new_index[lang_sys.ReqFeatureIndex]
    variations = getattr(gsub, "FeatureVariations", None)
    if variations is not None:
        for record in variations.FeatureVariationRecord:
            for sub in record.FeatureTableSubstitution.SubstitutionRecord:
                sub.FeatureIndex = new_index[sub.FeatureIndex]


def enforce_monospace(font: TTFont) -> tuple[int, list[int]]:
    """Make every mapped glyph one cell wide (zero-width marks excepted).

    ASCII must already fit, or the axes are wrong. Other characters whose glyph is off-width
    (e.g. U+FB03 ffi, two cells) are unmapped so the app falls back to another font for them.
    """
    hmtx = font["hmtx"]
    cmap = font.getBestCmap()
    cell = hmtx[cmap[ord(" ")]][0]
    off_width = sorted(cp for cp, g in cmap.items() if hmtx[g][0] not in (0, cell))
    ascii_off = [chr(cp) for cp in off_width if 0x20 <= cp <= 0x7E]
    if ascii_off:
        raise BuildError(
            f"not monospaced: ASCII {''.join(ascii_off)!r} differ from the space width (for Recursive, pin MONO = 1)"
        )
    for table in font["cmap"].tables:
        if hasattr(table, "cmap"):
            for cp in off_width:
                table.cmap.pop(cp, None)
    return cell, off_width


def set_names(font: TTFont, family: str, style: str) -> None:
    name = font["name"]
    ps_name = f"{ps_family(family)}-{style.replace(' ', '')}"
    version = f"{font['head'].fontRevision:.3f}"
    for name_id in (1, 2, 3, 4, 6, 16, 17, 21, 22, 25):
        name.removeNames(nameID=name_id)
    values = {1: family, 2: style, 3: f"{version};{ps_name}", 4: f"{family} {style}", 6: ps_name}
    for name_id, value in values.items():
        name.setName(value, name_id, 3, 1, 0x409)
        name.setName(value, name_id, 1, 0, 0)


def set_style_flags(font: TTFont, style: str, slant: float | None) -> None:
    """Set the style-linking flags, and the italic angle from `slant` (the pinned slnt axis or the `oblique` angle)."""
    bold, italic = "Bold" in style, "Italic" in style
    os2 = font["OS/2"]
    selection = os2.fsSelection & ~(FS_ITALIC | FS_BOLD | FS_REGULAR)
    if italic:
        selection |= FS_ITALIC
    if bold:
        selection |= FS_BOLD
    if not (bold or italic):
        selection |= FS_REGULAR
    os2.fsSelection = selection
    os2.usWeightClass = 700 if bold else 400  # style linking pairs 400 with 700, whatever wght is pinned
    font["head"].macStyle = (1 if bold else 0) | (2 if italic else 0)

    if slant is None:  # upright source, no oblique: keep the source's italic angle and caret slope
        return
    font["post"].italicAngle = slant
    hhea = font["hhea"]
    if slant:
        hhea.caretSlopeRise = font["head"].unitsPerEm
        hhea.caretSlopeRun = round(hhea.caretSlopeRise * math.tan(math.radians(-slant)))
    else:
        hhea.caretSlopeRise, hhea.caretSlopeRun = 1, 0


def set_monospace_flags(font: TTFont, cell: int) -> None:
    font["OS/2"].panose.bProportion = PANOSE_MONOSPACED
    font["OS/2"].xAvgCharWidth = cell
    font["post"].isFixedPitch = 1


if __name__ == "__main__":
    sys.exit(main())
