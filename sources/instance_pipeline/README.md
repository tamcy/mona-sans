# Custom instance pipeline

This folder provides a repeatable script to generate custom UFO instances from
`sources/MonaSans.glyphspackage`.

## What it does

1. Builds master UFOs + a base designspace with `fontmake`
2. Rewrites instances in a custom designspace from your YAML config and/or CLI values
3. Interpolates instance UFOs with `fontmake -o ufo --interpolate`
4. Verifies each generated UFO contains `features.fea`

The script validates all axis values and **fails hard** on out-of-range values.

> **Why fontmake, not makeinstancesufo, for step 3?**
> `MonaSans.glyphspackage` has a 4-axis structure with a discrete `ital` axis and
> optical-size masters. `makeinstancesufo`/`ufoProcessor` raises
> `"Locations must be unique"` on such sources because it cannot split the
> interpolable sub-space. `fontmake --interpolate` uses `splitInterpolable`
> internally and handles this correctly. The generated UFOs (cubic outlines,
> self-contained `features.fea`) are exactly the format that `makeinstancesufo`
> expects as **input sources** for any downstream fork-building step.

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

## Additional Notes

The distinction between instances and presets is intentional but the naming is not obvious. Here's the difference:

|                 | instances block                                     | presets block                                           |
|-----------------|-----------------------------------------------------|---------------------------------------------------------|
| **When loaded** | Every run, unconditionally                          | Only when you pass --preset NAME                        |
| **Purpose**     | Your confirmed masters — the ones you've settled on | Experimental groups you can switch between while tuning |
| **Activation**  | Automatic                                           | --preset mood_soft                                      |

The intended workflow is:

```
Trial phase          →   --instance "Name:wdth=100,wght=150,..."  (no config edit needed)
Grouping for tests   →   presets in YAML, activated with --preset
Confirmed & stable   →   graduate to instances block (always built)
```

So in practice:

```
# Exploring: nothing in instances yet, just test a preset
python build_instances.py --preset mood_soft

# Override one axis across the whole preset without editing YAML
python build_instances.py --preset mood_soft --set wght=180

# Confirmed a value: move it to `instances:` so it always builds
# from now on without any flags
python build_instances.py
```

That said — if having both blocks feels redundant right now (since nothing is confirmed yet), you can simply leave
instances: empty and only use presets. The instances block is just a convenience for when you're past the exploration
phase and want a default set that always builds.

```
# During exploration: keep this empty
instances: []

presets:
  mood_soft:
    ...
```