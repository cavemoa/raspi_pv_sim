"""PyQt6 desktop UI for the Raspberry Pi PV year simulator."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import matplotlib

matplotlib.use("QtAgg")

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QAbstractTableModel, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QComboBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QDoubleSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from raspi_pv_year_sim import (
    LoadProfile,
    SystemConfig,
    build_models_from_config,
    load_yaml_config,
    monthly_summary,
    populate_results_figure,
    run_simulation,
    validate_configuration,
)


OPERATION_MODE_ITEMS = [
    ("Continuous operation", "continuous"),
    ("Sunrise to sunset", "sunrise_to_sunset"),
    ("Sunset to sunrise", "sunset_to_sunrise"),
]


class MonthlySummaryTableModel(QAbstractTableModel):
    """Qt table model that exposes monthly simulation statistics."""

    COLUMNS = [
        ("month", "Month", "{}"),
        ("complete_periods", "Complete", "{:.0f}"),
        ("early_shutdown_periods", "Shutdowns", "{:.0f}"),
        ("solar_surplus_days", "Solar surplus", "{:.0f}"),
        ("max_soc_days", "Max SOC days", "{:.0f}"),
        ("mean_usable_pv_wh", "PV Wh/day", "{:.0f}"),
        ("mean_required_wh", "Required Wh", "{:.0f}"),
        ("mean_margin_wh", "Margin Wh", "{:.0f}"),
        ("mean_operating_hours", "Hours", "{:.2f}"),
        ("lowest_soc_pct", "Lowest SOC %", "{:.1f}"),
        ("total_unmet_wh", "Unmet Wh", "{:.1f}"),
        ("runtime_lost_pct", "Lost %", "{:.1f}"),
        ("max_time_lost_hours", "Max lost h", "{:.2f}"),
    ]

    def __init__(self):
        """Create an empty table model ready for monthly summary data."""
        super().__init__()
        self._summary = None

    def rowCount(self, parent=None) -> int:
        """Return the number of monthly rows in the current summary."""
        if parent is not None and parent.isValid():
            return 0
        return 0 if self._summary is None else len(self._summary)

    def columnCount(self, parent=None) -> int:
        """Return the fixed number of monthly-statistic columns."""
        if parent is not None and parent.isValid():
            return 0
        return len(self.COLUMNS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return formatted cell text for display and alignment roles."""
        if not index.isValid() or self._summary is None:
            return None
        key, _, formatter = self.COLUMNS[index.column()]
        value = self._summary.iloc[index.row()][key]
        if role == Qt.ItemDataRole.DisplayRole:
            return formatter.format(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if key == "month":
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """Return horizontal column headings and vertical row numbers."""
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section][1]
        return str(section + 1)

    def set_summary(self, summary) -> None:
        """Replace the table data with a new monthly summary DataFrame."""
        self.beginResetModel()
        self._summary = summary
        self.endResetModel()


class SimulationWorker(QThread):
    """Run the simulation on a worker thread so the Qt UI stays responsive."""
    finished_ok = pyqtSignal(object, object, object, object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, config: SystemConfig, load: LoadProfile):
        """Store the scenario models that will be simulated in the worker."""
        super().__init__()
        self.config = config
        self.load = load

    def run(self) -> None:
        """Execute the simulation and emit success, status, or failure signals."""
        try:
            hourly, daily = run_simulation(
                self.config,
                self.load,
                status_callback=self.status.emit,
            )
            self.finished_ok.emit(hourly, daily, self.config, self.load)
        except Exception as exc:
            self.failed.emit(str(exc))


