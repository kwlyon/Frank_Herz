"""Tkinter + Matplotlib laboratory interface."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import serial.tools.list_ports

from . import config
from .core import (
    AcquisitionController,
    Calibration,
    DataPoint,
    downsample_for_display,
    downsample_for_strip_recorder,
    nearest_xy_point,
)
from .export import export_xlsx
from .transport import SerialTransport, SimulatorTransport


XY_PLOT_MODE = "X–Y Plot"
STRIP_RECORDER_MODE = "Strip Recorder"
PLOT_MODES = (STRIP_RECORDER_MODE, XY_PLOT_MODE)


class PlotNavigationToolbar(NavigationToolbar2Tk):
    """Matplotlib toolbar whose Home action restores live autoscaling."""

    def __init__(self, *args, home_callback, **kwargs) -> None:
        self._home_callback = home_callback
        super().__init__(*args, **kwargs)

    def home(self, *args) -> None:
        del args
        self._home_callback()


class FranckHertzApp(tk.Tk):
    """Scientific XY plotter for paired tube-drive and picoammeter readings."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Franck-Hertz Data Acquisition")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self._transport: SerialTransport | SimulatorTransport | None = None
        self._event_queue: queue.Queue[tuple[str, bytes | str]] = queue.Queue()
        self._handshake_timeout_id: str | None = None
        self._identity_probe_id: str | None = None
        self._hardware_banner_seen = False
        self._paired_capability_seen = False
        self._paired_mode_requested = False
        self._resume_after_protocol = False
        self._last_plotted_count = -1
        self._changing_plot_limits = False
        self._cursor_dragging = False
        self._cursor_target_x: float | None = None
        self._cursor_x_values: list[float] = []
        self._cursor_y_values: list[float] = []
        self._cursor_selected_point: tuple[float, float] | None = None
        self._closing = False
        self._port_open = False
        self.autoscale_var = tk.BooleanVar(value=True)
        self.cursor_visible_var = tk.BooleanVar(value=True)
        self.plot_mode_var = tk.StringVar(value=STRIP_RECORDER_MODE)
        self.controller = AcquisitionController(
            sender=self._write,
            calibration=Calibration(),
        )

        self._build_ui()
        self._refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(config.UI_UPDATE_MS, self._process_events)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        controls = ttk.Frame(self, padding=(10, 10, 10, 4))
        controls.pack(fill=tk.X)

        self.connection_led = tk.Canvas(
            controls, width=18, height=18, highlightthickness=0
        )
        self.connection_led.grid(row=0, column=0, padx=(0, 7), pady=2)
        self._led = self.connection_led.create_oval(
            2, 2, 16, 16, fill="#777777", outline="#555555"
        )

        ttk.Label(controls, text="Arduino port:").grid(row=0, column=1, sticky="w")
        self.port_combo = ttk.Combobox(controls, width=26, state="readonly")
        self.port_combo.grid(row=0, column=2, padx=(5, 6))
        ttk.Button(controls, text="Refresh", command=self._refresh_ports).grid(
            row=0, column=3, padx=(0, 8)
        )
        self.connect_button = ttk.Button(
            controls, text="Connect", command=self._toggle_connection
        )
        self.connect_button.grid(row=0, column=4, padx=(0, 18))

        self.start_button = ttk.Button(
            controls,
            text="Start Acquisition",
            command=self._start_acquisition,
            state=tk.DISABLED,
        )
        self.start_button.grid(row=0, column=5, padx=(0, 6))
        self.stop_button = ttk.Button(
            controls,
            text="Stop Acquisition",
            command=self._stop_acquisition,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=0, column=6, padx=(0, 18))

        self.clear_button = ttk.Button(
            controls, text="Clear Data", command=self._clear_data
        )
        self.clear_button.grid(row=0, column=7, padx=(0, 6))
        self.export_button = ttk.Button(
            controls, text="Export Data", command=self._export_data
        )
        self.export_button.grid(row=0, column=8)
        controls.columnconfigure(9, weight=1)

        ttk.Label(controls, text="Plot mode:").grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        self.plot_mode_combo = ttk.Combobox(
            controls,
            width=20,
            state="readonly",
            textvariable=self.plot_mode_var,
            values=PLOT_MODES,
        )
        self.plot_mode_combo.grid(
            row=1, column=2, sticky="w", padx=(5, 6), pady=(8, 0)
        )
        self.plot_mode_combo.bind("<<ComboboxSelected>>", self._change_plot_mode)

        status_frame = ttk.Frame(self, padding=(12, 2, 12, 4))
        status_frame.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Disconnected")
        self.status_label = tk.Label(
            status_frame,
            textvariable=self.status_var,
            anchor="w",
            fg="#555555",
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.count_var = tk.StringVar(value="0 points")
        ttk.Label(status_frame, textvariable=self.count_var).pack(side=tk.RIGHT)

        calibration_text = (
            f"Drive scale: {self.controller.calibration.drive_scale:g}×    "
            f"Picoammeter: {self.controller.calibration.picoammeter_mv_per_pa:g} mV/pA"
        )
        ttk.Label(self, text=calibration_text, foreground="#555555").pack(
            anchor="w", padx=12, pady=(0, 2)
        )

        self.figure = Figure(figsize=(10.8, 6.2), dpi=100, constrained_layout=True)
        self.axes = self.figure.add_subplot(111)
        self.secondary_axes = self.axes.twinx()
        self.axes.set_title("Franck-Hertz Characteristic")
        self.axes.set_xlabel("Drive Voltage (V)")
        self.axes.set_ylabel("Tube Current (pA)")
        self.axes.grid(True, color="#d8d8d8", linewidth=0.8)
        self.axes.set_axisbelow(True)
        (self.plot_line,) = self.axes.plot(
            [],
            [],
            color="#145DA0",
            linewidth=1.35,
            marker="o",
            markersize=2.2,
            markeredgewidth=0,
        )
        self.cursor_line = self.axes.axvline(
            0.0,
            color="#E38B00",
            linewidth=1.8,
            linestyle="--",
            alpha=0.95,
            visible=False,
            zorder=5,
        )
        (self.cursor_marker,) = self.axes.plot(
            [],
            [],
            marker="o",
            markersize=6.0,
            markerfacecolor="#FFF4D6",
            markeredgecolor="#C66E00",
            markeredgewidth=1.3,
            linestyle="none",
            visible=False,
            zorder=6,
        )
        self.cursor_annotation = self.axes.annotate(
            "",
            xy=(0.0, 0.0),
            xytext=(11, 11),
            textcoords="offset points",
            fontsize=9,
            color="#3F2A00",
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": "#FFFDF6",
                "edgecolor": "#D28A19",
                "alpha": 0.92,
            },
            visible=False,
            zorder=7,
        )
        (self.current_time_line,) = self.axes.plot(
            [],
            [],
            color="#145DA0",
            linewidth=1.35,
            label="Tube Current",
            visible=False,
        )
        (self.drive_time_line,) = self.secondary_axes.plot(
            [],
            [],
            color="#C43B3B",
            linewidth=1.35,
            label="Drive Voltage",
            visible=False,
        )
        self.secondary_axes.set_visible(False)
        self.axes.margins(x=0.05, y=0.08)
        self.secondary_axes.margins(y=0.08)
        plot_frame = ttk.Frame(self, padding=(10, 2, 10, 8))
        plot_frame.pack(fill=tk.BOTH, expand=True)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(
            side=tk.TOP, fill=tk.BOTH, expand=True
        )

        navigation_frame = ttk.Frame(plot_frame)
        navigation_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(3, 0))
        self.toolbar = PlotNavigationToolbar(
            self.canvas,
            navigation_frame,
            pack_toolbar=False,
            home_callback=self._home_plot,
        )
        self.toolbar.update()
        self.toolbar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.autoscale_checkbutton = ttk.Checkbutton(
            navigation_frame,
            text="Auto-scale live",
            variable=self.autoscale_var,
            command=self._toggle_autoscale,
        )
        self.autoscale_checkbutton.pack(side=tk.RIGHT, padx=(10, 4))
        self.cursor_checkbutton = ttk.Checkbutton(
            navigation_frame,
            text="Measurement cursor",
            variable=self.cursor_visible_var,
            command=self._toggle_measurement_cursor,
        )
        self.cursor_checkbutton.pack(side=tk.RIGHT, padx=(10, 2))

        self.canvas.mpl_connect("button_press_event", self._cursor_mouse_press)
        self.canvas.mpl_connect("motion_notify_event", self._cursor_mouse_move)
        self.canvas.mpl_connect("button_release_event", self._cursor_mouse_release)

        # A toolbar pan/zoom changes the axes limits. That change pauses live
        # autoscaling so incoming samples do not immediately undo the view.
        self.axes.callbacks.connect("xlim_changed", self._manual_limits_changed)
        self.axes.callbacks.connect("ylim_changed", self._manual_limits_changed)
        self.secondary_axes.callbacks.connect(
            "ylim_changed", self._manual_limits_changed
        )
        self._change_plot_mode()

    def _change_plot_mode(self, _event=None) -> None:
        """Switch the displayed artists without changing acquisition or data."""

        mode = self.plot_mode_var.get()
        if mode not in PLOT_MODES:
            mode = STRIP_RECORDER_MODE
            self.plot_mode_var.set(mode)
        strip_mode = mode == STRIP_RECORDER_MODE

        self.plot_line.set_visible(not strip_mode)
        self.drive_time_line.set_visible(strip_mode)
        self.current_time_line.set_visible(strip_mode)
        self.secondary_axes.set_visible(strip_mode)
        self._cursor_dragging = False

        if strip_mode:
            self.cursor_checkbutton.pack_forget()
            self._hide_measurement_cursor(clear_data=True)
            self.axes.set_title("Two-Channel Strip Recorder")
            self.axes.set_xlabel("Elapsed Time (s)")
            self.axes.set_ylabel("Tube Current (pA)", color="#145DA0")
            self.axes.tick_params(axis="y", colors="#145DA0")
            self.secondary_axes.set_ylabel("Drive Voltage (V)", color="#C43B3B")
            self.secondary_axes.tick_params(axis="y", colors="#C43B3B")
        else:
            if not self.cursor_checkbutton.winfo_manager():
                self.cursor_checkbutton.pack(side=tk.RIGHT, padx=(10, 2))
            self.axes.set_title("Franck-Hertz Characteristic")
            self.axes.set_xlabel("Drive Voltage (V)")
            self.axes.set_ylabel("Tube Current (pA)", color="black")
            self.axes.tick_params(axis="y", colors="black")

        self.autoscale_var.set(True)
        self._last_plotted_count = -1
        self.toolbar.update()
        self._redraw_plot(force=True)

    def _toggle_measurement_cursor(self) -> None:
        """Show or hide the measurement cursor in X-Y mode."""

        if self.cursor_visible_var.get():
            self._refresh_measurement_cursor()
        else:
            self._hide_measurement_cursor()
        self.canvas.draw_idle()

    def _refresh_measurement_cursor(self) -> None:
        """Snap the cursor and its readout to the nearest displayed XY point."""

        if (
            self.plot_mode_var.get() != XY_PLOT_MODE
            or not self.cursor_visible_var.get()
            or not self._cursor_x_values
        ):
            self._hide_measurement_cursor()
            return

        if self._cursor_target_x is None:
            self._cursor_target_x = self._cursor_x_values[-1]
        selected = nearest_xy_point(
            self._cursor_x_values,
            self._cursor_y_values,
            self._cursor_target_x,
        )
        if selected is None:
            self._hide_measurement_cursor()
            return

        x_value, y_value = selected
        self._cursor_selected_point = selected
        self.cursor_line.set_data([x_value, x_value], [0.0, 1.0])
        self.cursor_marker.set_data([x_value], [y_value])
        self.cursor_annotation.xy = (x_value, y_value)
        self.cursor_annotation.set_text(
            f"Drive Voltage = {x_value:.5g} V\nFH Current = {y_value:.5g} pA"
        )

        x_low, x_high = self.axes.get_xlim()
        y_low, y_high = self.axes.get_ylim()
        place_left = x_value > (x_low + x_high) / 2.0
        place_below = y_value > (y_low + y_high) / 2.0
        self.cursor_annotation.set_position(
            (-11 if place_left else 11, -11 if place_below else 11)
        )
        self.cursor_annotation.set_ha("right" if place_left else "left")
        self.cursor_annotation.set_va("top" if place_below else "bottom")

        self.cursor_line.set_visible(True)
        self.cursor_marker.set_visible(True)
        self.cursor_annotation.set_visible(True)

    def _hide_measurement_cursor(self, clear_data: bool = False) -> None:
        self.cursor_line.set_visible(False)
        self.cursor_marker.set_visible(False)
        self.cursor_annotation.set_visible(False)
        self._cursor_selected_point = None
        if clear_data:
            # The primary X axis is elapsed time in Strip Recorder mode.  Empty
            # cursor artists keep their previous drive-voltage coordinate from
            # participating in the time-axis relimit operation.
            self.cursor_line.set_data([], [])
            self.cursor_marker.set_data([], [])

    def _cursor_mouse_press(self, event) -> None:
        """Begin a drag only when the visible cursor line is grabbed."""

        if (
            event.button != 1
            or event.inaxes is not self.axes
            or self.plot_mode_var.get() != XY_PLOT_MODE
            or not self.cursor_line.get_visible()
            or self.toolbar.mode
            or event.x is None
        ):
            return
        x_data = self.cursor_line.get_xdata()
        if len(x_data) != 2:
            return
        cursor_pixel_x = self.axes.transData.transform((x_data[0], 0.0))[0]
        if abs(event.x - cursor_pixel_x) <= 8.0:
            self._cursor_dragging = True

    def _cursor_mouse_move(self, event) -> None:
        if not self._cursor_dragging or event.x is None:
            return
        if event.inaxes is self.axes and event.xdata is not None:
            target_x = event.xdata
        else:
            target_x = self.axes.transData.inverted().transform((event.x, 0.0))[0]
        self._cursor_target_x = float(target_x)
        self._refresh_measurement_cursor()
        self.canvas.draw_idle()

    def _cursor_mouse_release(self, _event) -> None:
        self._cursor_dragging = False

    def _refresh_ports(self) -> None:
        previous = self.port_combo.get()
        ports = [port.device for port in serial.tools.list_ports.comports()]
        values = ports + [config.SIMULATOR_PORT]
        self.port_combo.configure(values=values)
        if previous in values:
            self.port_combo.set(previous)
        elif ports:
            self.port_combo.set(ports[0])
        else:
            self.port_combo.set(config.SIMULATOR_PORT)

    def _toggle_connection(self) -> None:
        if self._port_open:
            self._disconnect("Disconnected")
        else:
            self._connect()

    def _connect(self) -> None:
        selected = self.port_combo.get().strip()
        if not selected:
            messagebox.showerror("No port selected", "Select an Arduino serial port.")
            return

        transport_class = (
            SimulatorTransport if selected == config.SIMULATOR_PORT else SerialTransport
        )
        transport = transport_class(
            on_line=lambda line: self._event_queue.put(("line", line)),
            on_error=lambda error: self._event_queue.put(("error", error)),
        )
        try:
            transport.open(selected, config.BAUD_RATE)
        except (ConnectionError, OSError) as exc:
            messagebox.showerror("Connection failed", str(exc))
            self._set_status(f"Connection failed: {exc}", "error")
            return

        self._transport = transport
        self._port_open = True
        self.controller.disconnect()
        self._hardware_banner_seen = False
        self._paired_capability_seen = False
        self._paired_mode_requested = False
        self._resume_after_protocol = False
        self.connect_button.configure(text="Disconnect")
        self.port_combo.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)
        self._set_led("waiting")
        self._set_status(
            f"Port open at {config.BAUD_RATE:,} baud; waiting for device handshake…",
            "waiting",
        )
        self._cancel_handshake_timeout()
        self._handshake_timeout_id = self.after(
            int(config.HANDSHAKE_TIMEOUT_SECONDS * 1000),
            self._handshake_timeout,
        )
        self._schedule_identity_probe(500)

    def _handshake_timeout(self) -> None:
        self._handshake_timeout_id = None
        if self._port_open and not self.controller.device_ready:
            self._set_led("error")
            if self._hardware_banner_seen:
                detail = (
                    "paired mode did not start"
                    if self._paired_capability_seen
                    else "paired-channel firmware was not found"
                )
                self._set_status(
                    f"Modern Lab shield detected, but {detail}. "
                    "Upload the current Frank_Herz_DAQ.ino to this Arduino.",
                    "error",
                )
            else:
                self._set_status(
                    "Port is open, but no Modern Lab Data Acquisition Shield responded.",
                    "error",
                )

    def _disconnect(self, status: str, error: bool = False) -> None:
        self._cancel_handshake_timeout()
        self._cancel_identity_probe()
        try:
            if self.controller.running:
                self.controller.stop()
        except ConnectionError:
            pass
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()
        self._port_open = False
        self.controller.disconnect()
        self._hardware_banner_seen = False
        self._paired_capability_seen = False
        self._paired_mode_requested = False
        self._resume_after_protocol = False
        self.connect_button.configure(text="Connect")
        self.port_combo.configure(state="readonly")
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)
        self._set_led("error" if error else "off")
        self._set_status(status, "error" if error else "neutral")

    def _write(self, data: bytes) -> None:
        if self._transport is None or not self._transport.is_open():
            raise ConnectionError("Arduino connection is not open.")
        self._transport.write(data)

    def _send_acquisition_settings(self) -> None:
        self._write(f"avg,{config.DEFAULT_AVERAGES}\n".encode("ascii"))
        self._write(
            f"delay,{config.DEFAULT_SAMPLE_INTERVAL_MS}\n".encode("ascii")
        )

    def _schedule_identity_probe(self, delay_ms: int = config.IDENTIFY_RETRY_MS) -> None:
        self._cancel_identity_probe()
        self._identity_probe_id = self.after(delay_ms, self._probe_identity)

    def _probe_identity(self) -> None:
        self._identity_probe_id = None
        if not self._port_open or self.controller.device_ready:
            return
        try:
            self._write(config.IDENTIFY_COMMAND)
        except ConnectionError as exc:
            self._event_queue.put(("error", f"Device identification failed: {exc}"))
            return
        self._schedule_identity_probe()

    def _start_acquisition(self) -> None:
        try:
            self.controller.start()
        except (ConnectionError, RuntimeError) as exc:
            self._event_queue.put(("error", f"Could not start acquisition: {exc}"))
            return
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._set_status("Acquiring paired drive-voltage and tube-current data…", "good")

    def _stop_acquisition(self) -> None:
        try:
            self.controller.stop()
        except ConnectionError as exc:
            self._event_queue.put(("error", f"Acquisition stopped after serial error: {exc}"))
            return
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)
        self._set_status(
            f"Acquisition paused; {len(self.controller.dataset):,} points retained.",
            "neutral",
        )

    def _clear_data(self) -> bool:
        cleared = self.controller.confirm_and_clear(
            lambda: messagebox.askyesno(
                "Permanently clear data?",
                "Are you sure you want to permanently clear the current data?",
                icon="warning",
                default="no",
            )
        )
        if not cleared:
            return False
        # Discard records that were received before the confirmed click but have
        # not yet crossed the UI queue. A cancelled clear never discards data.
        self._discard_queued_data_lines()
        self.autoscale_var.set(True)
        self._cursor_target_x = None
        self._last_plotted_count = -1
        self._redraw_plot(force=True)
        self._set_status("Current dataset and plot cleared.", "neutral")
        return True

    def _discard_queued_data_lines(self) -> None:
        retained: list[tuple[str, bytes | str]] = []
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            kind, payload = event
            is_data = kind == "line" and (
                (isinstance(payload, bytes) and payload.startswith(b"DATA,"))
                or (isinstance(payload, str) and payload.startswith("DATA,"))
            )
            if not is_data:
                retained.append(event)
        for event in retained:
            self._event_queue.put(event)

    def _export_data(self) -> Path | None:
        points = self.controller.dataset.snapshot()
        if not points:
            messagebox.showinfo("No data", "There is no collected data to export.")
            return None
        filename = f"franck_hertz_{datetime.now():%Y%m%d-%H%M%S}.xlsx"
        selected = filedialog.asksaveasfilename(
            title="Export Franck-Hertz data",
            defaultextension=".xlsx",
            filetypes=(("Excel workbook", "*.xlsx"),),
            initialfile=filename,
        )
        if not selected:
            return None
        try:
            row_count = export_xlsx(selected, points, self.controller.calibration)
        except (OSError, PermissionError, ValueError) as exc:
            messagebox.showerror("Export failed", str(exc))
            self._set_status(f"Excel export failed: {exc}", "error")
            return None
        destination = Path(selected)
        if destination.suffix.lower() != ".xlsx":
            destination = destination.with_suffix(".xlsx")
        self._set_status(
            f"Exported {row_count:,} points to {destination}",
            "good",
        )
        return destination

    def _process_events(self) -> None:
        if self._closing:
            return
        changed = False
        for _ in range(1000):
            try:
                kind, payload = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self._disconnect(str(payload), error=True)
                continue
            if kind != "line":
                continue
            text = (
                payload.decode("ascii", errors="replace").strip()
                if isinstance(payload, bytes)
                else payload.strip()
            )
            if text == config.HANDSHAKE_BANNER:
                self._handle_hardware_banner()
            elif text == config.PROTOCOL_CAPABILITY:
                self._handle_protocol_capability()
            elif text == config.PAIRED_MODE_ACK:
                self._handle_paired_mode_ack()
            elif text.startswith("ERR,"):
                self._set_status(f"Arduino reported: {text}", "error")
            elif text.startswith("#") or not text:
                continue
            else:
                before = len(self.controller.dataset)
                self.controller.ingest(text)
                changed = changed or len(self.controller.dataset) != before
                if self.controller.storage_full:
                    self.start_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                    self._set_status(
                        "Acquisition paused because the stored-point safety limit was reached.",
                        "error",
                    )

        if changed or len(self.controller.dataset) != self._last_plotted_count:
            self._redraw_plot()
        self.after(config.UI_UPDATE_MS, self._process_events)

    def _handle_hardware_banner(self) -> None:
        """Record the shared shield identity; paired capability is still required."""

        if self.controller.device_ready:
            self._resume_after_protocol = self.controller.running
            self.controller.disconnect()
            self.start_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.DISABLED)
        self._hardware_banner_seen = True
        self._paired_capability_seen = False
        self._paired_mode_requested = False
        self._set_led("waiting")
        self._set_status(
            "Modern Lab shield detected; verifying paired-channel firmware…",
            "waiting",
        )

    def _handle_protocol_capability(self) -> None:
        """Enable acquisition only after the paired record format is confirmed."""

        self._hardware_banner_seen = True
        self._paired_capability_seen = True
        if self.controller.device_ready or self._paired_mode_requested:
            return
        self._paired_mode_requested = True
        self._set_status(
            "Compatible firmware detected; selecting paired-channel mode…",
            "waiting",
        )
        try:
            self._write(config.PAIRED_MODE_COMMAND)
        except ConnectionError as exc:
            self._event_queue.put(("error", f"Mode selection failed: {exc}"))

    def _handle_paired_mode_ack(self) -> None:
        """Finish setup only after the firmware confirms paired mode."""

        if not self._paired_capability_seen:
            return
        resume_after_reset = self._resume_after_protocol
        self._resume_after_protocol = False
        self._paired_mode_requested = False
        self.controller.mark_device_ready()
        self._cancel_handshake_timeout()
        self._cancel_identity_probe()
        self._set_led("on")
        try:
            self._send_acquisition_settings()
            if resume_after_reset:
                self.controller.start()
        except ConnectionError as exc:
            self._event_queue.put(("error", f"Device setup failed: {exc}"))
            return
        self.start_button.configure(
            state=tk.DISABLED if self.controller.running else tk.NORMAL
        )
        self.stop_button.configure(
            state=tk.NORMAL if self.controller.running else tk.DISABLED
        )
        if resume_after_reset:
            self._set_status(
                "Arduino reset detected; acquisition settings restored and streaming resumed.",
                "waiting",
            )
        else:
            self._set_status("Arduino connected and ready.", "good")

    def _redraw_plot(self, force: bool = False) -> None:
        points = self.controller.dataset.snapshot()
        count = len(points)
        if not force and count == self._last_plotted_count:
            return
        self.count_var.set(f"{count:,} point" + ("" if count == 1 else "s"))
        self._last_plotted_count = count

        if self.plot_mode_var.get() == STRIP_RECORDER_MODE:
            time_values, drive_values, current_values = (
                downsample_for_strip_recorder(points)
            )
            self._cursor_x_values = []
            self._cursor_y_values = []
            self._hide_measurement_cursor(clear_data=True)
            self.plot_line.set_data([], [])
            self.drive_time_line.set_data(time_values, drive_values)
            self.current_time_line.set_data(time_values, current_values)
            if time_values:
                self.axes.relim()
                self.secondary_axes.relim()
                if self.autoscale_var.get():
                    self._apply_strip_autoscale(
                        time_values, drive_values, current_values
                    )
            else:
                self._reset_empty_plot()
        else:
            x_values, y_values = downsample_for_display(points)
            self._cursor_x_values = x_values
            self._cursor_y_values = y_values
            self.plot_line.set_data(x_values, y_values)
            self.drive_time_line.set_data([], [])
            self.current_time_line.set_data([], [])
            if x_values and y_values:
                self.axes.relim()
                self.secondary_axes.relim()
                if self.autoscale_var.get():
                    self._apply_autoscale(x_values, y_values)
                self._refresh_measurement_cursor()
            else:
                self._cursor_target_x = None
                self._hide_measurement_cursor(clear_data=True)
                self._reset_empty_plot()
        self.canvas.draw_idle()

    def _reset_empty_plot(self) -> None:
        """Restore predictable limits for an empty plot in either mode."""

        self._changing_plot_limits = True
        try:
            self.axes.set_xlim(0.0, 1.0)
            self.axes.set_ylim(0.0, 1.0)
            self.secondary_axes.set_ylim(0.0, 1.0)
            self.axes.set_autoscalex_on(True)
            self.axes.set_autoscaley_on(True)
            self.secondary_axes.set_autoscalex_on(True)
            self.secondary_axes.set_autoscaley_on(True)
        finally:
            self._changing_plot_limits = False

    def _apply_autoscale(
        self, x_values: list[float], y_values: list[float]
    ) -> None:
        """Fit both axes while keeping live autoscaling enabled."""

        self._changing_plot_limits = True
        try:
            self.axes.set_autoscalex_on(True)
            self.axes.set_autoscaley_on(True)
            self.axes.relim()
            self.axes.autoscale_view()
            self._pad_equal_range(x_values, self.axes.set_xlim)
            self._pad_equal_range(y_values, self.axes.set_ylim)
            # set_xlim/set_ylim disable autoscaling for a single-valued axis.
            self.axes.set_autoscalex_on(True)
            self.axes.set_autoscaley_on(True)
        finally:
            self._changing_plot_limits = False

    def _apply_strip_autoscale(
        self,
        time_values: list[float],
        drive_values: list[float],
        current_values: list[float],
    ) -> None:
        """Fit the shared time axis and each strip-recorder channel independently."""

        self._changing_plot_limits = True
        try:
            self.axes.set_autoscalex_on(True)
            self.axes.set_autoscaley_on(True)
            self.secondary_axes.set_autoscalex_on(True)
            self.secondary_axes.set_autoscaley_on(True)
            self.axes.relim()
            self.secondary_axes.relim()
            self.axes.autoscale_view()
            self.secondary_axes.autoscale_view(scalex=False, scaley=True)
            self._pad_equal_range(time_values, self.axes.set_xlim)
            self._pad_equal_range(current_values, self.axes.set_ylim)
            self._pad_equal_range(drive_values, self.secondary_axes.set_ylim)
            self.axes.set_autoscalex_on(True)
            self.axes.set_autoscaley_on(True)
            self.secondary_axes.set_autoscalex_on(True)
            self.secondary_axes.set_autoscaley_on(True)
        finally:
            self._changing_plot_limits = False

    def _manual_limits_changed(self, _axes) -> None:
        """Hold a user-selected pan/zoom instead of overwriting it live."""

        if self._changing_plot_limits:
            return
        self.autoscale_var.set(False)
        self.axes.set_autoscalex_on(False)
        self.axes.set_autoscaley_on(False)
        self.secondary_axes.set_autoscalex_on(False)
        self.secondary_axes.set_autoscaley_on(False)

    def _toggle_autoscale(self) -> None:
        if self.autoscale_var.get():
            self._home_plot()
        else:
            self.axes.set_autoscalex_on(False)
            self.axes.set_autoscaley_on(False)
            self.secondary_axes.set_autoscalex_on(False)
            self.secondary_axes.set_autoscaley_on(False)

    def _home_plot(self) -> None:
        """Fit the complete dataset and resume live autoscaling."""

        self.autoscale_var.set(True)
        self._redraw_plot(force=True)

    @staticmethod
    def _pad_equal_range(values: list[float], setter) -> None:
        low = min(values)
        high = max(values)
        if low == high:
            padding = max(1.0, abs(low) * 0.1)
            setter(low - padding, high + padding)

    def _set_led(self, state: str) -> None:
        colors = {
            "off": ("#777777", "#555555"),
            "waiting": ("#E6A700", "#A87800"),
            "on": ("#2EAD5B", "#18793A"),
            "error": ("#D64545", "#8E2525"),
        }
        fill, outline = colors[state]
        self.connection_led.itemconfigure(self._led, fill=fill, outline=outline)

    def _set_status(self, text: str, state: str) -> None:
        colors = {
            "neutral": "#555555",
            "waiting": "#9A6800",
            "good": "#18793A",
            "error": "#B22929",
        }
        self.status_var.set(text)
        self.status_label.configure(fg=colors[state])

    def _cancel_handshake_timeout(self) -> None:
        if self._handshake_timeout_id is None:
            return
        try:
            self.after_cancel(self._handshake_timeout_id)
        except tk.TclError:
            pass
        self._handshake_timeout_id = None

    def _cancel_identity_probe(self) -> None:
        if self._identity_probe_id is None:
            return
        try:
            self.after_cancel(self._identity_probe_id)
        except tk.TclError:
            pass
        self._identity_probe_id = None

    def _on_close(self) -> None:
        self._closing = True
        self._cancel_handshake_timeout()
        self._cancel_identity_probe()
        try:
            if self.controller.running:
                self.controller.stop()
        except ConnectionError:
            pass
        if self._transport is not None:
            self._transport.close()
        self.destroy()


