#!/usr/bin/env python3
"""Confirm native profile and speaker-correction assets are present."""
from prepare import PROFILE_NAMES, ROOT

profiles = ROOT / "data/profiles"
fir = ROOT / "data/devices/gf75-thin-10ser-speakers.f32"
missing = [str(profiles / f"{name}.nsx") for name in PROFILE_NAMES
           if not (profiles / f"{name}.nsx").is_file()]
if not fir.is_file():
    missing.append(str(fir))
if missing:
    raise SystemExit("Run make prepare first; missing native assets: " + ", ".join(missing))
print("Native DSP assets are ready; no Windows runtime setup is used.")

