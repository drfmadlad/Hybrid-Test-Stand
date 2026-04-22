# GUIv1.py
# Test Stand UI — live telemetry from unified Teensy firmware + test profile editor.
# PySide6 + QtCharts + pyserial.

from __future__ import annotations

import csv
import math
import queue
import sys
import threading
import time
from typing import List, Tuple

import serial
import serial.tools.list_ports

from PySide6.QtCore import Qt, QSize, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QStackedWidget, QGroupBox, QFrame, QTableWidget, QTableWidgetItem,
    QPushButton, QRadioButton, QButtonGroup, QCheckBox, QLabel,
    QFileDialog, QMessageBox, QSizePolicy, QComboBox, QSlider, QSpinBox,
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis


# ── helpers ──────────────────────────────────────────────────────────────────

class HLine(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)


# ── Gauge Widget ─────────────────────────────────────────────────────────────

class CircleGauge(QWidget):
    """Circular gauge that shows a live numeric value."""

    def __init__(self, title: str, unit: str = "", decimals: int = 1,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._decimals = decimals
        self._value: float | None = None
        self.setMinimumSize(QSize(120, 120))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_value(self, value: float | None) -> None:
        self._value = value
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        size = min(self.width(), self.height()) - 12
        cx, cy = self.width() // 2, self.height() // 2 + 6
        r = max(40, size // 2)

        # circle
        p.setPen(self.palette().mid().color())
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)

        # title (top)
        p.setPen(self.palette().windowText().color())
        p.drawText(0, 2, self.width(), 22, Qt.AlignHCenter | Qt.AlignVCenter,
                   self._title)

        # value (center)
        if self._value is not None and not math.isnan(self._value):
            txt = f"{self._value:.{self._decimals}f}"
            if self._unit:
                txt += f" {self._unit}"
        else:
            txt = "\u2014"   # em dash

        vf = QFont(p.font())
        vf.setPointSize(vf.pointSize() + 2)
        vf.setBold(True)
        p.setFont(vf)
        p.drawText(0, cy - 10, self.width(), 20, Qt.AlignCenter, txt)


# ── Serial Worker ────────────────────────────────────────────────────────────

class SerialWorker(QObject):
    """Runs serial I/O on a background thread, emits parsed telemetry."""

    telemetry = Signal(dict)
    connection_changed = Signal(bool, str)   # (connected, message)

    def __init__(self) -> None:
        super().__init__()
        self._ser: serial.Serial | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._cmd_q: queue.Queue[str] = queue.Queue()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect_port(self, port: str, baud: int = 2_000_000) -> None:
        self.disconnect_port()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(port, baud), daemon=True)
        self._thread.start()

    def disconnect_port(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def send(self, cmd: str) -> None:
        self._cmd_q.put(cmd)

    # ── background thread ────────────────────────────────────────────────────

    def _loop(self, port: str, baud: int) -> None:
        try:
            self._ser = serial.Serial(port, baud, timeout=0.05)
            self.connection_changed.emit(True, f"Connected to {port}")
        except Exception as exc:
            self.connection_changed.emit(False, str(exc))
            return

        while self._running:
            # flush command queue
            while not self._cmd_q.empty():
                try:
                    cmd = self._cmd_q.get_nowait()
                    self._ser.write(f"{cmd}\n".encode("ascii"))
                except Exception:
                    pass

            # read one line
            try:
                raw = self._ser.readline()
                if raw:
                    line = raw.decode("ascii", errors="replace").strip()
                    if line.startswith("$T,"):
                        self._parse_telemetry(line)
            except Exception:
                if self._running:
                    self.connection_changed.emit(False, "Connection lost")
                break

        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _parse_telemetry(self, line: str) -> None:
        parts = line.split(",")
        if len(parts) < 12:
            return
        try:
            def pf(s: str) -> float:
                return float("nan") if s.strip() == "nan" else float(s)

            self.telemetry.emit({
                "ms":        int(parts[1]),
                "rpm":       pf(parts[2]),
                "coolant":   pf(parts[3]),
                "batt_v":    pf(parts[4]),
                "fuel_pct":  pf(parts[5]),
                "lambda":    pf(parts[6]),
                "bmp_temp":  pf(parts[7]),
                "bmp_press": pf(parts[8]),
                "bmp_alt":   pf(parts[9]),
                "flow":      pf(parts[10]),
                "servo_pct": pf(parts[11]),
            })
        except (ValueError, IndexError):
            pass


# ── Set Test Profile Page ────────────────────────────────────────────────────

class SetProfilePage(QWidget):
    backRequested = Signal()
    profileChanged = Signal(list, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel("Set Test Profile")
        f = title.font(); f.setBold(True); f.setPointSize(f.pointSize() + 2)
        title.setFont(f)
        root.addWidget(title)
        root.addWidget(HLine())

        content = QHBoxLayout(); content.setSpacing(12)
        root.addLayout(content, 1)

        # Left: table + buttons
        left_box = QGroupBox("Test Profile Table")
        left = QVBoxLayout(left_box); left.setContentsMargins(8, 8, 8, 8)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Time", "Target"])
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumWidth(280)
        self.table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.SelectedClicked |
            QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        left.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_add = QPushButton("Add Row")
        self.btn_remove = QPushButton("Remove Row")
        btns.addWidget(self.btn_add); btns.addWidget(self.btn_remove)
        left.addLayout(btns)
        content.addWidget(left_box, 1)

        # Center: chart
        chart_box = QGroupBox("Profile")
        chart_layout = QVBoxLayout(chart_box); chart_layout.setContentsMargins(8, 8, 8, 8)

        self.chart = QChart(); self.chart.legend().hide(); self.chart.setTitle("Test Profile")
        self.axis_x = QValueAxis(); self.axis_x.setTitleText("Time"); self.axis_x.setLabelFormat("%.0f")
        self.axis_y = QValueAxis(); self.axis_y.setTitleText("Target"); self.axis_y.setLabelFormat("%.0f")
        self.chart.addAxis(self.axis_x, Qt.AlignBottom); self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.series = QLineSeries(); self.chart.addSeries(self.series)
        self.series.attachAxis(self.axis_x); self.series.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart); self.chart_view.setRenderHint(QPainter.Antialiasing)
        chart_layout.addWidget(self.chart_view)
        content.addWidget(chart_box, 2)

        # Right: target selector
        right_box = QGroupBox("Target")
        right = QVBoxLayout(right_box); right.setContentsMargins(8, 8, 8, 8)
        self.rb_load = QRadioButton("% Load")
        self.rb_rpm = QRadioButton("RPM")
        self.rb_throttle = QRadioButton("% Throttle")
        self.rb_load.setChecked(True)
        self.target_group = QButtonGroup(self)
        for rb in (self.rb_load, self.rb_rpm, self.rb_throttle):
            self.target_group.addButton(rb); right.addWidget(rb)
        right.addStretch(1)
        content.addWidget(right_box, 0)

        root.addWidget(HLine())

        # Bottom: file ops + back
        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self.btn_load_csv = QPushButton("Load Profile CSV")
        self.btn_save_csv = QPushButton("Save Profile CSV")
        self.btn_back = QPushButton("Back")
        self.btn_back.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        bottom.addWidget(self.btn_load_csv); bottom.addWidget(self.btn_save_csv)
        bottom.addStretch(1); bottom.addWidget(self.btn_back)
        root.addLayout(bottom)

        # Seed rows
        for t, v in [(0, 20), (1, 90), (3, 50), (4, 20), (5, 0)]:
            self._append_row(t, v)

        # Signals
        self.btn_add.clicked.connect(self._on_add_row)
        self.btn_remove.clicked.connect(self._on_remove_row)
        self.table.itemChanged.connect(self._on_table_changed)
        self.btn_load_csv.clicked.connect(self._on_load_csv)
        self.btn_save_csv.clicked.connect(self._on_save_csv)
        self.btn_back.clicked.connect(self.backRequested.emit)
        for rb in (self.rb_load, self.rb_rpm, self.rb_throttle):
            rb.toggled.connect(lambda _=None: self._emit_profile_changed())

        self._update_chart_from_table()
        self._emit_profile_changed()

    def emit_current_profile(self) -> None:
        self._emit_profile_changed()

    # table ops
    def _append_row(self, t: float, v: float) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(t)))
        self.table.setItem(r, 1, QTableWidgetItem(str(v)))

    def _on_add_row(self) -> None:
        data = self._read_table(silent=True)
        if data:
            t, v = data[-1]
            self._append_row(t + 1, v)
        else:
            self._append_row(0, 0)
        self._update_chart_from_table()
        self._emit_profile_changed()

    def _on_remove_row(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount() > 0:
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self._update_chart_from_table()
        self._emit_profile_changed()

    def _on_table_changed(self, _item: QTableWidgetItem) -> None:
        for r in range(self.table.rowCount()):
            for c in (0, 1):
                it = self.table.item(r, c)
                if not it:
                    continue
                ok = self._is_float(it.text())
                it.setBackground(Qt.transparent if ok else Qt.red)
        self._update_chart_from_table()
        self._emit_profile_changed()

    # csv io
    def _on_load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Profile CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        try:
            rows: List[Tuple[float, float]] = []
            with open(path, "r", newline="") as f:
                for r in csv.reader(f):
                    if len(r) >= 2:
                        try:
                            rows.append((float(r[0]), float(r[1])))
                        except ValueError:
                            continue
            if not rows:
                QMessageBox.information(self, "No Data",
                                        "Expected two numeric columns: time,target")
                return
            self.table.setRowCount(0)
            for t, v in rows:
                self._append_row(t, v)
            self._update_chart_from_table()
            self._emit_profile_changed()
        except Exception as e:
            QMessageBox.warning(self, "Load Failed", f"Could not read file:\n{e}")

    def _on_save_csv(self) -> None:
        data = self._read_table(silent=False)
        if not data:
            QMessageBox.information(self, "Nothing to Save",
                                    "Table is empty or invalid.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Profile CSV", "profile.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "target"])
                for t, v in data:
                    w.writerow([t, v])
            QMessageBox.information(self, "Saved", f"Profile saved to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not save file:\n{e}")

    # chart sync
    def _read_table(self, *, silent: bool) -> List[Tuple[float, float]]:
        data: List[Tuple[float, float]] = []
        bad = False
        for r in range(self.table.rowCount()):
            it_t, it_v = self.table.item(r, 0), self.table.item(r, 1)
            if not it_t or not it_v:
                continue
            ts, vs = it_t.text().strip(), it_v.text().strip()
            if self._is_float(ts) and self._is_float(vs):
                data.append((float(ts), float(vs)))
            else:
                bad = True
        data.sort(key=lambda tv: tv[0])
        if bad and not silent:
            QMessageBox.warning(self, "Invalid Rows",
                                "Some rows were skipped due to non-numeric values.")
        return data

    def _update_chart_from_table(self) -> None:
        data = self._read_table(silent=True)
        pts: List[Tuple[float, float]] = []
        if data:
            t0, v0 = data[0]
            pts.append((t0, v0))
            for (t_prev, v_prev), (t_next, v_next) in zip(data[:-1], data[1:]):
                pts.append((t_next, v_prev))
                pts.append((t_next, v_next))

        self.series.clear()
        for x, y in pts:
            self.series.append(x, y)

        if data:
            ts = [t for t, _ in data]; vs = [v for _, v in data]
            tmin, tmax = min(ts), max(ts)
            vmin, vmax = min(vs), max(vs)
            if tmin == tmax: tmin -= 1; tmax += 1
            if vmin == vmax: vmin -= 1; vmax += 1
        else:
            tmin, tmax, vmin, vmax = 0.0, 5.0, 0.0, 100.0
        pad_x = 0.05 * (tmax - tmin); pad_y = 0.10 * (vmax - vmin)
        self.axis_x.setRange(tmin - pad_x, tmax + pad_x)
        self.axis_y.setRange(vmin - pad_y, vmax + pad_y)

    def _emit_profile_changed(self) -> None:
        data = self._read_table(silent=True)
        target = ("% Load" if self.rb_load.isChecked()
                  else ("RPM" if self.rb_rpm.isChecked() else "% Throttle"))
        self.profileChanged.emit(data, target)

    @staticmethod
    def _is_float(s: str) -> bool:
        try:
            float(s); return True
        except Exception:
            return False


# ── Upper (main) Page ────────────────────────────────────────────────────────

class UpperPage(QWidget):
    """Main page: connection, live gauges, profile plot, throttle control."""

    throttle_command = Signal(str)   # emits serial command strings

    def __init__(self, on_open_set_profile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ── Top bar: mode + set-profile ──────────────────────────────────────
        top = QHBoxLayout(); top.setSpacing(8)
        self.rb_manual = QRadioButton("Manual")
        self.rb_auto = QRadioButton("Auto")
        self.rb_manual.setChecked(True)
        self.btn_set_profile = QPushButton("Set Test Profile")
        self.btn_set_profile.setMinimumHeight(36)
        self.btn_set_profile.clicked.connect(on_open_set_profile)
        top.addWidget(self.rb_manual); top.addWidget(self.rb_auto)
        top.addSpacing(16); top.addWidget(self.btn_set_profile); top.addStretch(1)
        root.addLayout(top)

        # ── Connection bar ───────────────────────────────────────────────────
        conn = QHBoxLayout(); conn.setSpacing(6)
        conn.addWidget(QLabel("Port:"))
        self.cb_port = QComboBox(); self.cb_port.setMinimumWidth(120)
        conn.addWidget(self.cb_port)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_ports)
        conn.addWidget(self.btn_refresh)
        conn.addWidget(QLabel("Baud:"))
        self.cb_baud = QComboBox()
        self.cb_baud.addItems(["2000000", "1000000", "500000", "250000", "115200"])
        conn.addWidget(self.cb_baud)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setMinimumHeight(36)
        self.btn_connect.setCheckable(True)
        conn.addWidget(self.btn_connect)
        self.lbl_status = QLabel("Disconnected")
        conn.addWidget(self.lbl_status)
        conn.addStretch(1)
        root.addLayout(conn)
        root.addWidget(HLine())

        self._refresh_ports()

        # ── Middle: plot + gauges ────────────────────────────────────────────
        mid = QHBoxLayout(); mid.setSpacing(12)

        # Live chart
        plot_box = QGroupBox("")
        plot_layout = QVBoxLayout(plot_box); plot_layout.setContentsMargins(8, 8, 8, 8)
        self.chart = QChart(); self.chart.legend().hide()
        self.chart.setTitle("Test Profile")
        self.axis_x = QValueAxis(); self.axis_x.setTitleText("Time")
        self.axis_x.setLabelFormat("%.0f")
        self.axis_y = QValueAxis(); self.axis_y.setTitleText("Target")
        self.axis_y.setLabelFormat("%.0f")
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.series = QLineSeries(); self.chart.addSeries(self.series)
        self.series.attachAxis(self.axis_x); self.series.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        plot_layout.addWidget(self.chart_view)
        mid.addWidget(plot_box, 2)

        # Gauge grid
        gauges_box = QGroupBox("")
        g = QGridLayout(gauges_box)
        g.setContentsMargins(8, 8, 8, 8)
        g.setHorizontalSpacing(10); g.setVerticalSpacing(10)

        # (title, unit, decimals)
        gauge_defs = [
            ("RPM",      "",     0),
            ("AFR",      "",     1),
            ("Throttle", "%",    1),
            ("Battery",  "V",    2),
            ("Coolant",  "\u00b0C", 0),
            ("Fuel",     "%",    0),
            ("Flow",     "ml/m", 1),
            ("Amb Temp", "\u00b0C", 1),
            ("Pressure", "hPa",  1),
        ]
        self.gauges: dict[str, CircleGauge] = {}
        for idx, (title, unit, dec) in enumerate(gauge_defs):
            row, col = divmod(idx, 3)
            gauge = CircleGauge(title, unit, dec)
            g.addWidget(gauge, row, col)
            self.gauges[title] = gauge

        mid.addWidget(gauges_box, 1)
        root.addLayout(mid, 1)
        root.addWidget(HLine())

        # ── Bottom bar: logging, throttle, run/stop ──────────────────────────
        bottom = QHBoxLayout(); bottom.setSpacing(8)

        self.chk_logging = QCheckBox("Data Logging")
        bottom.addWidget(self.chk_logging)

        bottom.addSpacing(16)
        bottom.addWidget(QLabel("Throttle:"))
        self.throttle_slider = QSlider(Qt.Horizontal)
        self.throttle_slider.setRange(0, 100)
        self.throttle_slider.setValue(0)
        self.throttle_slider.setMinimumWidth(160)
        bottom.addWidget(self.throttle_slider)
        self.throttle_spin = QSpinBox()
        self.throttle_spin.setRange(0, 100)
        self.throttle_spin.setSuffix(" %")
        bottom.addWidget(self.throttle_spin)

        # keep slider and spin synced
        self.throttle_slider.valueChanged.connect(self.throttle_spin.setValue)
        self.throttle_spin.valueChanged.connect(self.throttle_slider.setValue)
        self.throttle_slider.valueChanged.connect(self._on_throttle_changed)

        bottom.addStretch(1)

        self.btn_run = QPushButton("Run Test Profile")
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #c62828; color: white; font-weight: bold; }"
            "QPushButton:pressed { background: #8e0000; }"
        )
        for b in (self.btn_run, self.btn_stop):
            b.setMinimumHeight(36)
        bottom.addWidget(self.btn_run); bottom.addWidget(self.btn_stop)
        root.addLayout(bottom)

    # ── port helpers ─────────────────────────────────────────────────────────

    def _refresh_ports(self) -> None:
        self.cb_port.clear()
        ports = serial.tools.list_ports.comports()
        for p in sorted(ports, key=lambda x: x.device):
            self.cb_port.addItem(p.device, p.description)
        if self.cb_port.count() == 0:
            self.cb_port.addItem("(none)")

    def selected_port(self) -> str:
        return self.cb_port.currentText()

    def selected_baud(self) -> int:
        return int(self.cb_baud.currentText())

    # ── throttle ─────────────────────────────────────────────────────────────

    def _on_throttle_changed(self, value: int) -> None:
        self.throttle_command.emit(f"t{value}")

    # ── telemetry update ─────────────────────────────────────────────────────

    def update_telemetry(self, data: dict) -> None:
        g = self.gauges

        rpm = data.get("rpm")
        g["RPM"].set_value(rpm)

        lam = data.get("lambda")
        if lam is not None and not math.isnan(lam):
            g["AFR"].set_value(lam * 14.7)
        else:
            g["AFR"].set_value(None)

        g["Throttle"].set_value(data.get("servo_pct"))
        g["Battery"].set_value(data.get("batt_v"))
        g["Coolant"].set_value(data.get("coolant"))
        g["Fuel"].set_value(data.get("fuel_pct"))
        g["Flow"].set_value(data.get("flow"))
        g["Amb Temp"].set_value(data.get("bmp_temp"))
        g["Pressure"].set_value(data.get("bmp_press"))

        # update throttle slider from firmware (only if user isn't dragging)
        servo = data.get("servo_pct")
        if servo is not None and not math.isnan(servo):
            if not self.throttle_slider.isSliderDown():
                self.throttle_slider.blockSignals(True)
                self.throttle_slider.setValue(int(round(servo)))
                self.throttle_slider.blockSignals(False)
                self.throttle_spin.blockSignals(True)
                self.throttle_spin.setValue(int(round(servo)))
                self.throttle_spin.blockSignals(False)

    # ── profile update (from SetProfilePage) ─────────────────────────────────

    def update_profile(self, data: List[Tuple[float, float]],
                       target_label: str) -> None:
        pts: List[Tuple[float, float]] = []
        if data:
            t0, v0 = data[0]
            pts.append((t0, v0))
            for (t_prev, v_prev), (t_next, v_next) in zip(data[:-1], data[1:]):
                pts.append((t_next, v_prev))
                pts.append((t_next, v_next))

        self.series.clear()
        for x, y in pts:
            self.series.append(x, y)

        self.axis_y.setTitleText(target_label if target_label else "Target")
        if data:
            ts = [t for t, _ in data]; vs = [v for _, v in data]
            tmin, tmax = min(ts), max(ts)
            vmin, vmax = min(vs), max(vs)
            if tmin == tmax: tmin -= 1; tmax += 1
            if vmin == vmax: vmin -= 1; vmax += 1
        else:
            tmin, tmax, vmin, vmax = 0.0, 5.0, 0.0, 100.0
        pad_x = 0.05 * (tmax - tmin); pad_y = 0.10 * (vmax - vmin)
        self.axis_x.setRange(tmin - pad_x, tmax + pad_x)
        self.axis_y.setRange(vmin - pad_y, vmax + pad_y)


# ── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Test Stand UI")
        self.resize(1280, 780)

        self.pages = QStackedWidget(self)
        self.setCentralWidget(self.pages)

        # Page 0: main
        self.upper = UpperPage(self._open_set_profile)
        self.pages.addWidget(self.upper)

        # Page 1: profile editor
        self.set_profile = SetProfilePage()
        self.set_profile.backRequested.connect(self._open_upper)
        self.set_profile.profileChanged.connect(self.upper.update_profile)
        self.pages.addWidget(self.set_profile)

        self.statusBar().showMessage("Ready")

        # Push initial profile
        self.set_profile.emit_current_profile()

        # ── Serial worker ────────────────────────────────────────────────────
        self.serial_worker = SerialWorker()
        self.serial_worker.telemetry.connect(self.upper.update_telemetry)
        self.serial_worker.connection_changed.connect(self._on_connection_changed)

        # Connect/disconnect button
        self.upper.btn_connect.clicked.connect(self._on_connect_toggle)

        # Throttle command -> serial
        self.upper.throttle_command.connect(self.serial_worker.send)

        # STOP button -> close throttle
        self.upper.btn_stop.clicked.connect(self._on_stop)

    def _open_set_profile(self) -> None:
        self.pages.setCurrentWidget(self.set_profile)
        self.statusBar().showMessage("Editing test profile...", 2000)

    def _open_upper(self) -> None:
        self.pages.setCurrentWidget(self.upper)
        self.statusBar().showMessage("Back to main", 1500)

    # ── serial connection ────────────────────────────────────────────────────

    def _on_connect_toggle(self, checked: bool) -> None:
        if checked:
            port = self.upper.selected_port()
            baud = self.upper.selected_baud()
            if port == "(none)":
                self.upper.btn_connect.setChecked(False)
                QMessageBox.warning(self, "No Port",
                                    "No serial ports found. Click Refresh.")
                return
            self.upper.btn_connect.setText("Disconnect")
            self.serial_worker.connect_port(port, baud)
        else:
            self.upper.btn_connect.setText("Connect")
            self.serial_worker.disconnect_port()

    def _on_connection_changed(self, connected: bool, message: str) -> None:
        self.upper.lbl_status.setText(message)
        self.statusBar().showMessage(message, 3000)
        if not connected:
            self.upper.btn_connect.setChecked(False)
            self.upper.btn_connect.setText("Connect")

    def _on_stop(self) -> None:
        self.serial_worker.send("z")
        self.upper.throttle_slider.setValue(0)

    def closeEvent(self, event) -> None:
        self.serial_worker.disconnect_port()
        super().closeEvent(event)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
