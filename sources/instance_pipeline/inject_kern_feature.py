#!/usr/bin/env python3
"""Inject a ``feature kern {}`` block into each instance UFO's features.fea.

UFO kerning data is stored in ``kerning.plist`` (pair values) and
``groups.plist`` (kern-side class members).  When fontmake interpolates
instance UFOs without ``--expand-features-to-instances``, it copies the
master's ``features.fea`` verbatim but does NOT generate a kern feature --
kerning values live in the plist files.

This script reads those plists and appends a ``feature kern {}`` block to
``features.fea``.  The generated block uses OpenType FEA class-pair
positioning (``@kern1_XXX / @kern2_XXX``), which is:

* Multi-master compatible -- all masters share identical class definitions
  and pair structure; only the numeric values differ across interpolation
  steps.
* Merge-friendly -- class names use the standard UFO naming convention so
  they are easy to identify and namespace if needed.

Usage (standalone)::

    python inject_kern_feature.py path/to/Font.ufo [Font2.ufo ...]
    python inject_kern_feature.py --ufo-dir path/to/instance_ufos/

The script is also called programmatically from ``build_instances.py``.
"""

from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# --------------------------------------------------------------------------- #
#  Plist helpers                                                                #
# --------------------------------------------------------------------------- #

def _load_plist(path: Path) -> dict:
    with path.open("rb") as fh:
        return plistlib.load(fh)


# --------------------------------------------------------------------------- #
#  FEA generation                                                               #
# --------------------------------------------------------------------------- #

def _fea_class_name(group_key: str) -> str:
    """Map a UFO public.kern group key to a FEA class name.

    ``public.kern1.KO_A``  ->  ``@kern1_KO_A``
    ``public.kern2.KO_V``  ->  ``@kern2_KO_V``
    """
    if group_key.startswith("public.kern1."):
        return "@kern1_" + group_key[len("public.kern1."):]
    if group_key.startswith("public.kern2."):
        return "@kern2_" + group_key[len("public.kern2."):]
    raise ValueError(f"Not a kern group key: {group_key!r}")


def build_kern_fea(groups: dict, kerning: dict) -> str:
    """Return the complete FEA text (class defs + feature block) for kerning.

    Parameters
    ----------
    groups:
        Contents of ``groups.plist`` (full dict, including non-kern groups).
    kerning:
        Contents of ``kerning.plist``.

    Returns
    -------
    str
        FEA text ready to be appended to ``features.fea``.
    """
    kern1: Dict[str, List[str]] = {}  # group_key -> [glyph, ...]
    kern2: Dict[str, List[str]] = {}

    for key, glyphs in groups.items():
        if key.startswith("public.kern1."):
            kern1[key] = list(glyphs)
        elif key.startswith("public.kern2."):
            kern2[key] = list(glyphs)

    lines: List[str] = []

    # -- 1. Class definitions (outside the feature block) --
    if kern1:
        lines.append("# Kern side 1 (left-side) groups")
        for key in sorted(kern1):
            fea = _fea_class_name(key)
            members = " ".join(kern1[key])
            lines.append(f"{fea} = [{members}];")
        lines.append("")

    if kern2:
        lines.append("# Kern side 2 (right-side) groups")
        for key in sorted(kern2):
            fea = _fea_class_name(key)
            members = " ".join(kern2[key])
            lines.append(f"{fea} = [{members}];")
        lines.append("")

    # -- 2. Parse kerning pairs into three buckets --
    #    glyph-glyph  : format-1 exceptions, highest lookup priority
    #    mixed        : glyph-class or class-glyph, emitted with enumerate
    #    cls_cls      : class-class, format-2 main lookup
    glyph_glyph: List[Tuple[str, str, int]] = []
    mixed:       List[Tuple[str, str, int]] = []
    cls_cls:     List[Tuple[str, str, int]] = []

    for first_raw, seconds in kerning.items():
        first_is_group = first_raw.startswith("public.kern1.")
        first_fea = _fea_class_name(first_raw) if first_is_group else first_raw

        for second_raw, value in seconds.items():
            ivalue = int(value)
            if ivalue == 0:
                continue  # zero-value pairs are no-ops; omit them

            second_is_group = second_raw.startswith("public.kern2.")
            second_fea = _fea_class_name(second_raw) if second_is_group else second_raw

            if not first_is_group and not second_is_group:
                glyph_glyph.append((first_fea, second_fea, ivalue))
            elif first_is_group and second_is_group:
                cls_cls.append((first_fea, second_fea, ivalue))
            else:
                mixed.append((first_fea, second_fea, ivalue))

    # Sort each bucket for deterministic output.
    glyph_glyph.sort()
    mixed.sort()
    cls_cls.sort()

    # -- 3. Build the feature block --
    fb: List[str] = ["feature kern {"]

    if glyph_glyph:
        fb.append("    # Glyph-pair exceptions (PairPosFormat 1)")
        for first, second, value in glyph_glyph:
            fb.append(f"    pos {first} {second} {value};")

    if mixed:
        fb.append("    # Mixed glyph/class pairs (enumerate -> PairPosFormat 1)")
        for first, second, value in mixed:
            fb.append(f"    enumerate; pos {first} {second} {value};")

    if cls_cls:
        fb.append("    # Class-pair adjustments (PairPosFormat 2)")
        for first, second, value in cls_cls:
            fb.append(f"    pos {first} {second} {value};")

    fb.append("} kern;")

    lines.extend(fb)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Per-UFO entry point                                                          #
# --------------------------------------------------------------------------- #

