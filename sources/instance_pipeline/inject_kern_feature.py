#!/usr/bin/env python3
"""Merge UFO kerning data into a ``features.fea.merged`` file.

UFO kerning is stored in ``kerning.plist`` (pair values) and ``groups.plist``
(kern-side class members).  The ``features.fea`` that fontmake copies into
each instance UFO already contains a ``feature kern {}`` block with a global
optical-size tracking rule::

    feature kern {
    pos @All <14 0 14 0 (opsz:20) ... 0 0 0 0>;

    # Automatic Code

    } kern;

This script:

1. Reads the UFO's ``features.fea`` as-is (the source of truth for the
   non-pair content -- tracking, language-system overrides, etc.).
2. Reads ``kerning.plist`` + ``groups.plist`` and generates class-based pair
   kerning FEA (``@kern1_xxx / @kern2_xxx``).
3. Injects the class definitions and pair rules INTO the existing
   ``feature kern {}`` block (just before ``} kern;``) so the optical-size
   tracking rule is preserved.
4. Writes the result to ``features.fea.merged`` (the original is untouched).

The output is multi-master compatible: all masters share identical class
definitions and pair structure; only numeric values differ.

Usage (standalone)::

    python inject_kern_feature.py path/to/Font.ufo [Font2.ufo ...]
    python inject_kern_feature.py --ufo-dir path/to/instance_ufos/

Called programmatically from ``build_instances.py``.
"""

from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def _build_kern_class_defs(groups: dict) -> str:
    """Return FEA class definitions for all kern1/kern2 groups."""
    kern1: Dict[str, List[str]] = {}
    kern2: Dict[str, List[str]] = {}

    for key, glyphs in groups.items():
        if key.startswith("public.kern1."):
            kern1[key] = list(glyphs)
        elif key.startswith("public.kern2."):
            kern2[key] = list(glyphs)

    lines: List[str] = []

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

    return "\n".join(lines)


def _build_kern_pair_rules(groups: dict, kerning: dict) -> str:
    """Return the pair-positioning FEA rules (no feature wrapper).

    Three buckets in priority order:
    - glyph-glyph  : format-1 exceptions
    - mixed        : glyph-class or class-glyph, emitted with ``enumerate``
    - cls_cls      : class-class format-2 pairs
    """
    kern1_keys = {k for k in groups if k.startswith("public.kern1.")}
    kern2_keys = {k for k in groups if k.startswith("public.kern2.")}

    glyph_glyph: List[Tuple[str, str, int]] = []
    mixed:       List[Tuple[str, str, int]] = []
    cls_cls:     List[Tuple[str, str, int]] = []

    for first_raw, seconds in kerning.items():
        first_is_group = first_raw in kern1_keys
        first_fea = _fea_class_name(first_raw) if first_is_group else first_raw

        for second_raw, value in seconds.items():
            ivalue = int(value)
            if ivalue == 0:
                continue  # zero-value pairs are no-ops

            second_is_group = second_raw in kern2_keys
            second_fea = _fea_class_name(second_raw) if second_is_group else second_raw

            if not first_is_group and not second_is_group:
                glyph_glyph.append((first_fea, second_fea, ivalue))
            elif first_is_group and second_is_group:
                cls_cls.append((first_fea, second_fea, ivalue))
            else:
                mixed.append((first_fea, second_fea, ivalue))

    glyph_glyph.sort()
    mixed.sort()
    cls_cls.sort()

    lines: List[str] = []

    if glyph_glyph:
        lines.append("    # Glyph-pair exceptions (PairPosFormat 1)")
        for first, second, value in glyph_glyph:
            lines.append(f"    pos {first} {second} {value};")

    if mixed:
        lines.append("    # Mixed glyph/class pairs (enumerate -> PairPosFormat 1)")
        for first, second, value in mixed:
            lines.append(f"    enumerate; pos {first} {second} {value};")

    if cls_cls:
        lines.append("    # Class-pair adjustments (PairPosFormat 2)")
        for first, second, value in cls_cls:
            lines.append(f"    pos {first} {second} {value};")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Merge logic                                                                  #
# --------------------------------------------------------------------------- #

# Sentinel comment written inside the kern feature block so we can find and
# replace our previously-injected block on a re-run.
_KERN_INJECT_BEGIN = "    # -- BEGIN injected kern pairs --"
_KERN_INJECT_END   = "    # -- END injected kern pairs --"

# Class-def header written BEFORE the feature kern block.
_CLASS_INJECT_BEGIN = "# -- BEGIN injected kern class defs --"
_CLASS_INJECT_END   = "# -- END injected kern class defs --"


