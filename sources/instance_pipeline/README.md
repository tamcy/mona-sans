# Custom instance pipeline

This folder provides a repeatable script to generate custom UFO instances from
`sources/MonaSans.glyphspackage`.

## What it does

1. Builds master UFOs + a base designspace with `fontmake`
2. Rewrites instances in a custom designspace from your YAML config and/or CLI values
3. Builds instance UFOs with `makeinstancesufo` (`afdko`)
4. Verifies each generated UFO contains `features.fea`

The script validates all axis values and **fails hard** on out-of-range values.

## Files

- `build_instances.py`: main pipeline script
- `instances.sample.yaml`: sample config with presets and instance entries
- `test_build_instances.py`: lightweight parser/validation tests
- `requirements.txt`: dependencies for this pipeline

## Quick start

```powershell
python -m pip install -r sources/instance_pipeline/requirements.txt
python sources/instance_pipeline/build_instances.py --dry-run --preset mood_soft
```

## Typical runs

Use config default `instances` + a preset:

```powershell
python sources/instance_pipeline/build_instances.py --preset mood_display
```

Use ad-hoc inline instances (good for quick trials):

```powershell
python sources/instance_pipeline/build_instances.py --instance "Trial 1:wdth=100,wght=150,opsz=16,ital=0"
python sources/instance_pipeline/build_instances.py --instance "Trial 2:wdth=95,wght=220,opsz=18,ital=0"
```

Apply global override to all selected instances:

```powershell
python sources/instance_pipeline/build_instances.py --preset mood_soft --set wght=180
```

Reuse existing master build artifacts:

```powershell
python sources/instance_pipeline/build_instances.py --skip-master-build --preset mood_soft
```

## Outputs

By default, outputs are written to:

- Base designspace: `sources/build/custom_instances/MonaSans.base.designspace`
- Custom designspace: `sources/build/custom_instances/MonaSans.custom.designspace`
- Instance UFOs: `sources/build/custom_instances/instance_ufos/`

## Notes

- Axis values are treated as **user-space values** and mapped into designspace coordinates.
- If you use custom `bounds` in YAML, they can only narrow axis ranges, not expand them.
- Default script behavior disables autohinting and overlap removal for cleaner iteration.

