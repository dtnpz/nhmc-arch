# NHMC for Arch

An unofficial clean-room native Linux audio-effects app for the MSI GF75 Thin 10SER (`1462:12a7`). The desktop controller is Python/PySide6, the audio DSP is a compiled C++ program linked directly against `libpipewire-0.3`, and PipeWire carries audio between the virtual `nhmc_original` sink and the selected physical output. Audio playback does not launch Wine or Windows binaries.

## Start

Open **NHMC for Arch** from the KDE application menu, or run:

    ~/.local/bin/nhmc-arch

The app opens its settings window and adds a system-tray icon. Closing the window hides it to the tray; it keeps running there. Right-click the tray icon to reopen settings, choose an output or profile, or start and stop processing. With “Use for system audio while running” enabled, ordinary application streams move through the effect chain. **Stop NHMC** restores the earlier output; **Quit** exits the app.

Music, Movie, Gaming, and Communication profiles use the extracted numeric settings from the device package. The native chain includes the GF75 2,048-tap speaker-correction FIR filter, EQ bands, bass and treble shelves, voice-presence EQ, compression, stereo widening, and room reverb. It processes stereo float audio at 48 kHz.

The coefficients and settings are extracted numeric parameters only. The DSP algorithms are native Linux reimplementations (`native/nhmc_native.cpp`); no proprietary Windows binaries are executed or redistributed in this repository. HRTF surround, multichannel upmix, microphone processing, Sound Tracker, and vendor control-panel behavior are outside this version.

The laptop-speaker filter is enabled for the built-in analog speaker output. Other outputs use the profile tone controls without that speaker correction. Reopen or refresh the app after changing or disconnecting output devices.

## Build and install

Dependencies: `gcc`, `make`, `pkg-config`, `libpipewire` (`pipewire`), `python`, `python-pyside6`, `pipewire-pulse`, `7zip` (only if re-running `make prepare`). Wine is not required.

    cd /datadisk/daily/gm/nhmc-arch
    make
    make setup
    make install

- `make` compiles `build/nhmc-native` against `libpipewire-0.3`.
- `make setup` checks that the extracted profile and GF75 speaker-filter assets exist under `data/`.
- `make install` writes `~/.local/bin/nhmc-arch`, its SVG icon, and `~/.local/share/applications/nhmc-arch.desktop`.
- `make prepare` (optional) accepts a driver archive path via argument or `NHMC_ARCHIVE`, verifies its SHA-256, and writes only the XML profile values and `.f32` FIR coefficients under `data/`.

## Native DSP details

The GF75 speaker filter is a 2,048-element single-precision FIR coefficient array in `data/devices/gf75-thin-10ser-speakers.f32`. Each channel uses the same taps. The remaining controls map profile gains to native biquad EQ, a linked stereo compressor/limiter, a delay-network reverb, and mid/side stereo widening.

## Source evidence

The matched host identity is GF75 Thin 10SER, Realtek subsystem `0x146212a7`, matching `146212A7-GF75-Thin-10SER-InternalSpeakers.nsx`. Generated coefficient and profile assets remain under `data/`.
