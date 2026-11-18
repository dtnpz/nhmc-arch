#!/usr/bin/env python3
"""Install a user-local launcher. Does not enable audio or autostart."""
from pathlib import Path
import shlex
import shutil
import subprocess
from nhmc import ROOT

binary = Path.home() / ".local/bin/nhmc-arch"
binary.parent.mkdir(parents=True, exist_ok=True)
binary.write_text('#!/bin/sh\nexec /usr/bin/python3 '+shlex.quote(str(ROOT / 'nhmc.py'))+' "$@"\n')
binary.chmod(0o755)

icon = Path.home() / ".local/share/icons/hicolor/scalable/apps/nhmc-arch.svg"
icon.parent.mkdir(parents=True, exist_ok=True)
icon.write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">\n'
    '  <rect x="1" y="1" width="30" height="30" rx="8" ry="8" fill="#5c4bd8"/>\n'
    '  <path d="M9 23V9l14 14V9" fill="none" stroke="#ffffff" stroke-width="3" '
    'stroke-linecap="round" stroke-linejoin="round"/>\n'
    '</svg>\n'
)

desktop = Path.home() / ".local/share/applications/nhmc-arch.desktop"
desktop.parent.mkdir(parents=True, exist_ok=True)
desktop.write_text(
    '[Desktop Entry]\n'
    'Type=Application\n'
    'Version=1.0\n'
    'Name=NHMC for Arch\n'
    'GenericName=Audio Effects Controller\n'
    'Comment=Native NHMC-derived audio effects for PipeWire\n'
    f'Exec={binary} gui\n'
    f'TryExec={binary}\n'
    f'Path={ROOT}\n'
    'Icon=nhmc-arch\n'
    'Terminal=false\n'
    'StartupNotify=true\n'
    'StartupWMClass=nhmc-arch\n'
    'Categories=AudioVideo;Audio;Mixer;Settings;\n'
    'Keywords=NHMC;Audio;Equalizer;PipeWire;Sound;MSI;\n'
)

if shutil.which("update-desktop-database"):
    subprocess.run(["update-desktop-database", str(desktop.parent)], check=False)
if shutil.which("kbuildsycoca6"):
    subprocess.run(["kbuildsycoca6"], check=False)

print(binary)
print(icon)
print(desktop)
