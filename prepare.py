#!/usr/bin/env python3
"""Prepare native DSP assets from the inspected NHMC restore archive."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
EXPECTED = "409b7c2151f73cc66c0865ad715d0eb581075ef96fafabe30f2640ab77593e52"
PROFILE_NAMES = ("Music", "Movie", "Gaming", "Communication")
DEVICE_PROFILE = "146212A7-GF75-Thin-10SER-InternalSpeakers.nsx"


def put_verified(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise SystemExit(f"Refusing to overwrite a different existing asset: {path}")
    if not path.exists():
        path.write_bytes(data)


def extract_settings(archive, temp):
    temp = Path(temp)
    exe = temp / "restore.exe"
    with zipfile.ZipFile(archive) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise SystemExit("Unexpected archive layout")
        exe.write_bytes(z.read(members[0]))
    msi = temp / "restore.msi"
    msi.write_bytes(subprocess.check_output(
        ["7z", "x", "-so", "-tPE", str(exe), ".rsrc/MSI/129"]))
    payload = temp / "payload"
    subprocess.run(["7z", "x", "-y", str(msi), "-o" + str(payload)],
                   check=True, stdout=subprocess.DEVNULL)
    settings = temp / "settings"
    subprocess.run([
        "7z", "x", "-y", str(payload / "fil782E3C171DB7B89A4D6FD9948EE68901"),
        "-o" + str(settings),
    ], check=True, stdout=subprocess.DEVNULL)
    return settings


def main():
    raw_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NHMC_ARCHIVE", "")
    if not raw_path:
        raise SystemExit("Pass the driver archive path as an argument or set NHMC_ARCHIVE.")
    archive = Path(raw_path)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED:
        raise SystemExit("Archive differs from the inspected package; refusing to map unverified contents")
    with tempfile.TemporaryDirectory(prefix="nhmc-native-prepare-") as temp:
        settings = extract_settings(archive, temp)
        for name in PROFILE_NAMES:
            source = settings / "AudioProfiles" / f"{name}.nsx"
            put_verified(ROOT / "data/profiles" / source.name, source.read_bytes())
        device_path = settings / "Devices" / DEVICE_PROFILE
        device = ET.parse(device_path).getroot().find("Settings")
        for side in ("FL", "FR"):
            encoded = device.find(f"kNhSet_DeviceOptimizationFilter{side}/Value").text
            taps = base64.b64decode(encoded, validate=True)
            if len(taps) != 2048 * 4:
                raise SystemExit(f"Unexpected {side} speaker filter length: {len(taps)}")
            output = ROOT / "data/devices" / f"gf75-thin-10ser-{side.lower()}.f32"
            put_verified(output, taps)
            print(f"{output.name}: 2048 native float coefficients, SHA-256 {hashlib.sha256(taps).hexdigest()}")
    left = (ROOT / "data/devices/gf75-thin-10ser-fl.f32").read_bytes()
    right = (ROOT / "data/devices/gf75-thin-10ser-fr.f32").read_bytes()
    if left != right:
        raise SystemExit("The native convolver currently requires matching left and right device filters")
    put_verified(ROOT / "data/devices/gf75-thin-10ser-speakers.f32", left)
    print("Native profiles and the matching GF75 speaker correction are prepared.")


if __name__ == "__main__":
    main()