def inject_kern_feature(ufo_path: Path, force: bool = False) -> bool:
    """Append a kern feature block to ``features.fea`` inside *ufo_path*.

    Returns ``True`` if the file was modified.
    """
    features_fea  = ufo_path / "features.fea"
    kerning_plist = ufo_path / "kerning.plist"
    groups_plist  = ufo_path / "groups.plist"

    if not ufo_path.is_dir():
        print(f"  ERROR: not a directory: {ufo_path}")
        return False

    if not features_fea.exists():
        print(f"  WARNING: no features.fea in {ufo_path.name}, skipping")
        return False

    if not kerning_plist.exists():
        print(f"  WARNING: no kerning.plist in {ufo_path.name}, skipping")
        return False

    fea_text = features_fea.read_text(encoding="utf-8")

    kern_present = bool(re.search(r"\bfeature\s+kern\b", fea_text))
    if kern_present and not force:
        print(
            f"  SKIP: {ufo_path.name} already has a kern feature "
            "(re-run with --force to regenerate)"
        )
        return False

    # Load plist data
    kerning = _load_plist(kerning_plist)
    groups  = _load_plist(groups_plist) if groups_plist.exists() else {}

    if not kerning:
        print(f"  SKIP: {ufo_path.name} -- kerning.plist is empty")
        return False

    kern_fea = build_kern_fea(groups, kerning)

    # Strip existing kern block when force-regenerating.
    if kern_present and force:
        # Remove the class-def header block generated by this script.
        fea_text = re.sub(
            r"\n# Kern side 1.*?(?=\nfeature |\Z)",
            "\n",
            fea_text,
            flags=re.DOTALL,
        )
        # Remove the kern feature block itself.
        fea_text = re.sub(
            r"\nfeature kern [{].*?[}] kern;",
            "",
            fea_text,
            flags=re.DOTALL,
        )

    fea_text = fea_text.rstrip("\n") + "\n\n" + kern_fea
    features_fea.write_text(fea_text, encoding="utf-8")

    pair_count = (
        kern_fea.count("\n    pos ")
        + kern_fea.count("\n    enumerate; pos ")
    )
    print(f"  OK: {ufo_path.name} -- injected kern feature ({pair_count} pairs)")
    return True


# --------------------------------------------------------------------------- #
#  Multi-master consistency check                                               #
# --------------------------------------------------------------------------- #

def check_kern_structure_consistency(ufo_paths: List[Path]) -> None:
    """Warn if kern group structures differ across UFOs.

    For multi-master sources every UFO must have the same kern groups and pair
    keys (only values may differ).  Differences here indicate a problem in the
    source data.
    """
    structures: Dict[Path, Tuple[frozenset, frozenset, frozenset]] = {}

    for ufo in ufo_paths:
        groups_path  = ufo / "groups.plist"
        kerning_path = ufo / "kerning.plist"

        if not groups_path.exists() or not kerning_path.exists():
            continue

        groups  = _load_plist(groups_path)
        kerning = _load_plist(kerning_path)

        kern1_keys = frozenset(k for k in groups if k.startswith("public.kern1."))
        kern2_keys = frozenset(k for k in groups if k.startswith("public.kern2."))
        pair_keys  = frozenset(
            (first, second)
            for first, seconds in kerning.items()
            for second in seconds
        )
        structures[ufo] = (kern1_keys, kern2_keys, pair_keys)

    if len(structures) < 2:
        return

    reference_ufo = next(iter(structures))
    ref_k1, ref_k2, ref_pairs = structures[reference_ufo]

    for ufo, (k1, k2, pairs) in structures.items():
        if ufo == reference_ufo:
            continue
        if k1 != ref_k1:
            print(
                f"  WARNING: kern1 group mismatch between "
                f"{reference_ufo.name} and {ufo.name}"
            )
        if k2 != ref_k2:
            print(
                f"  WARNING: kern2 group mismatch between "
                f"{reference_ufo.name} and {ufo.name}"
            )
        if pairs != ref_pairs:
            only_ref  = ref_pairs - pairs
            only_this = pairs - ref_pairs
            if only_ref:
                print(
                    f"  WARNING: {reference_ufo.name} has "
                    f"{len(only_ref)} pair(s) not in {ufo.name}"
                )
            if only_this:
                print(
                    f"  WARNING: {ufo.name} has "
                    f"{len(only_this)} pair(s) not in {reference_ufo.name}"
                )


# --------------------------------------------------------------------------- #
#  CLI                                                                          #
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ufo-dir",
        metavar="DIR",
        help="Directory whose immediate *.ufo children will be processed.",
    )
    parser.add_argument(
        "ufo",
        nargs="*",
        metavar="UFO",
        help="One or more UFO paths to process.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing kern feature block.",
    )
    parser.add_argument(
        "--check-consistency",
        action="store_true",
        default=True,
        help="Warn when kern group/pair structure differs across UFOs (default: on).",
    )
    parser.add_argument(
        "--no-check-consistency",
        dest="check_consistency",
        action="store_false",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.ufo_dir:
        ufo_dir = Path(args.ufo_dir)
        ufo_paths = sorted(ufo_dir.glob("*.ufo"))
        if not ufo_paths:
            print(f"No *.ufo directories found in {ufo_dir}")
            return 1
    elif args.ufo:
        ufo_paths = [Path(p) for p in args.ufo]
    else:
        print("ERROR: supply UFO paths or --ufo-dir.")
        return 1

    if args.check_consistency and len(ufo_paths) > 1:
        print("Checking kern structure consistency across UFOs ...")
        check_kern_structure_consistency(ufo_paths)

    modified = 0
    for ufo in ufo_paths:
        print(f"Processing {ufo.name} ...")
        if inject_kern_feature(ufo, force=args.force):
            modified += 1

    print(f"\nDone. Modified {modified}/{len(ufo_paths)} UFO(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

