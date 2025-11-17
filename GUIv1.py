# app.py
# Test Stand UI — Upper Window + fully functional "Set Test Profile" page.
# PySide6 + QtCharts, written for clarity and maintainability.

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

from PySide6.QtCore import Qt, QSize, QRect, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QStackedWidget,
    QGroupBox,
    QFrame,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QCheckBox,
    QLabel,
    QFileDialog,
    QMessageBox,
    QSizePolicy,
)

# QtCharts lives under PySide6.QtCharts
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis


# ---------- Small helpers ----------

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

    def paintEvent(self, event) -> None:  # noqa: N802
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
    Lower frame (editor) with a live-synced step plot.
      - Left: table [Time, Target] (+ Add / Remove)
      - Center: QtCharts step plot with axis labels
      - Right: Target selector
      - Bottom: Load CSV / Save CSV / Back
    """
    backRequested = Signal()

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

        # --- Main content row ---
        content = QHBoxLayout()
        content.setSpacing(12)
        root.addLayout(content, 1)

        # Left: Table + Add/Remove
        left_box = QGroupBox("Test Profile Table")
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(8, 8, 8, 8)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Time", "Target"])
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumWidth(280)
        self.table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.SelectedClicked |
            QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        left_layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Add Row")
        self.btn_remove = QPushButton("Remove Row")
        for b in (self.btn_add, self.btn_remove):
            b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        left_layout.addLayout(btn_row)

        content.addWidget(left_box, 1)

        # Center: QtCharts step plot
        chart_box = QGroupBox("Profile")
        chart_layout = QVBoxLayout(chart_box)
        chart_layout.setContentsMargins(8, 8, 8, 8)

        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.setTitle("Test Profile")

        # Axes with titles
        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Time")
        self.axis_x.setLabelFormat("%.0f")
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)

        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Target")
        self.axis_y.setLabelFormat("%.0f")
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)

        self.series = QLineSeries()
        self.chart.addSeries(self.series)
        self.series.attachAxis(self.axis_x)
        self.series.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        chart_layout.addWidget(self.chart_view)
        content.addWidget(chart_box, 2)

        # Right: Target selector
        right_box = QGroupBox("Target")
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.rb_load = QRadioButton("% Load")
        self.rb_rpm = QRadioButton("RPM")
        self.rb_throttle = QRadioButton("% Throttle")
        self.rb_load.setChecked(True)

        self.target_group = QButtonGroup(self)
        for rb in (self.rb_load, self.rb_rpm, self.rb_throttle):
            self.target_group.addButton(rb)
            right_layout.addWidget(rb)
        right_layout.addStretch(1)

        content.addWidget(right_box, 0)

        root.addWidget(HLine())

        # Bottom: file ops + back
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.btn_load_csv = QPushButton("Load Profile CSV")
        self.btn_save_csv = QPushButton("Save Profile CSV")
        self.btn_back = QPushButton("Back")
        self.btn_back.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        bottom.addWidget(self.btn_load_csv)
        bottom.addWidget(self.btn_save_csv)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_back)
        root.addLayout(bottom)

        # Seed with example rows
        for t, v in [(0, 20), (1, 90), (3, 50), (4, 20), (5, 0)]:
            self._append_row(t, v)

        # Signals
        self.btn_add.clicked.connect(self._on_add_row)
        self.btn_remove.clicked.connect(self._on_remove_row)
        self.table.itemChanged.connect(self._on_table_changed)
        self.btn_load_csv.clicked.connect(self._on_load_csv)
        self.btn_save_csv.clicked.connect(self._on_save_csv)
        self.btn_back.clicked.connect(self.backRequested.emit)

        # Draw initial chart
        self._update_chart_from_table()

    # ---- Table helpers ----

    def _append_row(self, t: float, v: float) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(t)))
        self.table.setItem(r, 1, QTableWidgetItem(str(v)))

    def _on_add_row(self) -> None:
        # Default: last time + 1, last target (or 0)
        data = self._read_table(silent=True)
        if data:
            last_t, last_v = data[-1]
            self._append_row(last_t + 1, last_v)
        else:
            self._append_row(0, 0)

        self._update_chart_from_table()

    def _on_remove_row(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount() > 0:
            rows = [self.table.rowCount() - 1]  # remove last if none selected
        for r in rows:
            self.table.removeRow(r)
        self._update_chart_from_table()

    def _on_table_changed(self, _item: QTableWidgetItem) -> None:
        # Validate numeric cells; color invalid ones.
        for r in range(self.table.rowCount()):
            for c in (0, 1):
                item = self.table.item(r, c)
                if item is None:
                    continue
                ok = self._is_float(item.text())
                item.setBackground(Qt.transparent if ok else Qt.red)
        self._update_chart_from_table()

    # ---- CSV IO ----

    def _on_load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Profile CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        try:
            rows: List[Tuple[float, float]] = []
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                for r in reader:
                    if len(r) < 2:
                        continue
                    try:
                        t = float(r[0]); v = float(r[1])
                        rows.append((t, v))
                    except ValueError:
                        # Skip headers/bad lines
                        continue
            if not rows:
                QMessageBox.information(self, "No Data", "No valid numeric rows found (expected: time,target).")
                return
            self.table.setRowCount(0)
            for t, v in rows:
                self._append_row(t, v)
            self._update_chart_from_table()
        except Exception as e:
            QMessageBox.warning(self, "Load Failed", f"Could not read file:\n{e}")

    def _on_save_csv(self) -> None:
        data = self._read_table(silent=False)
        if not data:
            QMessageBox.information(self, "Nothing to Save", "Table is empty or has invalid values.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Profile CSV", "profile.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "target"])
                for t, v in data:
                    writer.writerow([t, v])
            QMessageBox.information(self, "Saved", f"Profile saved to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not save file:\n{e}")

    # ---- Chart sync ----

    def _read_table(self, *, silent: bool) -> List[Tuple[float, float]]:
        """
        Returns numeric (time, target) rows.
        If `silent` is False, invalid cells cause the row to be dropped and a warning shown later.
        """
        data: List[Tuple[float, float]] = []
        bad = False
        for r in range(self.table.rowCount()):
            it_t = self.table.item(r, 0)
            it_v = self.table.item(r, 1)
            if it_t is None or it_v is None:
                continue
            t_s, v_s = it_t.text().strip(), it_v.text().strip()
            if not (self._is_float(t_s) and self._is_float(v_s)):
                bad = True
                continue
            data.append((float(t_s), float(v_s)))

        # sort by time so the plot is well formed
        data.sort(key=lambda tv: tv[0])

        if bad and not silent:
            QMessageBox.warning(self, "Invalid Rows", "Some rows were skipped due to non-numeric values.")
        return data

    def _update_chart_from_table(self) -> None:
        data = self._read_table(silent=True)

        # Build a step series: for each segment we add (t_i, v_i) and (t_{i+1}, v_i)
        step_points: List[Tuple[float, float]] = []
        if data:
            # Start at first time with first value
            t0, v0 = data[0]
            step_points.append((t0, v0))
            for (t_prev, v_prev), (t_next, v_next) in zip(data[:-1], data[1:]):
                # Horizontal to next time, then vertical jump (implicit by next point)
                step_points.append((t_next, v_prev))
                step_points.append((t_next, v_next))
        # Update series
        self.series.clear()
        for x, y in step_points:
            self.series.append(x, y)

        # Auto-set axes even when empty
        if data:
            t_vals = [t for t, _ in data]
            v_vals = [v for _, v in data]
            t_min, t_max = min(t_vals), max(t_vals)
            v_min, v_max = min(v_vals), max(v_vals)
            if t_min == t_max:
                t_min -= 1.0
                t_max += 1.0
            if v_min == v_max:
                v_min -= 1.0
                v_max += 1.0
        else:
            t_min, t_max, v_min, v_max = 0.0, 5.0, 0.0, 100.0

        pad_x = 0.05 * (t_max - t_min)
        pad_y = 0.10 * (v_max - v_min)
        self.axis_x.setRange(t_min - pad_x, t_max + pad_x)
        self.axis_y.setRange(v_min - pad_y, v_max + pad_y)

    @staticmethod
    def _is_float(s: str) -> bool:
        try:
            float(s)
            return True
        except Exception:
            return False


# ---------- Upper (first) page ----------

class UpperPage(QWidget):
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

        # Middle: plot placeholder + gauge placeholders
        mid = QHBoxLayout(); mid.setSpacing(12)

        # Simple label placeholder for the main plot
        plot_box = QGroupBox("")
        plot_layout = QVBoxLayout(plot_box)
        lbl = QLabel("Test Profile (placeholder)")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setMinimumHeight(300)
        plot_layout.addWidget(lbl)
        mid.addWidget(plot_box, 2)

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


# ---------- Main Window hosting both pages ----------

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
        self.pages.addWidget(self.set_profile)

        self.statusBar().showMessage("Ready")

    # Navigation
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
