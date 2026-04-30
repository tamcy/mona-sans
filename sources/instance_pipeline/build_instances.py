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
from typing import Dict, Iterable, List, Tuple

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
    location: Dict[str, float]  # keyed by axis tag


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


def config_instances(config: dict) -> List[InstanceRequest]:
    result: List[InstanceRequest] = []
    for row in config.get("instances", []):
        name = row.get("name")
        location = row.get("location", {})
        if not name or not isinstance(location, dict):
            raise ValueError("Each config instance needs 'name' and 'location' mapping")
        result.append(InstanceRequest(name=name, location={k: float(v) for k, v in location.items()}))
    return result


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
            inst_name = row.get("name")
            location = row.get("location", {})
            if not inst_name or not isinstance(location, dict):
                raise ValueError(
                    f"Preset '{name}' entries need 'name' and 'location' mapping"
                )
            result.append(
                InstanceRequest(
                    name=inst_name,
                    location={k: float(v) for k, v in location.items()},
                )
            )
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
) -> List[InstanceRequest]:
    if not overrides:
        return requests
    merged: List[InstanceRequest] = []
    for req in requests:
        location = dict(req.location)
        location.update(overrides)
        merged.append(InstanceRequest(name=req.name, location=location))
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

        for tag in known_tags:
            if tag not in req.location:
                raise ValueError(
                    f"Instance '{req.name}' is missing axis '{tag}'. "
                    f"Required axes: {', '.join(sorted(known_tags))}"
                )

        for tag in req.location.keys():
            if tag not in known_tags:
                raise ValueError(
                    f"Instance '{req.name}' uses unknown axis '{tag}'. "
                    f"Known: {', '.join(sorted(known_tags))}"
                )

        design_location: Dict[str, float] = {}
        for tag, user_value in req.location.items():
            ds_minmax = axis_bounds_for_tag(ds, tag)
            min_value, max_value = resolve_bounds(config, tag, ds_minmax)
            if user_value < min_value or user_value > max_value:
                raise ValueError(
                    f"Out-of-range value for '{req.name}' axis {tag}: {user_value}. "
                    f"Allowed: {min_value}..{max_value}"
                )
            axis_name = axis_name_for_tag(ds, tag)
            design_location[axis_name] = map_user_to_design(ds, tag, user_value)

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
        help="Global axis override applied to all selected instances. Repeatable.",
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
    requests = apply_global_axis_overrides(requests, overrides)

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
    # Step 4 – inject feature kern from kerning/groups plists              #
    # ------------------------------------------------------------------ #
    if not args.dry_run and not args.skip_kern_inject:
        print("\nInjecting kern feature into instance UFOs …")
        existing = [p for p in expected_ufo_paths if p.is_dir()]
        if len(existing) > 1:
            check_kern_structure_consistency(existing)
        for ufo_path in existing:
            print(f"  Processing {ufo_path.name} …")
            inject_kern_feature(ufo_path, force=args.force_kern_inject)

    print(f"Wrote custom designspace: {custom_designspace}")
    print(f"Instance UFO directory: {final_instance_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