class SliderControl(QWidget):
    """Reusable labelled horizontal integer slider with a live value label."""
    value_changed = pyqtSignal(int)

    def __init__(
        self,
        title: str,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str,
        step: int = 1,
    ):
        """Create a labelled slider for one integer-valued scenario parameter."""
        super().__init__()
        self.suffix = suffix
        self.title_label = QLabel(title)
        self.value_label = QLabel()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setSingleStep(step)
        self.slider.setPageStep(step * 5)
        self.slider.setValue(value)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title_label)
        top.addStretch()
        top.addWidget(self.value_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addLayout(top)
        layout.addWidget(self.slider)

        self.slider.valueChanged.connect(self._on_value_changed)
        self._on_value_changed(value)

    def value(self) -> int:
        """Return the current slider value."""
        return self.slider.value()

    def set_value(self, value: int) -> None:
        """Programmatically set the slider value and update connected UI."""
        self.slider.setValue(value)

    def _on_value_changed(self, value: int) -> None:
        """Update the label and re-emit the slider value when it changes."""
        self.value_label.setText(f"{value}{self.suffix}")
        self.value_changed.emit(value)


class LoadProfileControl(QWidget):
    """One load-profile row with a fraction slider and wattage spin box."""
    value_changed = pyqtSignal()

    def __init__(self, title: str, fraction_pct: int, power_w: float):
        """Create a load-control row for idle, moderate, or heavy operation."""
        super().__init__()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("loadTitle")
        self.percent_label = QLabel()
        self.percent_label.setObjectName("loadPercent")

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setValue(fraction_pct)

        self.power_spin = QDoubleSpinBox()
        self.power_spin.setObjectName("powerSpin")
        self.power_spin.setRange(0.0, 30.0)
        self.power_spin.setSingleStep(0.1)
        self.power_spin.setDecimals(1)
        self.power_spin.setSuffix(" W")
        self.power_spin.setValue(power_w)
        self.power_spin.setFixedWidth(82)
        self.power_spin.setAlignment(Qt.AlignmentFlag.AlignRight)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title_label)
        top.addStretch()
        top.addWidget(self.percent_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.slider, stretch=1)
        row.addWidget(self.power_spin)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addLayout(top)
        layout.addLayout(row)

        self.slider.valueChanged.connect(self._on_slider_changed)
        self.power_spin.valueChanged.connect(self._on_power_changed)
        self._on_slider_changed(fraction_pct)

    def fraction_pct(self) -> int:
        """Return the load-state fraction as an integer percentage."""
        return self.slider.value()

    def power_w(self) -> float:
        """Return the load-state wattage from the spin box."""
        return self.power_spin.value()

    def _on_slider_changed(self, value: int) -> None:
        """Update the percent label and notify listeners after slider changes."""
        self.percent_label.setText(f"{value}%")
        self.value_changed.emit()

    def _on_power_changed(self) -> None:
        """Notify listeners after the wattage spin box changes."""
        self.value_changed.emit()


class MetricCard(QFrame):
    """Compact card used for the high-level scenario summary metrics."""
    def __init__(self, title: str, value: str):
        """Create a metric card with a value and small title label."""
        super().__init__()
        self.setObjectName("metricCard")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        title_label = QLabel(title)
        title_label.setObjectName("metricTitle")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        layout.addWidget(self.value_label)
        layout.addWidget(title_label)

    def set_value(self, value: str) -> None:
        """Update the displayed metric value."""
        self.value_label.setText(value)


