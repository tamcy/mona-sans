#!/usr/bin/env python3
"""Generate custom UFO instances from MonaSans.glyphspackage.

Pipeline:
1) Build master UFOs + base designspace from Glyphs source (fontmake)
2) Create a custom designspace with only user-defined instances
3) Interpolate instance UFOs with fontmake (-o ufo --interpolate)
4) Inject ``feature kern {}`` from kerning/groups plists into features.fea

fontmake is used for step 3 (not makeinstancesufo) because the source has a
4-axis structure with a discrete ital axis and optical-size masters; fontmake's
splitInterpolable handles this correctly, while makeinstancesufo/ufoProcessor
raises "Locations must be unique" on such sources.

Output UFOs have cubic outlines and fully-expanded features.fea (including the
kern feature) — the format required as input sources for a subsequent
makeinstancesufo run.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from fontTools.designspaceLib import DesignSpaceDocument, InstanceDescriptor

from inject_kern_feature import (
    check_kern_structure_consistency,
    inject_kern_feature,
)

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install with: pip install PyYAML"
    ) from exc


@dataclasses.dataclass
class InstanceRequest:
    name: str
    location: Dict[str, float]  # keyed by axis tag — values in USER space
    design_location: Dict[str, float] = dataclasses.field(default_factory=dict)
    # design_location: axis tag → design-space value, bypasses the user→design
    # axis map for that axis.  Use this when the user-space map is non-monotonic
    # or otherwise inaccessible (e.g. opsz in this source has text masters at
    # design=1 which cannot be reached through any user-space value).


def parse_axis_assignment(raw: str) -> Tuple[str, float]:
    """Parse AXIS=VALUE into (axis_tag, float_value)."""
    if "=" not in raw:
        raise ValueError(f"Invalid axis assignment '{raw}'. Expected AXIS=VALUE")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{4}", key):
        raise ValueError(f"Invalid axis tag '{key}'. Expected 4-char tag")
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid value for axis '{key}': {value}") from exc
    return key, parsed


def parse_inline_instance(raw: str) -> InstanceRequest:
    """Parse NAME:wdth=100,wght=150,opsz=14,ital=0."""
    if ":" not in raw:
        raise ValueError(
            f"Invalid --instance '{raw}'. Expected NAME:axis=value,..."
        )
    name, location_raw = raw.split(":", 1)
    name = name.strip()
    if not name:
        raise ValueError("Instance name cannot be empty")
    location: Dict[str, float] = {}
    for token in location_raw.split(","):
        tag, val = parse_axis_assignment(token.strip())
        location[tag] = val
    return InstanceRequest(name=name, location=location)


def sanitize_for_ps_name(value: str) -> str:
    # Keep this close to OpenType naming constraints.
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", value.replace(" ", ""))
    return cleaned[:63] if cleaned else "Unnamed"


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def _parse_instance_row(row: dict, context: str) -> InstanceRequest:
    """Parse one YAML instance row into an InstanceRequest."""
    name = row.get("name")
    location = row.get("location", {})
    design_location = row.get("design_location", {})
    if not name or not isinstance(location, dict):
        raise ValueError(f"{context}: each entry needs 'name' and 'location' mapping")
    if not isinstance(design_location, dict):
        raise ValueError(f"{context} '{name}': 'design_location' must be a mapping")
    return InstanceRequest(
        name=str(name),
        location={k: float(v) for k, v in location.items()},
        design_location={k: float(v) for k, v in design_location.items()},
    )


def config_instances(config: dict) -> List[InstanceRequest]:
    return [_parse_instance_row(row, "instances") for row in config.get("instances", [])]


def preset_instances(config: dict, selected: Iterable[str]) -> List[InstanceRequest]:
    presets = config.get("presets", {})
    if not isinstance(presets, dict):
        raise ValueError("Config key 'presets' must be a mapping")

    result: List[InstanceRequest] = []
    for name in selected:
        if name not in presets:
            available = ", ".join(sorted(presets.keys()))
            raise ValueError(f"Unknown preset '{name}'. Available: {available}")
        rows = presets[name]
        if not isinstance(rows, list):
            raise ValueError(f"Preset '{name}' must be a list")
        for row in rows:
            result.append(_parse_instance_row(row, f"preset '{name}'"))
    return result


def run_cmd(cmd: List[str], cwd: Path, dry_run: bool) -> None:
    print("$", " ".join(cmd))
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(cwd), check=True)


def build_master_designspace(
    repo_root: Path,
    glyphs_source: Path,
    designspace_path: Path,
    master_dir: Path,
    instance_dir: Path,
    dry_run: bool,
) -> None:
    designspace_path.parent.mkdir(parents=True, exist_ok=True)
    master_dir.mkdir(parents=True, exist_ok=True)
    instance_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "fontmake",
        "-g",
        str(glyphs_source),
        "-o",
        "ufo",
        "--master-dir",
        str(master_dir),
        "--instance-dir",
        str(instance_dir),
        "--designspace-path",
        str(designspace_path),
        "--ufo-structure",
        "package",
    ]
    run_cmd(cmd, cwd=repo_root, dry_run=dry_run)


def axis_bounds_for_tag(ds: DesignSpaceDocument, axis_tag: str) -> Tuple[float, float]:
    for axis in ds.axes:
        if axis.tag == axis_tag:
            return float(axis.minimum), float(axis.maximum)
    raise ValueError(f"Axis '{axis_tag}' is not present in designspace")


def axis_name_for_tag(ds: DesignSpaceDocument, axis_tag: str) -> str:
    for axis in ds.axes:
        if axis.tag == axis_tag:
            if axis.name is None:
                raise ValueError(f"Axis '{axis_tag}' has no name in designspace")
            return axis.name
    raise ValueError(f"Axis '{axis_tag}' is not present in designspace")


def map_user_to_design(ds: DesignSpaceDocument, axis_tag: str, value: float) -> float:
    for axis in ds.axes:
        if axis.tag == axis_tag:
            if axis.map:
                return float(axis.map_forward(value))
            return float(value)
    raise ValueError(f"Axis '{axis_tag}' is not present in designspace")


def _canonicalize_axes(ds: DesignSpaceDocument) -> None:
    """Strip user→design maps from axes whose sources extend beyond the mapped range.

    When an axis has a non-monotonic or restricted user→design map (like the
    ``opsz`` axis in this source), source masters can sit at design-space
    coordinates that are outside the mapped range.  varLib normalises instance
    locations against the *mapped* range, so those coordinates normalise to
    values outside ``[-1, 1]`` and fontmake silently drops the instances.

    This function fixes the problem by removing the map for affected axes and
    resetting ``minimum / default / maximum`` to the true design-space extent
    of the sources.  Because the custom designspace is only used for generating
    static instances (not for end-user variable-font consumption), having
    user-space == design-space is perfectly fine.
    """
    for axis in ds.axes:
        if not axis.map:
            continue  # no map → user space already equals design space

        # Collect every design-space value this axis takes across all sources.
        design_vals: List[float] = [
            float(src.location[axis.name])
            for src in ds.sources
            if axis.name in (src.location or {})
        ]
        if not design_vals:
            continue

        d_min = min(design_vals)
        d_max = max(design_vals)

        # Mapped design-space range (what the old map covered).
        mapped_design_vals = [float(output) for _, output in axis.map]
        mapped_min = min(mapped_design_vals) if mapped_design_vals else d_min
        mapped_max = max(mapped_design_vals) if mapped_design_vals else d_max

        # Only act when sources actually extend *outside* the mapped range.
        if d_min >= mapped_min and d_max <= mapped_max:
            continue  # all sources reachable through existing map → leave alone

        # Where does the current user-space default land in design space?
        d_default = float(axis.map_forward(axis.default))
        d_default = max(d_min, min(d_max, d_default))

        axis.map = []
        axis.minimum = d_min
        axis.default = d_default
        axis.maximum = d_max


def _deduplicate_sources(ds: DesignSpaceDocument) -> None:
    """Remove sources that share an identical design-space location.

    Glyphs brace-layers can produce multiple source UFOs at the same design
    coordinate when several glyphs carry brace layers with the same values.
    varLib's variation model requires each source to occupy a unique location;
    duplicates cause interpolation to fail with "Locations must be unique".

    We keep the *first* source encountered at each location and discard
    subsequent ones, logging the removed count.
    """
    seen: dict = {}
    unique: List = []
    for src in ds.sources:
        key = tuple(sorted((k, float(v)) for k, v in (src.location or {}).items()))
        if key not in seen:
            seen[key] = src
            unique.append(src)
    removed = len(ds.sources) - len(unique)
    if removed:
        import logging as _logging
        _logging.getLogger(__name__).info(
            "Removed %d duplicate source(s) with identical design-space locations.",
            removed,
        )
    ds.sources = unique


def design_axis_bounds(ds: DesignSpaceDocument, axis_tag: str) -> Tuple[float, float]:
    """Return (min, max) design-space values for *axis_tag* by scanning all sources.

    This is more reliable than mapping the user-space extremes through a
    potentially non-monotonic axis map.  For example, the opsz axis in this
    source has a non-monotonic user→design map; the text masters sit at
    design=1, which the user-space map never reaches.  Scanning sources gives
    the true design-space extent.
    """
    axis_name = axis_name_for_tag(ds, axis_tag)
    values: List[float] = []
    for source in ds.sources:
        loc = source.location or {}
        if axis_name in loc:
            values.append(float(loc[axis_name]))
    if not values:
        raise ValueError(f"No sources found with axis name '{axis_name}'")
    return min(values), max(values)


def resolve_bounds(config: dict, axis_tag: str, ds_bounds: Tuple[float, float]) -> Tuple[float, float]:
    configured = (config.get("bounds") or {}).get(axis_tag)
    if configured is None:
        return ds_bounds
    try:
        cmin = float(configured["min"])
        cmax = float(configured["max"])
    except Exception as exc:
        raise ValueError(
            f"bounds.{axis_tag} must provide numeric 'min' and 'max'"
        ) from exc
    # Config can only narrow, not expand, designspace bounds.
    if cmin < ds_bounds[0] or cmax > ds_bounds[1]:
        raise ValueError(
            f"bounds.{axis_tag} ({cmin}..{cmax}) exceeds designspace bounds "
            f"({ds_bounds[0]}..{ds_bounds[1]})"
        )
    return cmin, cmax


def apply_global_axis_overrides(
    requests: List[InstanceRequest],
    overrides: Dict[str, float],
    design_overrides: Optional[Dict[str, float]] = None,
) -> List[InstanceRequest]:
    d_overrides: Dict[str, float] = design_overrides or {}
    if not overrides and not d_overrides:
        return requests
    merged: List[InstanceRequest] = []
    for req in requests:
        location = dict(req.location)
        location.update(overrides)
        # Remove any user-space entry for axes that are being overridden in design space
        for tag in d_overrides:
            location.pop(tag, None)
        d_location = dict(req.design_location)
        d_location.update(d_overrides)
        merged.append(InstanceRequest(name=req.name, location=location, design_location=d_location))
    return merged


def build_custom_designspace(
    base_designspace_path: Path,
    out_designspace_path: Path,
    family_name: str,
    requests: List[InstanceRequest],
    config: dict,
    out_instance_dir: Path,
) -> List[Path]:
    ds = DesignSpaceDocument.fromfile(str(base_designspace_path))

    # Ensure axes whose sources extend beyond the user→design map range are
    # rewritten so that all design-space source positions are reachable by
    # varLib's normaliser.  The opsz axis in this source is the key case:
    # Text masters live at design=1, which is below the map's output minimum
    # of 44, causing fontmake to silently drop instances whose opsz normalises
    # outside [-1, 1].
    _canonicalize_axes(ds)

    # Remove sources that share an identical design-space location.  Glyphs
    # brace-layers can generate duplicate source entries (e.g. multiple glyphs
    # with brace-layer "{75, 300, 1, 1}"), which causes varLib to raise
    # "Locations must be unique" when building the interpolation model.
    _deduplicate_sources(ds)

    known_tags = {axis.tag for axis in ds.axes}
    if not known_tags:
        raise ValueError("No axes found in base designspace")

    filenames: List[Path] = []
    ds.instances = []

    seen_names = set()
    for req in requests:
        if req.name in seen_names:
            raise ValueError(f"Duplicate instance name '{req.name}'")
        seen_names.add(req.name)

        # Validate that all known axes are covered by either location or design_location
        for tag in known_tags:
            if tag not in req.location and tag not in req.design_location:
                raise ValueError(
                    f"Instance '{req.name}' is missing axis '{tag}'. "
                    f"Provide it in 'location' (user space) or 'design_location' (design space). "
                    f"Required axes: {', '.join(sorted(known_tags))}"
                )

        for tag in req.location.keys():
            if tag not in known_tags:
                raise ValueError(
                    f"Instance '{req.name}' uses unknown axis '{tag}' in location. "
                    f"Known: {', '.join(sorted(known_tags))}"
                )

        for tag in req.design_location.keys():
            if tag not in known_tags:
                raise ValueError(
                    f"Instance '{req.name}' uses unknown axis '{tag}' in design_location. "
                    f"Known: {', '.join(sorted(known_tags))}"
                )

        # Build design-space location: start from user-space location (mapped)
        design_location: Dict[str, float] = {}
        for tag, user_value in req.location.items():
            if tag in req.design_location:
                # design_location takes precedence; skip user-space processing
                continue
            ds_minmax = axis_bounds_for_tag(ds, tag)
            min_value, max_value = resolve_bounds(config, tag, ds_minmax)
            if user_value < min_value or user_value > max_value:
                raise ValueError(
                    f"Out-of-range value for '{req.name}' axis {tag}: {user_value}. "
                    f"Allowed: {min_value}..{max_value}"
                )
            axis_name = axis_name_for_tag(ds, tag)
            design_location[axis_name] = map_user_to_design(ds, tag, user_value)

        # Apply design_location overrides (raw design-space values, no mapping)
        for tag, design_value in req.design_location.items():
            d_min, d_max = design_axis_bounds(ds, tag)
            if design_value < d_min or design_value > d_max:
                raise ValueError(
                    f"Out-of-range design-space value for '{req.name}' axis {tag}: {design_value}. "
                    f"Design-space bounds (from sources): {d_min}..{d_max}"
                )
            axis_name = axis_name_for_tag(ds, tag)
            design_location[axis_name] = design_value

        style_name = req.name
        ps_name = sanitize_for_ps_name(f"{family_name}-{style_name}")
        file_name = f"{sanitize_for_ps_name(req.name)}.ufo"
        out_ufo = out_instance_dir / file_name
        rel = out_ufo.relative_to(out_designspace_path.parent)

        inst = InstanceDescriptor()
        inst.familyName = family_name
        inst.styleName = style_name
        inst.postScriptFontName = ps_name
        inst.name = f"{family_name} {style_name}"
        inst.location = design_location
        inst.filename = rel.as_posix()

        ds.addInstance(inst)
        filenames.append(out_ufo)

    out_designspace_path.parent.mkdir(parents=True, exist_ok=True)
    ds.write(str(out_designspace_path))
    return filenames


def verify_features_files(instance_paths: Iterable[Path]) -> None:
    missing = []
    for ufo_path in instance_paths:
        fea = ufo_path / "features.fea"
        if not fea.exists():
            missing.append(str(fea))
    if missing:
        sample = "\n".join(missing[:5])
        raise RuntimeError(
            "Missing features.fea in generated UFOs. First entries:\n" + sample
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: current working directory)",
    )
    parser.add_argument(
        "--glyphs-source",
        default="sources/MonaSans.glyphspackage",
        help="Path to Glyphs source package",
    )
    parser.add_argument(
        "--config",
        default="sources/instance_pipeline/instances.sample.yaml",
        help="Path to custom instances YAML config",
    )
    parser.add_argument(
        "--preset",
        action="append",
        default=[],
        help="Preset name from config. Repeatable.",
    )
    parser.add_argument(
        "--instance",
        action="append",
        default=[],
        help="Inline instance in format NAME:wdth=...,wght=...,opsz=...,ital=...",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="AXIS=VALUE",
        help="Global axis override applied to all selected instances (user space). Repeatable.",
    )
    parser.add_argument(
        "--set-design",
        action="append",
        default=[],
        metavar="AXIS=VALUE",
        help=(
            "Global design-space axis override applied to all instances. "
            "Bypasses the user→design axis map. "
            "Useful for opsz: use opsz=1 for Text masters, opsz=100 for Display. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--work-dir",
        default="sources/build/custom_instances",
        help="Working directory for masters/designspaces/output",
    )
    parser.add_argument(
        "--skip-master-build",
        action="store_true",
        help="Reuse existing base designspace and master UFOs in --work-dir",
    )
    parser.add_argument(
        "--skip-kern-inject",
        action="store_true",
        help="Do not inject the kern feature into output UFOs.",
    )
    parser.add_argument(
        "--force-kern-inject",
        action="store_true",
        help="Overwrite an existing kern feature block when injecting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not execute external build/interpolation commands",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    repo_root = Path(args.repo_root).resolve()
    glyphs_source = (repo_root / args.glyphs_source).resolve()
    config_path = (repo_root / args.config).resolve()

    work_dir = (repo_root / args.work_dir).resolve()
    master_dir = work_dir / "master_ufo"
    scratch_instance_dir = work_dir / "instance_ufos_source"
    final_instance_dir = work_dir / "instance_ufos"
    base_designspace = work_dir / "MonaSans.base.designspace"
    custom_designspace = work_dir / "MonaSans.custom.designspace"

    cfg = load_config(config_path)
    family_name = cfg.get("familyName", "Mona Sans Fork")

    requests: List[InstanceRequest] = []
    requests.extend(config_instances(cfg))
    requests.extend(preset_instances(cfg, args.preset))
    requests.extend(parse_inline_instance(raw) for raw in args.instance)

    overrides: Dict[str, float] = {}
    for raw in args.set:
        tag, value = parse_axis_assignment(raw)
        overrides[tag] = value
    design_overrides: Dict[str, float] = {}
    for raw in args.set_design:
        tag, value = parse_axis_assignment(raw)
        design_overrides[tag] = value
    requests = apply_global_axis_overrides(requests, overrides, design_overrides)

    if not requests:
        raise SystemExit(
            "No instances selected. Use config 'instances', --preset, or --instance"
        )

    if not args.skip_master_build:
        build_master_designspace(
            repo_root=repo_root,
            glyphs_source=glyphs_source,
            designspace_path=base_designspace,
            master_dir=master_dir,
            instance_dir=scratch_instance_dir,
            dry_run=args.dry_run,
        )

    if not base_designspace.exists():
        if args.dry_run:
            print(
                "Dry-run note: base designspace does not exist yet, so range/location "
                "validation and custom designspace writing were skipped."
            )
            print(
                "Run once without --dry-run (or with --skip-master-build after an "
                "initial build) to complete full validation."
            )
            return 0
        raise SystemExit(
            f"Base designspace not found: {base_designspace}. "
            "Run without --skip-master-build first."
        )

    final_instance_dir.mkdir(parents=True, exist_ok=True)
    expected_ufo_paths = build_custom_designspace(
        base_designspace_path=base_designspace,
        out_designspace_path=custom_designspace,
        family_name=family_name,
        requests=requests,
        config=cfg,
        out_instance_dir=final_instance_dir,
    )

    interpolate_cmd = [
        sys.executable,
        "-m",
        "fontmake",
        "-m",
        str(custom_designspace),
        "-o",
        "ufo",
        "--interpolate",
        "--ufo-structure",
        "package",
        # NOTE: --expand-features-to-instances is intentionally omitted.
        # It re-parses features.fea through feaLib, which fails on `condition`
        # blocks (used for variable font feature variations in this source).
        # Without it, fontmake copies the master's features.text verbatim into
        # each instance — no parsing, full content preserved, condition blocks
        # included intact.
        "--output-dir",
        str(final_instance_dir),
    ]
    run_cmd(interpolate_cmd, cwd=repo_root, dry_run=args.dry_run)

    if not args.dry_run:
        verify_features_files(expected_ufo_paths)

    # ------------------------------------------------------------------ #
    # Step 4 -- merge kern pairs into features.fea.merged                 #
    # ------------------------------------------------------------------ #
    if not args.dry_run and not args.skip_kern_inject:
        print("\nMerging kern feature into instance UFOs ...")
        existing = [p for p in expected_ufo_paths if p.is_dir()]
        if len(existing) > 1:
            check_kern_structure_consistency(existing)
        for ufo_path in existing:
            print(f"  Processing {ufo_path.name} ...")
            inject_kern_feature(ufo_path, force=args.force_kern_inject, out_suffix=".merged")

    print(f"Wrote custom designspace: {custom_designspace}")
    print(f"Instance UFO directory: {final_instance_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

