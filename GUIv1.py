# app.py
# Test Stand UI — Upper Window + "Set Test Profile" page (PySide6)
# Focused on readability and future extensibility.

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from PySide6.QtCore import Qt, QSize, QRect
from PySide6.QtGui import QPainter, QPen, QFont, QAction
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


# ---------- Small helpers ----------

class HLine(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)


class PlotPlaceholder(QWidget):
    """
    Minimal 'plot' placeholder with a title, axes, and a simple step-like sketch.
    Replace with QtCharts/pyqtgraph later without touching page layout.
    """
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self.setMinimumSize(QSize(420, 260))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paintEvent(self, event) -> None:  # noqa: N802
        rect = self.rect().adjusted(10, 10, -10, -10)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Frame
        pen = QPen(self.palette().mid().color())
        pen.setWidth(2)
        p.setPen(pen)
        p.drawRoundedRect(rect, 12, 12)

        # Title
        title_rect = QRect(rect.left(), rect.top(), rect.width(), 26)
        p.setPen(self.palette().windowText().color())
        title_font = QFont(self.font()); title_font.setBold(True)
        p.setFont(title_font)
        p.drawText(title_rect, Qt.AlignCenter, self._title)

        # Axes
        area = rect.adjusted(16, 30, -16, -16)
        p.setPen(self.palette().mid().color())
        p.drawLine(area.bottomLeft(), area.topLeft())
        p.drawLine(area.bottomLeft(), area.bottomRight())

        # Axis labels
        lab = QFont(self.font()); lab.setPointSizeF(max(8.0, lab.pointSizeF()))
        p.setFont(lab); p.setPen(self.palette().windowText().color())
        p.drawText(area.left() - 6, area.top() + 10, "Target")
        p.drawText(area.right() - 24, area.bottom() + 14, "Time")

        # Step sketch
        p.setPen(self.palette().windowText().color())
        left = area.left() + int(area.width() * 0.08)
        mid1 = area.left() + int(area.width() * 0.35)
        mid2 = area.left() + int(area.width() * 0.70)
        y_hi = area.top() + int(area.height() * 0.15)
        y_mid = area.top() + int(area.height() * 0.55)
        y_lo = area.bottom() - 2
        p.drawLine(left, y_lo, left, y_hi)
        p.drawLine(left, y_hi, mid1, y_hi)
        p.drawLine(mid1, y_hi, mid1, y_mid)
        p.drawLine(mid1, y_mid, mid2, y_mid)
        p.drawLine(mid2, y_mid, mid2, y_lo)


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

        pen = QPen(self.palette().mid().color()); pen.setWidth(2)
        p.setPen(pen); p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)

        p.setPen(self.palette().windowText().color())
        title = QFont(self.font()); title.setBold(True); p.setFont(title)
        p.drawText(0, 2, self.width(), 22, Qt.AlignHCenter | Qt.AlignVCenter, self._title)

        dash = QFont(self.font()); dash.setPointSizeF(max(10.0, dash.pointSizeF())); p.setFont(dash)
        p.drawText(0, cy - 10, self.width(), 20, Qt.AlignCenter, "—")


# ---------- Set Test Profile Page ----------

class SetProfilePage(QWidget):
    """
    Implements the lower 'Set Test Profile' frame:
      - Left: editable table [Time, Target]
      - Center: plot placeholder
      - Right: target selector (% Load / RPM / % Throttle)
      - Bottom: Load Profile CSV + Back
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel("Set Test Profile")
        title_font = title.font(); title_font.setBold(True); title_font.setPointSize(title_font.pointSize() + 2)
        title.setFont(title_font)
        root.addWidget(title)
        root.addWidget(HLine())

        content = QHBoxLayout()
        content.setSpacing(12)
        root.addLayout(content, 1)

        # --- Left: Table ---
        left_box = QGroupBox("Test Profile Table")
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(8, 8, 8, 8)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Time", "Target"])
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumWidth(260)
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed)
        left_layout.addWidget(self.table, 1)

        # Seed with example rows from the sketch
        for t, v in [(0, 20), (1, 90), (3, 50), (4, 20), (5, 0)]:
            self._append_row(t, v)

        content.addWidget(left_box, 1)

        # --- Center: Plot ---
        self.plot = PlotPlaceholder("Test Profile")
        content.addWidget(self.plot, 2)

        # --- Right: Target selector ---
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

        # --- Bottom buttons ---
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        root.addLayout(bottom)

        self.btn_load_csv = QPushButton("Load Profile CSV")
        self.btn_back = QPushButton("Back")
        self.btn_back.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        bottom.addWidget(self.btn_load_csv)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_back)

        # Wire actions
        self.btn_load_csv.clicked.connect(self._on_load_csv)

    # ---- API for host window ----

    def on_back_requested(self, slot) -> None:
        self.btn_back.clicked.connect(slot)

    # ---- Internals ----

    def _append_row(self, t: float, v: float) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(t)))
        self.table.setItem(row, 1, QTableWidgetItem(str(v)))

    def _on_load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Profile CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return

        try:
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception as e:
            QMessageBox.warning(self, "Load Failed", f"Could not read file:\n{e}")
            return

        # Expect two columns: time, target
        cleaned: List[tuple[float, float]] = []
        for r in rows:
            if len(r) < 2:
                continue
            try:
                t = float(r[0]); v = float(r[1])
                cleaned.append((t, v))
            except ValueError:
                # Skip header or bad lines
                continue

        if not cleaned:
            QMessageBox.information(self, "No Data", "No valid rows found (expected two numeric columns: time, target).")
            return

        self.table.setRowCount(0)
        for t, v in cleaned:
            self._append_row(t, v)


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

        # Middle: plot + gauges
        mid = QHBoxLayout(); mid.setSpacing(12)
        left_plot = PlotPlaceholder("Test Profile"); mid.addWidget(left_plot, 2)

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
        self.resize(1220, 720)

        self.pages = QStackedWidget(self)
        self.setCentralWidget(self.pages)

        # Page 0: Upper
        self.upper = UpperPage(self._open_set_profile)
        self.pages.addWidget(self.upper)

        # Page 1: Set Profile
        self.set_profile = SetProfilePage()
        self.set_profile.on_back_requested(self._open_upper)
        self.pages.addWidget(self.set_profile)

        # Menu shortcut for navigation (optional)
        back_act = QAction("Back to Main", self); back_act.setShortcut("Esc"); back_act.triggered.connect(self._open_upper)
        self.addAction(back_act)

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
