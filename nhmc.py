#!/usr/bin/env python3
"""Native Linux tray controller for NHMC-derived PipeWire effects."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
SINK = "nhmc_original"
PROFILE_DIR = ROOT / "data/profiles"
FIR_PATH = ROOT / "data/devices/gf75-thin-10ser-speakers.f32"
EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
PROFILES = ["Music", "Movie", "Gaming", "Communication"]


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=30, **kwargs).stdout.strip()


def sinks():
    return json.loads(command(["pactl", "--format=json", "list", "sinks"]))


def defaults(profile):
    settings = ET.parse(PROFILE_DIR / f"{profile}.nsx").getroot().find("Settings")
    return {item.tag: item.attrib.get("Value") for item in settings}


def native_args(settings, profile, use_speaker_fir):
    args = [
        str(ROOT / "build/nhmc-native"),
        "--profile", profile,
        "--bass-db", str(settings["bass"]),
        "--bass-hz", str(settings.get("bass_hz", 200)),
        "--treble-db", str(settings["treble"]),
        "--clarity-db", str(settings["clarity"]),
        "--compressor", str(int(settings["compressor"])),
        "--reverb", str(int(settings["reverb"])),
        "--widen", str(int(settings["widen"])),
        "--eq", ",".join(str(x) for x in settings["eq"]),
    ]
    if use_speaker_fir and FIR_PATH.exists():
        args += ["--fir", str(FIR_PATH)]
    if not settings["enabled"]:
        args.append("--disabled")
    return args


class Bridge:
    """Owns its PipeWire virtual sink and native DSP child processes."""
    def __init__(self, name=SINK):
        self.name = name
        self.module = None
        self.children = []
        self.logs = []
        self.previous_default = None
        self.moved = []
        self.output = None

    @property
    def running(self):
        return bool(self.children) and all(p.poll() is None for p in self.children)

    def start(self, output, settings, profile, make_default=False):
        if self.children:
            raise RuntimeError("The native bridge is already running")
        current_sinks = sinks()
        available = {s["name"] for s in current_sinks}
        if output == self.name or output not in available:
            raise RuntimeError("Choose an available physical output")
        if self.name in available:
            raise RuntimeError(f"An output named {self.name} already exists")
        self.output = output
        self.previous_default = command(["pactl", "get-default-sink"])
        if self.previous_default == self.name or self.previous_default not in available:
            self.previous_default = output
        device = next(s for s in current_sinks if s["name"] == output)
        use_speaker_fir = (
            "pci-0000_00_1f.3" in output
            and device.get("active_port") == "analog-output-speaker"
            and FIR_PATH.exists()
        )
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)

        def spawn(label, args, **kwargs):
            log = (log_dir / f"{self.name}-{label}.log").open("wb")
            self.logs.append(log)
            proc = subprocess.Popen(args, cwd=ROOT, stderr=log, **kwargs)
            self.children.append(proc)
            return proc

        try:
            args = native_args(settings, profile, use_speaker_fir) + [
                "--pipewire",
                "--sink", self.name,
                "--target", output,
            ]
            spawn("dsp", args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if not self.running:
                    raise RuntimeError(f"Native audio bridge stopped. See {log_dir}")
                dsp_log = log_dir / f"{self.name}-dsp.log"
                if (
                    dsp_log.exists()
                    and b"native DSP ready" in dsp_log.read_bytes()
                    and any(s["name"] == self.name for s in sinks())
                ):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError(f"Native DSP initialization timed out. See {log_dir}")
            if make_default:
                command(["pactl", "set-default-sink", self.name])
                stream_list = json.loads(command(["pactl", "--format=json", "list", "sink-inputs"]))
                sink_names = {s["index"]: s["name"] for s in sinks()}
                for stream in stream_list:
                    props = stream.get("properties", {})
                    if (
                        props.get("application.name", "").startswith("NHMC-native")
                        or props.get("node.name") == "nhmc_playback"
                    ):
                        continue
                    current_sink = sink_names.get(stream["sink"])
                    if current_sink and current_sink != self.name:
                        move = subprocess.run(
                            ["pactl", "move-sink-input", str(stream["index"]), self.name],
                            capture_output=True, timeout=5,
                        )
                        if move.returncode == 0:
                            self.moved.append((stream["index"], current_sink))
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.children or self.module is not None:
            try:
                available = {s["name"] for s in sinks()}
                fallback = self.previous_default if self.previous_default in available else self.output
                if fallback in available:
                    if command(["pactl", "get-default-sink"]) == self.name:
                        command(["pactl", "set-default-sink", fallback])
                    sink_index = next((s["index"] for s in sinks() if s["name"] == self.name), None)
                    original_targets = dict(self.moved)
                    for stream in json.loads(command(["pactl", "--format=json", "list", "sink-inputs"])):
                        if stream["sink"] == sink_index:
                            target = original_targets.get(stream["index"], fallback)
                            subprocess.run(["pactl", "move-sink-input", str(stream["index"]), target],
                                           capture_output=True, timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
        for proc in reversed(self.children):
            if proc.poll() is None:
                proc.terminate()
        for proc in self.children:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.children.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()
        if self.module is not None:
            subprocess.run(["pactl", "unload-module", str(self.module)],
                           capture_output=True, timeout=5)
            self.module = None
        self.moved.clear()


def gui():
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QMenu,
        QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
    )

    def tray_icon():
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#5c4bd8"))
        painter.drawRoundedRect(1, 1, 30, 30, 8, 8)
        painter.setPen(QPen(QColor("#ffffff"), 3.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(9, 23, 9, 9)
        painter.drawLine(9, 9, 23, 23)
        painter.drawLine(23, 23, 23, 9)
        painter.end()
        return QIcon(pixmap)

    class MainWindow(QWidget):
        def __init__(self, controller):
            super().__init__()
            self.controller = controller
            self.setWindowTitle("NHMC for Arch")
            self.setMinimumWidth(650)
            layout = QVBoxLayout(self)
            title = QLabel("NHMC for Arch")
            title.setStyleSheet("font-size: 24px; font-weight: 600; margin: 8px 0")
            layout.addWidget(title)
            layout.addWidget(QLabel("Native Linux DSP · PipeWire"))

            form = QFormLayout()
            layout.addLayout(form)
            self.output = QComboBox()
            self.output.currentIndexChanged.connect(self.output_changed)
            form.addRow("Output device", self.output)
            self.profile = QComboBox()
            self.profile.addItems(PROFILES)
            self.profile.currentTextChanged.connect(self.profile_changed)
            form.addRow("Profile", self.profile)

            self.controls = {}
            for label, key in [("Bass (dB)", "bass"), ("Treble (dB)", "treble"),
                               ("Voice clarity (dB)", "clarity")]:
                spin = QDoubleSpinBox()
                spin.setRange(-12, 12)
                spin.setSingleStep(.5)
                form.addRow(label, spin)
                self.controls[key] = spin

            eq_group = QGroupBox("10-band equalizer (dB)")
            grid = QGridLayout(eq_group)
            self.eq_controls = []
            for i, hz in enumerate(EQ_BANDS):
                row, col = divmod(i, 5)
                spin = QDoubleSpinBox()
                spin.setRange(-12, 12)
                spin.setSingleStep(.5)
                grid.addWidget(QLabel(f"{hz} Hz"), row * 2, col)
                grid.addWidget(spin, row * 2 + 1, col)
                self.eq_controls.append(spin)
            layout.addWidget(eq_group)

            self.enabled = QCheckBox("Enable effects")
            self.reverb = QCheckBox("Room reverb")
            self.widen = QCheckBox("Stereo widening")
            self.compressor = QCheckBox("Dynamic compression")
            self.use_default = QCheckBox("Use for system audio while running")
            for check in [self.enabled, self.reverb, self.widen,
                          self.compressor, self.use_default]:
                layout.addWidget(check)
            self.enabled.toggled.connect(lambda v: self.set_setting("enabled", v))
            self.reverb.toggled.connect(lambda v: self.set_setting("reverb", v))
            self.widen.toggled.connect(lambda v: self.set_setting("widen", v))
            self.compressor.toggled.connect(lambda v: self.set_setting("compressor", v))
            self.use_default.toggled.connect(self.set_use_default)
            for key, spin in self.controls.items():
                spin.valueChanged.connect(lambda value, k=key: self.set_setting(k, value))
            for index, spin in enumerate(self.eq_controls):
                spin.valueChanged.connect(lambda value, i=index: self.set_eq(i, value))

            self.status = QLabel("Stopped · audio routing is unchanged")
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            buttons = QHBoxLayout()
            layout.addLayout(buttons)
            self.start_button = QPushButton("Start")
            self.apply_button = QPushButton("Apply settings")
            self.stop_button = QPushButton("Stop")
            for button in [self.start_button, self.apply_button, self.stop_button]:
                buttons.addWidget(button)
            self.start_button.clicked.connect(controller.start)
            self.apply_button.clicked.connect(controller.apply)
            self.stop_button.clicked.connect(controller.stop)
            self.load_values()

        def load_values(self):
            self.profile.blockSignals(True)
            self.profile.setCurrentText(self.controller.profile)
            self.profile.blockSignals(False)
            self.output.blockSignals(True)
            self.output.clear()
            for sink in sinks():
                if sink["name"] != SINK:
                    self.output.addItem(sink.get("description", sink["name"]), sink["name"])
            index = self.output.findData(self.controller.output)
            if index >= 0:
                self.output.setCurrentIndex(index)
            self.output.blockSignals(False)
            for key, spin in self.controls.items():
                spin.blockSignals(True)
                spin.setValue(float(self.controller.values[key]))
                spin.blockSignals(False)
            for i, spin in enumerate(self.eq_controls):
                spin.blockSignals(True)
                spin.setValue(float(self.controller.values["eq"][i]))
                spin.blockSignals(False)
            for checkbox, key in [
                (self.enabled, "enabled"), (self.reverb, "reverb"),
                (self.widen, "widen"), (self.compressor, "compressor"),
            ]:
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(self.controller.values[key]))
                checkbox.blockSignals(False)
            self.use_default.blockSignals(True)
            self.use_default.setChecked(self.controller._use_default)
            self.use_default.blockSignals(False)
            self.update_status(self.controller.status)

        def set_setting(self, key, value):
            self.controller.values[key] = value

        def set_eq(self, index, value):
            self.controller.values["eq"][index] = float(value)

        def set_use_default(self, value):
            self.controller._use_default = bool(value)

        def output_changed(self):
            output = self.output.currentData()
            if output is not None and output != self.controller.output:
                self.controller.output = output
                if self.controller.bridge.running:
                    QTimer.singleShot(0, self.controller.apply)

        def profile_changed(self, profile):
            if profile == self.controller.profile:
                return
            self.controller.profile = profile
            self.controller.values = self.controller.profile_defaults(profile)
            self.load_values()
            QTimer.singleShot(0, self.controller.build_menu)
            if self.controller.bridge.running:
                QTimer.singleShot(0, self.controller.apply)

        def update_status(self, text):
            self.status.setText(text)
            running = self.controller.bridge.running
            self.start_button.setEnabled(not running)
            self.stop_button.setEnabled(running)

        def closeEvent(self, event):
            if self.controller.allow_close:
                event.accept()
                return
            if not self.controller.tray.isSystemTrayAvailable():
                self.controller.quit()
                event.accept()
                return
            event.ignore()
            self.hide()
            self.controller.tray.showMessage(
                "NHMC for Arch", "The app is still running in the system tray.")

    class TrayController:
        def __init__(self, app):
            self.app = app
            self.bridge = Bridge()
            self.profile = "Music"
            self.output = self.default_output()
            self.values = self.profile_defaults(self.profile)
            self._use_default = True
            self.status = "Stopped"
            self.allow_close = False
            self.window = MainWindow(self)
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
            self.menu = QMenu()
            self.tray = QSystemTrayIcon(tray_icon(), app)
            self.tray.setToolTip("NHMC for Arch — Stopped")
            self.tray.setContextMenu(self.menu)
            self.build_menu()
            self.tray.activated.connect(self.activated)
            QTimer.singleShot(0, self.tray.show)
            self.timer = QTimer(app)
            self.timer.timeout.connect(self.check_bridge)
            self.timer.start(2000)
            app.aboutToQuit.connect(self.shutdown)

        def profile_defaults(self, profile):
            values = defaults(profile)
            return {
                "enabled": True,
                "bass": float(values.get("kNhSet_BassBoostGainDB") or 0)
                    if values.get("kNhSet_BassBoostState", "1") == "1" else 0,
                "bass_hz": 200,
                "treble": float(values.get("kNhSet_TrebleBoostGainDB") or 0)
                    if values.get("kNhSet_TrebleBoostState", "1") == "1" else 0,
                "clarity": float(values.get("kNhSet_ClarityVoiceGainDB") or 0)
                    if values.get("kNhSet_ClarityState", "1") == "1" else 0,
                "compressor": values.get("kNhSet_CompressorState") == "1",
                "reverb": values.get("kNhSet_ReverbState") == "1",
                "widen": values.get("kNhSet_StereoWideningState") == "1",
                "eq": [0.0] * 10,
            }

        def action(self, menu, text, callback, checkable=False):
            item = QAction(text, menu)
            item.setCheckable(checkable)
            menu.addAction(item)
            if checkable:
                item.toggled.connect(callback)
            else:
                item.triggered.connect(callback)
            return item

        def build_menu(self):
            self.menu.clear()
            self.state_action = self.action(self.menu, "Stopped", lambda: None)
            self.state_action.setEnabled(False)
            self.action(self.menu, "Open NHMC", self.show_window)
            self.run_action = self.action(self.menu, "Start NHMC", self.toggle_running)
            self.menu.addSeparator()

            output_menu = self.menu.addMenu("Output device")
            self.output_group = QActionGroup(output_menu)
            self.output_group.setExclusive(True)
            for sink in sinks():
                if sink["name"] == SINK:
                    continue
                item = QAction(sink.get("description", sink["name"]), output_menu)
                item.setCheckable(True)
                item.setChecked(sink["name"] == self.output)
                item.setData(sink["name"])
                self.output_group.addAction(item)
                output_menu.addAction(item)
            self.output_group.triggered.connect(self.choose_output)
            output_menu.addSeparator()
            self.action(output_menu, "Refresh output devices", self.refresh)

            profile_menu = self.menu.addMenu("Profile")
            self.profile_group = QActionGroup(profile_menu)
            self.profile_group.setExclusive(True)
            for name in PROFILES:
                item = QAction(name, profile_menu)
                item.setCheckable(True)
                item.setChecked(name == self.profile)
                item.setData(name)
                self.profile_group.addAction(item)
                profile_menu.addAction(item)
            self.profile_group.triggered.connect(self.choose_profile)

            self.menu.addSeparator()
            self.action(self.menu, "Enable effects", self.set_enabled, checkable=True).setChecked(
                self.values["enabled"])
            self.action(self.menu, "Use for system audio while running",
                        self.set_system_audio, checkable=True).setChecked(self._use_default)

            for label, key in [("Bass", "bass"), ("Treble", "treble"),
                               ("Voice clarity", "clarity")]:
                submenu = self.menu.addMenu(label)
                self.add_gain_actions(submenu, key, range(-12, 13, 2))
            eq_menu = self.menu.addMenu("10-band equalizer")
            for index, hz in enumerate(EQ_BANDS):
                submenu = eq_menu.addMenu(f"{hz} Hz")
                self.add_gain_actions(submenu, f"eq{index}", [-12, -6, -3, 0, 3, 6, 12])

            self.action(self.menu, "Room reverb", self.set_reverb, checkable=True).setChecked(
                self.values["reverb"])
            self.action(self.menu, "Stereo widening", self.set_widen, checkable=True).setChecked(
                self.values["widen"])
            self.action(self.menu, "Dynamic compression",
                        self.set_compressor, checkable=True).setChecked(self.values["compressor"])
            self.menu.addSeparator()
            self.action(self.menu, "Apply settings", self.apply)
            self.action(self.menu, "Stop NHMC", self.stop)
            self.menu.addSeparator()
            self.action(self.menu, "Quit", self.quit)

        def add_gain_actions(self, submenu, key, choices):
            group = QActionGroup(submenu)
            group.setExclusive(True)
            current = self.values["eq"][int(key[2:])] if key.startswith("eq") else self.values[key]
            for value in choices:
                label = f"{value:+g} dB" if value else "0 dB"
                item = QAction(label, submenu)
                item.setCheckable(True)
                item.setChecked(abs(float(current) - value) < 0.01)
                item.setData(value)
                group.addAction(item)
                submenu.addAction(item)
            group.triggered.connect(lambda item, k=key: self.set_gain(k, item.data()))

        def activated(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                self.toggle_running()

        def choose_output(self, item):
            self.output = item.data()
            self.window.load_values()
            if self.bridge.running:
                QTimer.singleShot(0, self.apply)

        def choose_profile(self, item):
            self.profile = item.data()
            self.values = self.profile_defaults(self.profile)
            QTimer.singleShot(0, self.build_menu)
            if self.bridge.running:
                QTimer.singleShot(0, self.apply)
            self.window.load_values()

        def default_output(self):
            physical = [s["name"] for s in sinks() if s["name"] != SINK]
            current = command(["pactl", "get-default-sink"])
            if current in physical:
                return current
            preferred = "alsa_output.pci-0000_00_1f.3.analog-stereo"
            if preferred in physical:
                return preferred
            return physical[0] if physical else current

        def refresh(self):
            available = {s["name"] for s in sinks() if s["name"] != SINK}
            if self.output not in available:
                self.output = self.default_output()
            QTimer.singleShot(0, self.build_menu)
            self.window.load_values()

        def set_enabled(self, state):
            self.values["enabled"] = bool(state)
            self.window.load_values()

        def set_system_audio(self, state):
            self._use_default = bool(state)
            self.window.load_values()

        def set_gain(self, key, value):
            if key.startswith("eq"):
                self.values["eq"][int(key[2:])] = float(value)
            else:
                self.values[key] = float(value)
            self.window.load_values()
            QTimer.singleShot(0, self.build_menu)

        def set_reverb(self, state):
            self.values["reverb"] = bool(state)
            self.window.load_values()

        def set_widen(self, state):
            self.values["widen"] = bool(state)
            self.window.load_values()

        def set_compressor(self, state):
            self.values["compressor"] = bool(state)
            self.window.load_values()

        def toggle_running(self):
            if self.bridge.running:
                self.stop()
            else:
                self.start()

        def start(self):
            try:
                self.bridge.start(self.output, self.values, self.profile, self._use_default)
                self.update_status("Running")
                self.tray.showMessage("NHMC for Arch", "Native audio effects are running.")
            except Exception as error:
                self.update_status("Stopped")
                self.tray.showMessage("NHMC for Arch", str(error),
                                      QSystemTrayIcon.MessageIcon.Warning, 8000)

        def apply(self):
            running = self.bridge.running
            self.bridge.stop()
            if running:
                self.start()
            else:
                self.update_status("Stopped · settings saved")

        def stop(self):
            self.bridge.stop()
            self.update_status("Stopped")

        def update_status(self, text):
            self.status = text
            self.state_action.setText(text)
            self.tray.setToolTip(f"NHMC for Arch — {text}")
            self.run_action.setText("Stop NHMC" if self.bridge.running else "Start NHMC")
            self.window.update_status(text)

        def show_window(self):
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

        def check_bridge(self):
            if self.bridge.children and not self.bridge.running:
                self.bridge.stop()
                self.update_status("Stopped · processor exited")
            available = {s["name"] for s in sinks() if s["name"] != SINK}
            if self.output not in available:
                self.output = self.default_output()
                QTimer.singleShot(0, self.build_menu)
                self.window.load_values()

        def quit(self):
            self.bridge.stop()
            self.allow_close = True
            self.tray.hide()
            self.window.close()
            self.app.quit()

        def shutdown(self):
            self.bridge.stop()
            self.tray.hide()

    app = QApplication(sys.argv)
    app.setApplicationName("NHMC for Arch")
    app.setDesktopFileName("nhmc-arch")
    app.setWindowIcon(tray_icon())
    app.setQuitOnLastWindowClosed(False)

    server_name = "nhmc-arch-gui"
    probe = QLocalSocket()
    probe.connectToServer(server_name)
    if probe.waitForConnected(300):
        probe.write(b"show\n")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0

    QLocalServer.removeServer(server_name)
    server = QLocalServer(app)
    server.listen(server_name)
    app.aboutToQuit.connect(server.close)

    try:
        controller = TrayController(app)
    except Exception as error:
        print(f"NHMC for Arch: {error}", file=sys.stderr)
        return 1

    def handle_new_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                continue
            conn.waitForReadyRead(100)
            msg = bytes(conn.readAll()).decode("utf-8", errors="ignore").strip()
            conn.disconnectFromServer()
            conn.deleteLater()
            if msg == "apply":
                controller.apply()
            elif msg == "start":
                if not controller.bridge.running:
                    controller.start()
            else:
                controller.show_window()

    server.newConnection.connect(handle_new_connection)
    return app.exec()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="gui", choices=["gui", "run"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "gui":
        return gui()
    bridge = Bridge()
    output = args.arguments[0] if args.arguments else command(["pactl", "get-default-sink"])
    settings = {"enabled": True, "bass": 6, "bass_hz": 200, "treble": 4, "clarity": 4,
                "compressor": False, "reverb": False, "widen": False, "eq": [0] * 10}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    try:
        bridge.start(output, settings, "Music")
        print(f"Native NHMC DSP ready: {output}", flush=True)
        while bridge.running:
            time.sleep(.5)
        return 1
    finally:
        bridge.stop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr or str(error))