class MainWindow(QMainWindow):
    """Main PyQt window containing controls, summary cards, and plots."""
    def __init__(self):
        """Load YAML defaults, build the UI, and run the initial simulation."""
        super().__init__()
        self.setWindowTitle("Raspberry Pi PV Simulator")
        self.resize(1420, 900)
        self.worker: SimulationWorker | None = None

        raw_config = load_yaml_config(Path("raspi_pv_config.yaml"))
        self.base_config, self.base_load, _, _ = build_models_from_config(raw_config)
        self.summary_model = MonthlySummaryTableModel()

        self._build_ui()
        self._apply_styles()
        self._update_load_status()
        self._run_simulation()

    def _build_ui(self) -> None:
        """Construct the left control column and right matplotlib plot panel."""
        root = QWidget()
        self.setCentralWidget(root)

        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(18)

        self.control_panel = QFrame()
        self.control_panel.setObjectName("controlPanel")
        self.control_panel.setFixedWidth(330)
        controls = QVBoxLayout(self.control_panel)
        controls.setContentsMargins(18, 18, 18, 18)
        controls.setSpacing(14)

        title = QLabel("PV Pi Scenario")
        title.setObjectName("appTitle")
        subtitle = QLabel("Adjust the core hardware and load assumptions, then rerun the yearly model.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        controls.addWidget(title)
        controls.addWidget(subtitle)

        controls.addSpacing(6)
        controls.addWidget(self._section_label("Operation"))
        self.operation_combo = QComboBox()
        self.operation_combo.setObjectName("operationCombo")
        for label, mode in OPERATION_MODE_ITEMS:
            self.operation_combo.addItem(label, mode)
        mode_index = self.operation_combo.findData(self.base_config.operation_mode)
        if mode_index < 0:
            mode_index = self.operation_combo.findData("sunset_to_sunrise")
        self.operation_combo.setCurrentIndex(mode_index)
        controls.addWidget(self.operation_combo)

        controls.addWidget(self._section_label("Hardware"))
        self.panel_slider = SliderControl("Solar panel", 10, 200, round(self.base_config.panel_w), " W", 5)
        self.battery_slider = SliderControl("Battery capacity", 5, 150, round(self.base_config.battery_ah), " Ah", 1)
        self.tilt_slider = SliderControl("Panel tilt", 0, 75, round(self.base_config.panel_tilt_deg), " deg", 1)
        controls.addWidget(self.panel_slider)
        controls.addWidget(self.battery_slider)
        controls.addWidget(self.tilt_slider)

        controls.addWidget(self._section_label("Battery Care"))
        self.min_soc_slider = SliderControl("Shutdown SOC", 0, 40, round(self.base_config.min_soc_fraction * 100), "%", 1)
        self.max_soc_slider = SliderControl("Max charge SOC", 50, 100, round(self.base_config.max_soc_fraction * 100), "%", 1)
        controls.addWidget(self.min_soc_slider)
        controls.addWidget(self.max_soc_slider)

        controls.addWidget(self._section_label("Pi Load Mix"))
        self.idle_load = LoadProfileControl("Idle", round(self.base_load.idle_pct * 100), self.base_load.idle_w)
        self.moderate_load = LoadProfileControl(
            "Moderate",
            round(self.base_load.moderate_pct * 100),
            self.base_load.moderate_w,
        )
        self.heavy_load = LoadProfileControl("Heavy", round(self.base_load.heavy_pct * 100), self.base_load.heavy_w)
        controls.addWidget(self.idle_load)
        controls.addWidget(self.moderate_load)
        controls.addWidget(self.heavy_load)

        self.load_status = QLabel()
        self.load_status.setObjectName("loadStatus")
        self.load_status.setWordWrap(True)
        controls.addWidget(self.load_status)

        for slider in [
            self.panel_slider,
            self.battery_slider,
            self.tilt_slider,
            self.min_soc_slider,
            self.max_soc_slider,
        ]:
            slider.value_changed.connect(self._on_slider_changed)
        for load_control in [self.idle_load, self.moderate_load, self.heavy_load]:
            load_control.value_changed.connect(self._on_slider_changed)

        self.run_button = QPushButton("Run simulation")
        self.run_button.setObjectName("runButton")
        self.run_button.clicked.connect(self._run_simulation)
        controls.addWidget(self.run_button)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label)
        controls.addStretch()

        self.metric_grid = QGridLayout()
        self.metric_grid.setHorizontalSpacing(10)
        self.metric_grid.setVerticalSpacing(10)
        self.success_card = MetricCard("Complete periods", "-")
        self.shutdown_card = MetricCard("Shutdown periods", "-")
        self.soc_card = MetricCard("Lowest SOC", "-")
        self.lost_card = MetricCard("Runtime lost", "-")
        self.metric_grid.addWidget(self.success_card, 0, 0)
        self.metric_grid.addWidget(self.shutdown_card, 0, 1)
        self.metric_grid.addWidget(self.soc_card, 1, 0)
        self.metric_grid.addWidget(self.lost_card, 1, 1)
        controls.addLayout(self.metric_grid)

        self.plot_panel = QFrame()
        self.plot_panel.setObjectName("plotPanel")
        result_layout = QVBoxLayout(self.plot_panel)
        result_layout.setContentsMargins(14, 14, 14, 14)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("resultTabs")

        plot_tab = QWidget()
        plot_layout = QVBoxLayout(plot_tab)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(10, 7), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        plot_layout.addWidget(self.canvas)

        table_tab = QWidget()
        table_layout = QVBoxLayout(table_tab)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.summary_table = QTableView()
        self.summary_table.setObjectName("summaryTable")
        self.summary_table.setModel(self.summary_model)
        self.summary_table.setAlternatingRowColors(True)
        self.summary_table.setSortingEnabled(False)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.horizontalHeader().setStretchLastSection(False)
        self.summary_table.setFixedHeight(440)
        table_layout.addWidget(self.summary_table)

        table_note = QLabel(
            "<ul>"
            "<li><b>Complete</b>: operating periods completed without shutdown.</li>"
            "<li><b>Shutdowns</b>: operating periods that ended early.</li>"
            "<li><b>Solar surplus</b>: days where PV energy exceeded scheduled demand.</li>"
            "<li><b>Max SOC days</b>: days the battery reached the configured charge cap.</li>"
            "<li><b>PV Wh/day</b>: mean usable PV generation in watt-hours per day.</li>"
            "<li><b>Required Wh</b>: mean scheduled operating demand in watt-hours.</li>"
            "<li><b>Margin Wh</b>: mean daily PV energy minus scheduled demand, in watt-hours.</li>"
            "<li><b>Hours</b>: mean scheduled operating-period duration in hours.</li>"
            "<li><b>Lowest SOC %</b>: lowest battery state of charge reached in the month.</li>"
            "<li><b>Unmet Wh</b>: total unmet load after early shutdowns, in watt-hours.</li>"
            "<li><b>Lost %</b>: percentage of scheduled operating time lost to shutdowns.</li>"
            "<li><b>Max lost h</b>: maximum operating time lost in a single period, in hours.</li>"
            "</ul>"
        )
        table_note.setObjectName("tableNote")
        table_note.setWordWrap(True)
        table_layout.addWidget(table_note)

        self.tabs.addTab(plot_tab, "Plots")
        self.tabs.addTab(table_tab, "Monthly table")
        result_layout.addWidget(self.tabs)

        root_layout.addWidget(self.control_panel)
        root_layout.addWidget(self.plot_panel, stretch=1)

    def _section_label(self, text: str) -> QLabel:
        """Create a consistently styled section heading for the control panel."""
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _on_slider_changed(self) -> None:
        """Handle any scenario-control change that affects load-status validity."""
        self._update_load_status()

    def _update_load_status(self) -> None:
        """Refresh the load total, average wattage, and run-button enabled state."""
        total = self.idle_load.fraction_pct() + self.moderate_load.fraction_pct() + self.heavy_load.fraction_pct()
        load = self._current_load(allow_invalid=True)
        message = f"Load mix total: {total}% | Average load: {load.average_w:.2f} W"
        if total == 100:
            self.load_status.setProperty("state", "ok")
            self.run_button.setEnabled(True)
        else:
            self.load_status.setProperty("state", "warning")
            self.run_button.setEnabled(False)
            message += " | adjust to exactly 100%"
        self.load_status.setText(message)
        self.load_status.style().unpolish(self.load_status)
        self.load_status.style().polish(self.load_status)

    def _current_config(self) -> SystemConfig:
        """Build a SystemConfig from current UI controls and YAML-backed defaults."""
        return replace(
            self.base_config,
            operation_mode=str(self.operation_combo.currentData()),
            panel_w=float(self.panel_slider.value()),
            panel_tilt_deg=float(self.tilt_slider.value()),
            battery_ah=float(self.battery_slider.value()),
            min_soc_fraction=self.min_soc_slider.value() / 100.0,
            max_soc_fraction=self.max_soc_slider.value() / 100.0,
            refresh_weather_cache=False,
        )

    def _current_load(self, allow_invalid: bool = False) -> LoadProfile:
        """Build a LoadProfile from current load sliders and wattage spin boxes."""
        return LoadProfile(
            idle_pct=self.idle_load.fraction_pct() / 100.0,
            moderate_pct=self.moderate_load.fraction_pct() / 100.0,
            heavy_pct=self.heavy_load.fraction_pct() / 100.0,
            idle_w=self.idle_load.power_w(),
            moderate_w=self.moderate_load.power_w(),
            heavy_w=self.heavy_load.power_w(),
        )

    def _run_simulation(self) -> None:
        """Validate the UI scenario and launch the worker-thread simulation."""
        config = self._current_config()
        load = self._current_load()
        try:
            validate_configuration(config, load)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid scenario", str(exc))
            return

        self.run_button.setEnabled(False)
        self.status_label.setText("Starting simulation...")
        self.worker = SimulationWorker(config, load)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished_ok.connect(self._on_simulation_finished)
        self.worker.failed.connect(self._on_simulation_failed)
        self.worker.start()

    def _on_simulation_finished(self, hourly, daily, config: SystemConfig, load: LoadProfile) -> None:
        """Render successful simulation results into the plot and summary cards."""
        populate_results_figure(self.figure, hourly, daily, config)
        self.canvas.draw_idle()
        self._update_summary_table(daily, load)
        self._update_metrics(daily, load)
        self.status_label.setText("Done")
        self.run_button.setEnabled(True)
        self.worker = None

    def _on_simulation_failed(self, message: str) -> None:
        """Restore the UI and show an error dialog after simulation failure."""
        self.status_label.setText("Simulation failed")
        self.run_button.setEnabled(True)
        self.worker = None
        QMessageBox.critical(self, "Simulation failed", message)

    def _update_metrics(self, daily, load: LoadProfile) -> None:
        """Update year-level metric cards from the daily simulation summary."""
        complete = int((~daily["early_shutdown"]).sum())
        shutdowns = int(daily["early_shutdown"].sum())
        lowest_soc = float(daily["min_soc_pct"].min())
        lost_hours = float(daily["unmet_load_wh"].sum() / load.average_w) if load.average_w else 0.0
        total_hours = float(daily["night_hours"].sum())
        lost_pct = lost_hours / total_hours * 100.0 if total_hours else 0.0

        self.success_card.set_value(f"{complete}/{len(daily)}")
        self.shutdown_card.set_value(str(shutdowns))
        self.soc_card.set_value(f"{lowest_soc:.1f}%")
        self.lost_card.set_value(f"{lost_pct:.1f}%")

    def _update_summary_table(self, daily, load: LoadProfile) -> None:
        """Refresh the monthly statistics table from the daily summary."""
        self.summary_model.set_summary(monthly_summary(daily, load))
        self.summary_table.resizeColumnsToContents()

    def _apply_styles(self) -> None:
        """Apply the compact light visual styling for the desktop UI."""
        app_font = QFont("Segoe UI", 10)
        QApplication.instance().setFont(app_font)
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f5f5f7;
            }
            #controlPanel, #plotPanel {
                background: rgba(255, 255, 255, 0.94);
                border: 1px solid #d7d7dc;
                border-radius: 18px;
            }
            #appTitle {
                color: #1d1d1f;
                font-size: 24px;
                font-weight: 700;
            }
            #subtitle {
                color: #6e6e73;
                font-size: 12px;
                line-height: 16px;
            }
            #sectionLabel {
                color: #1d1d1f;
                font-size: 12px;
                font-weight: 700;
                margin-top: 8px;
                text-transform: uppercase;
            }
            QLabel {
                color: #1d1d1f;
            }
            SliderControl QLabel {
                font-size: 12px;
            }
            #loadTitle, #loadPercent {
                font-size: 12px;
            }
            QSlider::groove:horizontal {
                height: 5px;
                border-radius: 3px;
                background: #dedee3;
            }
            QSlider::sub-page:horizontal {
                height: 5px;
                border-radius: 3px;
                background: #007aff;
            }
            QSlider::handle:horizontal {
                width: 20px;
                height: 20px;
                margin: -8px 0;
                border-radius: 10px;
                background: #ffffff;
                border: 1px solid #c7c7cc;
            }
            QSlider::handle:horizontal:hover {
                border: 1px solid #007aff;
            }
            #powerSpin {
                background: #ffffff;
                border: 1px solid #d1d1d6;
                border-radius: 8px;
                padding: 4px 6px;
                min-height: 22px;
                font-size: 12px;
            }
            #operationCombo {
                background: #ffffff;
                border: 1px solid #d1d1d6;
                border-radius: 10px;
                padding: 7px 10px;
                min-height: 24px;
            }
            #resultTabs::pane {
                border: none;
            }
            #resultTabs QTabBar::tab {
                background: #ececf1;
                color: #1d1d1f;
                border: 1px solid #d1d1d6;
                border-radius: 9px;
                padding: 7px 14px;
                margin-right: 6px;
            }
            #resultTabs QTabBar::tab:selected {
                background: #ffffff;
                border-color: #b9b9c1;
            }
            #summaryTable {
                background: #ffffff;
                alternate-background-color: #f7f7fa;
                border: 1px solid #d7d7dc;
                border-radius: 10px;
                gridline-color: #ececf1;
                selection-background-color: #dbeafe;
                selection-color: #1d1d1f;
            }
            #summaryTable QHeaderView::section {
                background: #f5f5f7;
                color: #1d1d1f;
                border: none;
                border-bottom: 1px solid #d7d7dc;
                padding: 7px 8px;
                font-weight: 700;
            }
            #tableNote {
                color: #6e6e73;
                font-size: 12px;
                line-height: 16px;
                padding: 6px 4px 0 4px;
            }
            #runButton {
                background: #007aff;
                border: none;
                border-radius: 10px;
                color: white;
                font-weight: 700;
                min-height: 34px;
            }
            #runButton:disabled {
                background: #b8cce8;
            }
            #runButton:hover:!disabled {
                background: #006ee6;
            }
            #statusLabel {
                color: #6e6e73;
                font-size: 12px;
            }
            #loadStatus {
                border-radius: 10px;
                padding: 8px 10px;
                font-size: 12px;
            }
            #loadStatus[state="ok"] {
                color: #1f6b35;
                background: #edf8f0;
            }
            #loadStatus[state="warning"] {
                color: #8a4b00;
                background: #fff4df;
            }
            #metricCard {
                background: #f7f7fa;
                border: 1px solid #e0e0e6;
                border-radius: 12px;
            }
            #metricValue {
                color: #1d1d1f;
                font-size: 19px;
                font-weight: 700;
            }
            #metricTitle {
                color: #6e6e73;
                font-size: 11px;
            }
            """
        )


def main() -> None:
    """Application entry point: create QApplication, show the window, and run Qt."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