def run_self_test() -> None:
    """Fast packaged-app diagnostic used for source and executable smoke tests."""

    calibration = Calibration()
    sent: list[bytes] = []
    controller = AcquisitionController(sent.append, calibration=calibration)
    controller.mark_device_ready()
    controller.start()
    controller.ingest("DATA,100,1600,3200,0.100000,0.200000")
    controller.stop()
    if len(controller.dataset) != 1:
        raise RuntimeError("Acquisition pipeline self-test failed.")
    path = Path.cwd() / ".frank_herz_self_test.xlsx"
    try:
        export_xlsx(path, controller.dataset.snapshot(), calibration)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Excel export self-test failed.")
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def run_gui_smoke_test() -> None:
    """Exercise the real GUI controls against the protocol simulator."""

    app = FranckHertzApp()
    app.port_combo.set(config.SIMULATOR_PORT)
    failures: list[str] = []

    def limits_include(values, limits) -> bool:
        if not values:
            return False
        low, high = sorted(limits)
        tolerance = max(1.0, abs(low), abs(high)) * 1e-9
        return min(values) >= low - tolerance and max(values) <= high + tolerance

    def connect() -> None:
        if app.plot_mode_var.get() != STRIP_RECORDER_MODE:
            failures.append("strip recorder was not the default plot mode")
        if tuple(app.plot_mode_combo.cget("values")) != PLOT_MODES:
            failures.append("plot-mode dropdown order was not strip recorder first")
        if app.axes.get_ylabel() != "Tube Current (pA)":
            failures.append("tube current was not on the default left strip axis")
        if app.secondary_axes.get_ylabel() != "Drive Voltage (V)":
            failures.append("drive voltage was not on the default right strip axis")
        if app.axes.get_legend() is not None:
            failures.append("strip mode displayed an unnecessary legend box")
        if app.cursor_checkbutton.winfo_manager():
            failures.append("measurement-cursor control was visible in strip mode")
        if app.cursor_line.get_visible() or app.cursor_annotation.get_visible():
            failures.append("measurement cursor was visible in strip mode")

        app.plot_mode_var.set(XY_PLOT_MODE)
        app._change_plot_mode()
        if app.axes.get_xlabel() != "Drive Voltage (V)":
            failures.append("X–Y mode could not be selected before acquisition")
        if app.cursor_checkbutton.winfo_manager() != "pack":
            failures.append("measurement-cursor control was missing from X–Y mode")
        app.plot_mode_var.set(STRIP_RECORDER_MODE)
        app._change_plot_mode()
        app._connect()

    def start() -> None:
        if not app.controller.device_ready:
            failures.append("simulator handshake was not processed")
        else:
            app._start_acquisition()

    def switch_plot_modes_while_running() -> None:
        before = app.controller.dataset.snapshot()
        if not before or not app.controller.running:
            failures.append("acquisition was not running before the plot-mode switch")
            return

        app.plot_mode_var.set(STRIP_RECORDER_MODE)
        app._change_plot_mode()
        if not app.controller.running or app.controller.dataset.snapshot() != before:
            failures.append("strip-mode selection changed acquisition or stored data")
        times = list(app.drive_time_line.get_xdata())
        drive_values = list(app.drive_time_line.get_ydata())
        current_values = list(app.current_time_line.get_ydata())
        if not times or len(times) != len(drive_values) or len(times) != len(
            current_values
        ):
            failures.append("strip mode did not display both channels against time")
        if not app.secondary_axes.get_visible():
            failures.append("strip-mode secondary axis was not visible")
        if app.axes.get_ylabel() != "Tube Current (pA)":
            failures.append("tube current was not on the left strip axis")
        if app.secondary_axes.get_ylabel() != "Drive Voltage (V)":
            failures.append("drive voltage was not on the right strip axis")
        if app.current_time_line.get_color() != "#145DA0":
            failures.append("tube-current strip trace was not blue")
        if app.drive_time_line.get_color() != "#C43B3B":
            failures.append("drive-voltage strip trace was not red")
        if app.cursor_checkbutton.winfo_manager() or app.cursor_line.get_visible():
            failures.append("measurement cursor remained available in strip mode")
        if not app.autoscale_var.get():
            failures.append("strip-mode live autoscaling was not enabled")
        if not limits_include(times, app.axes.get_xlim()):
            failures.append("strip-mode time autoscaling did not contain acquired data")
        if not limits_include(current_values, app.axes.get_ylim()):
            failures.append("strip-mode current autoscaling did not contain acquired data")
        if not limits_include(drive_values, app.secondary_axes.get_ylim()):
            failures.append("strip-mode drive autoscaling did not contain acquired data")

        app.plot_mode_var.set(XY_PLOT_MODE)
        app._change_plot_mode()
        if not app.controller.running or app.controller.dataset.snapshot() != before:
            failures.append("X–Y selection changed acquisition or stored data")
        if app.secondary_axes.get_visible() or app.axes.get_xlabel() != "Drive Voltage (V)":
            failures.append("X–Y plot was not restored after live switching")
        if not len(app.plot_line.get_xdata()) or not len(app.plot_line.get_ydata()):
            failures.append("X–Y data was not restored after live switching")
            return
        if app.cursor_checkbutton.winfo_manager() != "pack":
            failures.append("measurement-cursor control was not restored in X–Y mode")
        if not app.cursor_line.get_visible() or not app.cursor_annotation.get_visible():
            failures.append("measurement cursor did not appear on acquired X–Y data")
        if not app.autoscale_var.get():
            failures.append("X–Y live autoscaling was not enabled")

        x_values = list(app.plot_line.get_xdata())
        y_values = list(app.plot_line.get_ydata())
        if not limits_include(x_values, app.axes.get_xlim()):
            failures.append("X–Y drive autoscaling did not contain acquired data")
        if not limits_include(y_values, app.axes.get_ylim()):
            failures.append("X–Y current autoscaling did not contain acquired data")

        app.cursor_visible_var.set(False)
        app._toggle_measurement_cursor()
        if app.cursor_line.get_visible() or app.cursor_annotation.get_visible():
            failures.append("measurement-cursor control did not hide its artists")
        app.cursor_visible_var.set(True)
        app._toggle_measurement_cursor()
        if not app.cursor_line.get_visible() or not app.cursor_annotation.get_visible():
            failures.append("measurement-cursor control did not restore its artists")

        app.canvas.draw()
        current_x = app.cursor_line.get_xdata()[0]
        target_x = min(x_values) if current_x != min(x_values) else max(x_values)
        current_pixel_x = app.axes.transData.transform((current_x, 0.0))[0]
        app._cursor_mouse_press(
            SimpleNamespace(
                button=1,
                inaxes=app.axes,
                x=current_pixel_x,
                xdata=current_x,
            )
        )
        if not app._cursor_dragging:
            failures.append("measurement cursor could not be grabbed")
        target_pixel_x = app.axes.transData.transform((target_x, 0.0))[0]
        app._cursor_mouse_move(
            SimpleNamespace(
                inaxes=app.axes,
                x=target_pixel_x,
                xdata=target_x,
            )
        )
        app._cursor_mouse_release(SimpleNamespace(button=1))
        expected = nearest_xy_point(x_values, y_values, target_x)
        if app._cursor_selected_point != expected:
            failures.append("dragged cursor did not select the nearest plotted X–Y point")
        if app._cursor_dragging:
            failures.append("measurement cursor remained grabbed after mouse release")
        if "Drive Voltage" not in app.cursor_annotation.get_text() or (
            "FH Current" not in app.cursor_annotation.get_text()
        ):
            failures.append("measurement-cursor coordinate readout was incomplete")

    retained_after_stop = [0]

    def stop_and_check_retention() -> None:
        if not len(app.controller.dataset):
            failures.append("no simulated measurements reached the GUI")
        live_x = list(app.plot_line.get_xdata())
        live_y = list(app.plot_line.get_ydata())
        if not limits_include(live_x, app.axes.get_xlim()) or not limits_include(
            live_y, app.axes.get_ylim()
        ):
            failures.append("X–Y axes stopped autoscaling as new data arrived")
        app._stop_acquisition()
        retained_after_stop[0] = len(app.controller.dataset)
        if not retained_after_stop[0]:
            failures.append("stop erased the GUI dataset")

        if not app.autoscale_var.get():
            failures.append("live autoscaling was not enabled by default")
        manual_limits = (10.0, 20.0)
        app.axes.set_xlim(*manual_limits)
        if app.autoscale_var.get():
            failures.append("manual plot scaling did not pause live autoscaling")
        app._redraw_plot(force=True)
        if tuple(app.axes.get_xlim()) != manual_limits:
            failures.append("a live redraw overwrote manual plot limits")
        app.toolbar.home()
        if not app.autoscale_var.get():
            failures.append("Home did not restore live autoscaling")
        if tuple(app.axes.get_xlim()) == manual_limits:
            failures.append("Home did not fit the collected data")

        app._start_acquisition()

    def check_resume_and_reset() -> None:
        if len(app.controller.dataset) <= retained_after_stop[0]:
            failures.append("resume did not append to the existing GUI dataset")
        # Repeating identity + capability emulates an Arduino reset during a run.
        app._event_queue.put(("line", config.HANDSHAKE_BANNER.encode("ascii")))
        app._event_queue.put(("line", config.PROTOCOL_CAPABILITY.encode("ascii")))

    def check_clear_confirmation() -> None:
        app._stop_acquisition()
        before_clear = len(app.controller.dataset)
        original_askyesno = messagebox.askyesno
        try:
            messagebox.askyesno = lambda *args, **kwargs: False
            if app._clear_data() or len(app.controller.dataset) != before_clear:
                failures.append("cancelled clear changed the GUI dataset")
            messagebox.askyesno = lambda *args, **kwargs: True
            if not app._clear_data() or len(app.controller.dataset) != 0:
                failures.append("confirmed clear did not erase the GUI dataset")
        finally:
            messagebox.askyesno = original_askyesno

    def simulate_link_loss() -> None:
        transport = app._transport
        if isinstance(transport, SimulatorTransport):
            transport.simulate_disconnect()
        else:
            failures.append("GUI smoke test lost its simulator transport")

    def verify_disconnect_and_close() -> None:
        if app.controller.device_ready or app.controller.running:
            failures.append("connection loss did not stop the GUI acquisition state")
        if "interrupted" not in app.status_var.get().lower():
            failures.append("connection loss was not reported in GUI status")
        app._on_close()

    app.after(50, connect)
    app.after(250, start)
    app.after(550, switch_plot_modes_while_running)
    app.after(850, stop_and_check_retention)
    app.after(1250, check_resume_and_reset)
    app.after(1500, check_clear_confirmation)
    app.after(1650, simulate_link_loss)
    app.after(1850, verify_disconnect_and_close)
    app.mainloop()
    if failures:
        raise RuntimeError("; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Franck-Hertz data acquisition")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="run a non-GUI acquisition/export diagnostic and exit",
    )
    parser.add_argument(
        "--gui-smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    options = parser.parse_args(argv)
    if options.smoke_test:
        run_self_test()
        return 0
    if options.gui_smoke_test:
        run_gui_smoke_test()
        return 0
    FranckHertzApp().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
