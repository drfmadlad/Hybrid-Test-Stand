# app.py
# Test Stand UI — main page plot stays in sync with Set Test Profile.
# PySide6 + QtCharts. Clean, readable, ready to extend.

from __future__ import annotations

import csv
import sys
from typing import List, Tuple

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QStackedWidget, QGroupBox, QFrame, QTableWidget, QTableWidgetItem,
    QPushButton, QRadioButton, QButtonGroup, QCheckBox, QLabel,
    QFileDialog, QMessageBox, QSizePolicy
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis


# ---------- small helpers ----------
class HLine(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)


class CircleGaugePlaceholder(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self.setMinimumSize(QSize(120, 120))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        size = min(self.width(), self.height()) - 12
        cx, cy = self.width() // 2, self.height() // 2 + 6
        r = max(40, size // 2)

        p.setPen(self.palette().mid().color())
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        p.setPen(self.palette().windowText().color())
        p.drawText(0, 2, self.width(), 22, Qt.AlignHCenter | Qt.AlignVCenter, self._title)
        p.drawText(0, cy - 10, self.width(), 20, Qt.AlignCenter, "—")


# ---------- Set Test Profile Page ----------
class SetProfilePage(QWidget):
    """
    Editor:
      - Left: table [Time, Target] + Add/Remove
      - Center: step plot with axis labels
      - Right: Target selector
      - Bottom: Load/Save CSV + Back
    Emits `profileChanged(data, target_label)` on every change.
    """
    backRequested = Signal()
    profileChanged = Signal(list, str)  # List[Tuple[float, float]], target label

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

        # --- Left: table + buttons ---
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

        # --- Center: chart ---
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

        # --- Right: target selector ---
        right_box = QGroupBox("Target")
        right = QVBoxLayout(right_box); right.setContentsMargins(8, 8, 8, 8)
        self.rb_load = QRadioButton("% Load"); self.rb_rpm = QRadioButton("RPM"); self.rb_throttle = QRadioButton("% Throttle")
        self.rb_load.setChecked(True)
        self.target_group = QButtonGroup(self)
        for rb in (self.rb_load, self.rb_rpm, self.rb_throttle):
            self.target_group.addButton(rb); right.addWidget(rb)
        right.addStretch(1)
        content.addWidget(right_box, 0)

        root.addWidget(HLine())

        # --- Bottom: file ops + back ---
        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self.btn_load_csv = QPushButton("Load Profile CSV")
        self.btn_save_csv = QPushButton("Save Profile CSV")
        self.btn_back = QPushButton("Back"); self.btn_back.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        bottom.addWidget(self.btn_load_csv); bottom.addWidget(self.btn_save_csv); bottom.addStretch(1); bottom.addWidget(self.btn_back)
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
            rb.toggled.connect(lambda _=None: self._emit_profile_changed())  # updates target label

        # Initial draw + emit
        self._update_chart_from_table()
        self._emit_profile_changed()

    # --- public helpers ---
    def emit_current_profile(self) -> None:
        """Call once after wiring to push current profile to listeners."""
        self._emit_profile_changed()

    # --- table ops ---
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
        # validate numeric cells (light feedback)
        for r in range(self.table.rowCount()):
            for c in (0, 1):
                it = self.table.item(r, c)
                if not it:
                    continue
                ok = self._is_float(it.text())
                it.setBackground(Qt.transparent if ok else Qt.red)
        self._update_chart_from_table()
        self._emit_profile_changed()

    # --- csv io ---
    def _on_load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Profile CSV", "", "CSV Files (*.csv);;All Files (*)")
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
                QMessageBox.information(self, "No Data", "Expected two numeric columns: time,target")
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
            QMessageBox.information(self, "Nothing to Save", "Table is empty or invalid.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Profile CSV", "profile.csv", "CSV Files (*.csv)")
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

    # --- chart sync + emit ---
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
            QMessageBox.warning(self, "Invalid Rows", "Some rows were skipped due to non-numeric values.")
        return data

    def _update_chart_from_table(self) -> None:
        data = self._read_table(silent=True)
        # build step points
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

        # axis ranges
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
        target = "% Load" if self.rb_load.isChecked() else ("RPM" if self.rb_rpm.isChecked() else "% Throttle")
        self.profileChanged.emit(data, target)

    @staticmethod
    def _is_float(s: str) -> bool:
        try:
            float(s); return True
        except Exception:
            return False


# ---------- Upper (main) page ----------
class UpperPage(QWidget):
    """Main page with mode controls, live plot, gauge placeholders, and run controls."""
    def __init__(self, on_open_set_profile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self); root.setContentsMargins(12, 12, 12, 12); root.setSpacing(10)

        # Top bar
        top = QHBoxLayout(); top.setSpacing(8)
        self.rb_manual = QRadioButton("Manual"); self.rb_auto = QRadioButton("Auto"); self.rb_manual.setChecked(True)
        self.btn_set_profile = QPushButton("Set Test Profile"); self.btn_set_profile.setMinimumHeight(36)
        self.btn_set_profile.clicked.connect(on_open_set_profile)
        top.addWidget(self.rb_manual); top.addWidget(self.rb_auto); top.addSpacing(16); top.addWidget(self.btn_set_profile); top.addStretch(1)
        root.addLayout(top); root.addWidget(HLine())

        # Middle: plot + gauges
        mid = QHBoxLayout(); mid.setSpacing(12)

        # Live chart on the left
        plot_box = QGroupBox("")
        plot_layout = QVBoxLayout(plot_box); plot_layout.setContentsMargins(8, 8, 8, 8)
        self.chart = QChart(); self.chart.legend().hide(); self.chart.setTitle("Test Profile")
        self.axis_x = QValueAxis(); self.axis_x.setTitleText("Time"); self.axis_x.setLabelFormat("%.0f")
        self.axis_y = QValueAxis(); self.axis_y.setTitleText("Target"); self.axis_y.setLabelFormat("%.0f")
        self.chart.addAxis(self.axis_x, Qt.AlignBottom); self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.series = QLineSeries(); self.chart.addSeries(self.series)
        self.series.attachAxis(self.axis_x); self.series.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart); self.chart_view.setRenderHint(QPainter.Antialiasing)
        plot_layout.addWidget(self.chart_view)
        mid.addWidget(plot_box, 2)

        # Gauge placeholders on the right
        gauges_box = QGroupBox("")
        g = QGridLayout(gauges_box); g.setContentsMargins(8, 8, 8, 8); g.setHorizontalSpacing(10); g.setVerticalSpacing(10)
        titles = ("RPM", "AFR", "Throttle", "LV Voltage", "Coolant", "% Load", "kW", "", "")
        for i, t in enumerate(titles):
            r, c = divmod(i, 3)
            g.addWidget(CircleGaugePlaceholder(t if t else " "), r, c)
        mid.addWidget(gauges_box, 1)

        root.addLayout(mid, 1); root.addWidget(HLine())

        # Bottom bar
        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self.chk_logging = QCheckBox("Data Logging")
        self.btn_run = QPushButton("Run Test Profile"); self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #c62828; color: white; font-weight: bold; }"
            "QPushButton:pressed { background: #8e0000; }"
        )
        for b in (self.btn_run, self.btn_stop): b.setMinimumHeight(36)
        bottom.addWidget(self.chk_logging); bottom.addStretch(1); bottom.addWidget(self.btn_run); bottom.addWidget(self.btn_stop)
        root.addLayout(bottom)

    # called by MainWindow when profile changes
    def update_profile(self, data: List[Tuple[float, float]], target_label: str) -> None:
        # step series
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

        # axis labels and ranges
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


# ---------- Main Window ----------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Test Stand UI")
        self.resize(1240, 760)

        self.pages = QStackedWidget(self)
        self.setCentralWidget(self.pages)

        # Page 0: Upper
        self.upper = UpperPage(self._open_set_profile)
        self.pages.addWidget(self.upper)

        # Page 1: Set Profile
        self.set_profile = SetProfilePage()
        self.set_profile.backRequested.connect(self._open_upper)
        # >>> Live sync: connect editor -> main plot
        self.set_profile.profileChanged.connect(self.upper.update_profile)
        self.pages.addWidget(self.set_profile)

        self.statusBar().showMessage("Ready")

        # Push initial profile to the main page
        self.set_profile.emit_current_profile()

    def _open_set_profile(self) -> None:
        self.pages.setCurrentWidget(self.set_profile)
        self.statusBar().showMessage("Editing test profile…", 2000)

    def _open_upper(self) -> None:
        self.pages.setCurrentWidget(self.upper)
        self.statusBar().showMessage("Back to main", 1500)


# ---------- Entrypoint ----------
def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