def merge_kern_into_fea(
    fea_text: str,
    groups: dict,
    kerning: dict,
) -> str:
    """Return a new FEA string with kern pairs merged into the kern feature.

    Strategy
    --------
    1. If a previously-injected block (sentinel comments) is found, replace it.
    2. Otherwise inject before the closing ``} kern;``.
    3. Class definitions are placed just before ``feature kern {``.

    The existing content of ``feature kern {}`` (e.g. the optical-size
    tracking rule ``pos @All <...>``) is always preserved.
    """
    class_defs   = _build_kern_class_defs(groups)
    pair_rules   = _build_kern_pair_rules(groups, kerning)

    injected_pairs_block = (
        f"{_KERN_INJECT_BEGIN}\n"
        f"{pair_rules}\n"
        f"{_KERN_INJECT_END}"
    )

    # ------------------------------------------------------------------ #
    # A. Handle previously-injected pair block (idempotent re-run).       #
    # ------------------------------------------------------------------ #
    pair_pattern = re.compile(
        re.escape(_KERN_INJECT_BEGIN) + r".*?" + re.escape(_KERN_INJECT_END),
        re.DOTALL,
    )
    if pair_pattern.search(fea_text):
        fea_text = pair_pattern.sub(injected_pairs_block, fea_text)
    else:
        # Insert before the closing ``} kern;``
        close_pattern = re.compile(r"(\n[}] kern;)")
        if not close_pattern.search(fea_text):
            raise ValueError(
                "No 'feature kern { ... } kern;' block found in features.fea. "
                "Cannot inject kern pairs."
            )
        replacement = f"\n{injected_pairs_block}\n\\1"
        fea_text = close_pattern.sub(replacement, fea_text, count=1)

    # ------------------------------------------------------------------ #
    # B. Handle class definitions block (placed before feature kern {).   #
    # ------------------------------------------------------------------ #
    injected_class_block = (
        f"{_CLASS_INJECT_BEGIN}\n"
        f"{class_defs}"
        f"{_CLASS_INJECT_END}\n"
    )

    class_pattern = re.compile(
        re.escape(_CLASS_INJECT_BEGIN) + r".*?" + re.escape(_CLASS_INJECT_END) + r"\n",
        re.DOTALL,
    )
    if class_pattern.search(fea_text):
        fea_text = class_pattern.sub(injected_class_block, fea_text)
    else:
        # Insert just before ``feature kern {``
        feat_kern_pattern = re.compile(r"(\nfeature kern [{])")
        if feat_kern_pattern.search(fea_text):
            fea_text = feat_kern_pattern.sub(
                f"\n{injected_class_block}\\1", fea_text, count=1
            )
        else:
            # No existing kern feature at all -- append everything at the end
            fea_text = (
                fea_text.rstrip("\n")
                + f"\n\n{injected_class_block}\nfeature kern {{\n"
                + injected_pairs_block
                + "\n} kern;\n"
            )

    return fea_text


# --------------------------------------------------------------------------- #
#  Per-UFO entry point                                                          #
# --------------------------------------------------------------------------- #

def inject_kern_feature(
    ufo_path: Path,
    force: bool = False,
    out_suffix: str = ".merged",
) -> Optional[Path]:
    """Merge kern data into ``features.fea{out_suffix}`` inside *ufo_path*.

    Parameters
    ----------
    ufo_path:
        Path to the UFO directory.
    force:
        Re-inject even if the output file already exists.
    out_suffix:
        Suffix appended to ``features.fea`` for the output file.
        Default is ``.merged``, producing ``features.fea.merged``.
        Pass ``""`` to overwrite ``features.fea`` in-place.

    Returns
    -------
    Path of the written file, or ``None`` if skipped.
    """
    features_fea  = ufo_path / "features.fea"
    out_path      = ufo_path / f"features.fea{out_suffix}"
    kerning_plist = ufo_path / "kerning.plist"
    groups_plist  = ufo_path / "groups.plist"

    if not ufo_path.is_dir():
        print(f"  ERROR: not a directory: {ufo_path}")
        return None

    if not features_fea.exists():
        print(f"  WARNING: no features.fea in {ufo_path.name}, skipping")
        return None

    if not kerning_plist.exists():
        print(f"  WARNING: no kerning.plist in {ufo_path.name}, skipping")
        return None

    if out_path.exists() and not force:
        print(
            f"  SKIP: {out_path.name} already exists in {ufo_path.name} "
            "(re-run with --force to regenerate)"
        )
        return None

    fea_text = features_fea.read_text(encoding="utf-8")
    kerning  = _load_plist(kerning_plist)
    groups   = _load_plist(groups_plist) if groups_plist.exists() else {}

    if not kerning:
        print(f"  SKIP: {ufo_path.name} -- kerning.plist is empty")
        return None

    merged = merge_kern_into_fea(fea_text, groups, kerning)
    out_path.write_text(merged, encoding="utf-8")

    pair_count = merged.count("\n    pos ") + merged.count("\n    enumerate; pos ")
    print(
        f"  OK: {ufo_path.name} -- wrote {out_path.name} "
        f"({pair_count} pair rules injected)"
    )
    return out_path


# --------------------------------------------------------------------------- #
#  Multi-master consistency check                                               #
# --------------------------------------------------------------------------- #

def check_kern_structure_consistency(ufo_paths: List[Path]) -> None:
    """Warn if kern group structures differ across UFOs.

    For multi-master sources every UFO must have the same kern groups and pair
    keys (only values may differ).
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
        help="Overwrite an existing output file.",
    )
    parser.add_argument(
        "--out-suffix",
        default=".merged",
        metavar="SUFFIX",
        help=(
            "Suffix appended to 'features.fea' for the output file "
            "(default: '.merged' -> 'features.fea.merged'). "
            "Pass '' to overwrite features.fea in-place."
        ),
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
        result = inject_kern_feature(ufo, force=args.force, out_suffix=args.out_suffix)
        if result is not None:
            modified += 1

    print(f"\nDone. Wrote {modified}/{len(ufo_paths)} merged file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

