#!/usr/bin/env python3
"""PyQt monitor for telemetry frames received through SimpleBus.

Telemetry format, between two START_END_SYM bytes:
    [hall] [reed switch] [inside temperature whole] [inside temperature fraction]
    [outside temperature whole] [outside temperature fraction]
    [RPM1 whole] [RPM1 fraction] [RPM2 whole] [RPM2 fraction]
    [RPM3 whole] [RPM3 fraction] [RPM4 whole] [RPM4 fraction]
    [reference temperature] [door stage]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque

import serial
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import simplebus_protocol as proto


MAX_POINTS = 1000
MA_WINDOW = 7
MAX_FAN_RPM = 5000
REFRESH_PORTS_TEXT = "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u043f\u043e\u0440\u0442\u044b"
CONNECT_TEXT = "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c\u0441\u044f"
DISCONNECT_TEXT = "\u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c\u0441\u044f"


class TelemetryDecoder:
    """Decode temperatures, three RPM values, and the door stage."""

    def decode_capture(self, payload: bytes) -> tuple[int, int, float, float, int, int, int, int, float, int] | None:
        if len(payload) != 16:
            return None

        (
            hall,
            reed,
            inside_whole,
            inside_fraction,
            outside_whole,
            outside_fraction,
            rpm1_whole,
            rpm1_fraction,
            rpm2_whole,
            rpm2_fraction,
            rpm3_whole,
            rpm3_fraction,
            rpm4_whole,
            rpm4_fraction,
            reference_byte,
            door_stage,
        ) = payload
        inside_temperature = self._decode_temperature(inside_whole, inside_fraction)
        outside_temperature = self._decode_temperature(outside_whole, outside_fraction)
        rpm1 = self._decode_rpm(rpm1_whole, rpm1_fraction)
        rpm2 = self._decode_rpm(rpm2_whole, rpm2_fraction)
        rpm3 = self._decode_rpm(rpm3_whole, rpm3_fraction)
        rpm4 = self._decode_rpm(rpm4_whole, rpm4_fraction)
        reference_temperature = self._decode_integer_temperature(reference_byte)
        return (
            int(bool(hall)),
            int(bool(reed)),
            inside_temperature,
            outside_temperature,
            rpm1,
            rpm2,
            rpm3,
            rpm4,
            reference_temperature,
            door_stage,
        )

    @staticmethod
    def _decode_temperature(whole_byte: int, normalized_fraction: int) -> float:
        whole_part = TelemetryDecoder._decode_integer_temperature(whole_byte)
        hundredths = round(normalized_fraction * 99 / 255)
        return whole_part + hundredths / 100

    @staticmethod
    def _decode_integer_temperature(value: int) -> int:
        return value if value < 128 else value - 256

    @staticmethod
    def _decode_rpm(whole_byte: int, fraction_byte: int) -> int:
        encoded = (whole_byte << 8) | fraction_byte
        return (encoded * MAX_FAN_RPM + 32767) // 65535


class TelemetryFrameCollector:
    """Collect a fixed 16-byte payload wrapped by START_END_SYM."""

    def __init__(self, marker: int = proto.MARKER, payload_size: int = 16) -> None:
        self.marker = marker
        self.payload_size = payload_size
        self.capturing = False
        self._buffer = bytearray()

    def process_byte(self, value: int) -> bool:
        if not self.capturing:
            if value == self.marker:
                self.capturing = True
                self._buffer.clear()
            return False

        # A payload byte may equal the marker, so read the known payload size
        # before checking for the closing marker.
        if len(self._buffer) < self.payload_size:
            self._buffer.append(value)
            return False

        if value == self.marker:
            self.capturing = False
            return True

        self.capturing = False
        self._buffer.clear()
        return False

    def get_buffer(self) -> bytes:
        return bytes(self._buffer)

    def reset(self) -> None:
        self.capturing = False
        self._buffer.clear()


class TelemetryStore:
    """Keep the latest readings and calculate MA(7) for both temperatures."""

    def __init__(self) -> None:
        self.times: deque[float] = deque()
        self.inside_temperatures: deque[float] = deque()
        self.outside_temperatures: deque[float] = deque()
        self.inside_temperature_ma: deque[float] = deque()
        self.outside_temperature_ma: deque[float] = deque()
        self.reference_temperatures: deque[float] = deque()
        self.rpm1: deque[int] = deque()
        self.rpm2: deque[int] = deque()
        self.rpm3: deque[int] = deque()
        self.rpm4: deque[int] = deque()
        self.door_stages: deque[int] = deque()
        self._inside_ma_values: deque[float] = deque()
        self._outside_ma_values: deque[float] = deque()
        self._inside_ma_sum = 0.0
        self._outside_ma_sum = 0.0
        self._started_at = time.monotonic()

    def add(
        self,
        inside_temperature: float,
        outside_temperature: float,
        rpm1: int,
        rpm2: int,
        rpm3: int,
        rpm4: int,
        reference_temperature: float,
        door_stage: int,
    ) -> None:
        self.times.append(time.monotonic() - self._started_at)
        self.inside_temperatures.append(inside_temperature)
        self.outside_temperatures.append(outside_temperature)
        self.rpm1.append(rpm1)
        self.rpm2.append(rpm2)
        self.rpm3.append(rpm3)
        self.rpm4.append(rpm4)
        self.reference_temperatures.append(reference_temperature)
        self.door_stages.append(door_stage)

        self._inside_ma_values.append(inside_temperature)
        self._inside_ma_sum += inside_temperature
        if len(self._inside_ma_values) > MA_WINDOW:
            self._inside_ma_sum -= self._inside_ma_values.popleft()
        self.inside_temperature_ma.append(self._inside_ma_sum / len(self._inside_ma_values))

        self._outside_ma_values.append(outside_temperature)
        self._outside_ma_sum += outside_temperature
        if len(self._outside_ma_values) > MA_WINDOW:
            self._outside_ma_sum -= self._outside_ma_values.popleft()
        self.outside_temperature_ma.append(self._outside_ma_sum / len(self._outside_ma_values))

        while len(self.times) > MAX_POINTS:
            self.times.popleft()
            self.inside_temperatures.popleft()
            self.outside_temperatures.popleft()
            self.inside_temperature_ma.popleft()
            self.outside_temperature_ma.popleft()
            self.rpm1.popleft()
            self.rpm2.popleft()
            self.rpm3.popleft()
            self.rpm4.popleft()
            self.reference_temperatures.popleft()
            self.door_stages.popleft()


class TemperatureWindow(QMainWindow):
    """Qt window containing the temperature graph and its stage background."""

    def __init__(self, baud_rate: int, initial_port: str | None = None) -> None:
        super().__init__()
        self.baud_rate = baud_rate
        self.monitor: SerialMonitor | None = None
        self.setWindowTitle("Монитор температуры")
        self.resize(1100, 650)

        figure = Figure(constrained_layout=True)
        self.canvas = FigureCanvas(figure)
        grid = figure.add_gridspec(8, 2)
        self.inside_ax = figure.add_subplot(grid[:4, 0])
        self.outside_ax = figure.add_subplot(grid[4:, 0], sharex=self.inside_ax)
        self.rpm1_ax = figure.add_subplot(grid[:2, 1], sharex=self.inside_ax)
        self.rpm2_ax = figure.add_subplot(grid[2:4, 1], sharex=self.inside_ax)
        self.rpm3_ax = figure.add_subplot(grid[4:6, 1], sharex=self.inside_ax)
        self.rpm4_ax = figure.add_subplot(grid[6:, 1], sharex=self.inside_ax)
        # self.ax is kept as an alias for the inside graph and its stage shading.
        self.ax = self.inside_ax
        self.inside_line, = self.inside_ax.plot(
            [], [], color="tab:red", linewidth=1.5, label="Inside stand MA(7)"
        )
        self.reference_line = self.inside_ax.axhline(
            0, color="black", linestyle="--", linewidth=1.2, label="Reference temperature"
        )
        self.outside_line, = self.outside_ax.plot(
            [], [], color="tab:blue", linewidth=1.5, label="Outside MA(7)"
        )
        self.rpm1_line, = self.rpm1_ax.plot([], [], color="tab:green", linewidth=1.2, label="Cooler 1")
        self.rpm2_line, = self.rpm2_ax.plot([], [], color="tab:orange", linewidth=1.2, label="Cooler 2")
        self.rpm3_line, = self.rpm3_ax.plot([], [], color="tab:purple", linewidth=1.2, label="Cooler 3")
        self.rpm4_line, = self.rpm4_ax.plot([], [], color="tab:brown", linewidth=1.2, label="Cooler 4")
        self.ax.set_title("Temperature")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Temperature (°C)")
        self.ax.grid(True, alpha=0.25)
        self.ax.legend(loc="upper left")
        self.inside_ax.set_title("Temperature inside stand")
        self.inside_ax.set_ylabel("Temperature (C)")
        self.inside_ax.set_xlabel("")
        self.outside_ax.set_title("Temperature outside")
        self.outside_ax.set_xlabel("Time (s)")
        self.outside_ax.set_ylabel("Temperature (C)")
        self.outside_ax.grid(True, alpha=0.25)
        self.outside_ax.legend(loc="upper left")
        for axis, title, maximum_rpm in (
            (self.rpm1_ax, "Cooler 1 RPM", 3000),
            (self.rpm2_ax, "Cooler 2 RPM", 3000),
            (self.rpm3_ax, "Cooler 3 RPM", 3000),
            (self.rpm4_ax, "Cooler 4 RPM", 5000),
        ):
            axis.set_title(title)
            axis.set_ylabel("RPM")
            axis.set_ylim(0, maximum_rpm)
            axis.grid(True, alpha=0.25)
            axis.legend(loc="upper left")
        self.rpm4_ax.set_xlabel("Time (s)")
        self._stage_patches = []

        central = QWidget()
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("Обновить порты")
        self.connect_button = QPushButton("Подключиться")
        self.refresh_button.setText(REFRESH_PORTS_TEXT)
        self.connect_button.setText(CONNECT_TEXT)
        controls.addWidget(self.port_combo)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.connect_button)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(self.canvas)
        self.setCentralWidget(central)
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.refresh_ports()
        if initial_port:
            index = self.port_combo.findData(initial_port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.statusBar().showMessage("Выберите порт и нажмите «Подключиться».")

    def refresh_ports(self) -> None:
        current_port = self.port_combo.currentData()
        ports = proto.list_serial_ports()
        self.port_combo.clear()
        for port in ports:
            self.port_combo.addItem(port, port)
        if current_port:
            index = self.port_combo.findData(current_port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.connect_button.setEnabled(bool(ports))
        if not ports:
            self.statusBar().showMessage("Последовательные порты не найдены.")

    def toggle_connection(self) -> None:
        if self.monitor is not None:
            self.monitor.close()
            self.monitor = None
            QTimer.singleShot(0, lambda: self.connect_button.setText(CONNECT_TEXT))
            self.port_combo.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.connect_button.setText("Подключиться")
            self.statusBar().showMessage("Отключено.")
            return

        port = self.port_combo.currentData()
        if not port:
            return
        try:
            device = serial.Serial(port, baudrate=self.baud_rate, timeout=0)
        except serial.SerialException as exc:
            QMessageBox.critical(self, "Ошибка подключения", str(exc))
            return

        self.monitor = SerialMonitor(device, self)
        QTimer.singleShot(0, lambda: self.connect_button.setText(DISCONNECT_TEXT))
        self.port_combo.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.connect_button.setText("Отключиться")
        self.statusBar().showMessage(f"Connected to {port} at {self.baud_rate} baud.")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.monitor is not None:
            self.monitor.close()
        super().closeEvent(event)

    @staticmethod
    def _stage_color(stage: int) -> str | None:
        if stage > 5:
            return "#ff4d4d"
        if stage > 2:
            return "#ffe45c"
        return None

    @staticmethod
    def _right_edge(times: list[float]) -> float:
        if len(times) < 2:
            return times[-1] + 1.0
        return times[-1] + max(0.001, times[-1] - times[-2])

    def _draw_stage_background(self, times: list[float], stages: list[int]) -> None:
        for patch in self._stage_patches:
            patch.remove()
        self._stage_patches.clear()

        start_index = 0
        current_color = self._stage_color(stages[0])
        for index in range(1, len(stages) + 1):
            next_color = self._stage_color(stages[index]) if index < len(stages) else None
            if index == len(stages) or next_color != current_color:
                if current_color is not None:
                    end_time = times[index] if index < len(times) else self._right_edge(times)
                    patch = self.ax.axvspan(
                        times[start_index], end_time, color=current_color, alpha=0.35, zorder=0
                    )
                    self._stage_patches.append(patch)
                start_index = index
                current_color = next_color

    def update_telemetry(
        self,
        store: TelemetryStore,
        inside_temperature: float,
        outside_temperature: float,
        rpm1: int,
        rpm2: int,
        rpm3: int,
        rpm4: int,
        reference_temperature: float,
        door_stage: int,
    ) -> None:
        times = list(store.times)
        inside_temperatures = list(store.inside_temperature_ma)
        outside_temperatures = list(store.outside_temperature_ma)
        rpm1_values = list(store.rpm1)
        rpm2_values = list(store.rpm2)
        rpm3_values = list(store.rpm3)
        rpm4_values = list(store.rpm4)
        stages = list(store.door_stages)
        if not times:
            return

        self.inside_line.set_data(times, inside_temperatures)
        self.outside_line.set_data(times, outside_temperatures)
        self.rpm1_line.set_data(times, rpm1_values)
        self.rpm2_line.set_data(times, rpm2_values)
        self.rpm3_line.set_data(times, rpm3_values)
        self.rpm4_line.set_data(times, rpm4_values)
        self.reference_line.set_ydata([reference_temperature, reference_temperature])
        self._draw_stage_background(times, stages)
        self.ax.set_xlim(times[0], self._right_edge(times))
        self.ax.relim()
        self.ax.autoscale_view(scalex=False, scaley=True)
        self.outside_ax.relim()
        self.outside_ax.autoscale_view(scalex=False, scaley=True)
        temperature = inside_temperature
        status_message = (
            f"Inside: {inside_temperature:.2f} C   |   Outside: {outside_temperature:.2f} C   |   "
            f"Reference: {reference_temperature:.2f} C   |   Door stage: {door_stage}   |   "
            f"Samples: {len(times)}"
        )
        QTimer.singleShot(0, lambda: self.statusBar().showMessage(status_message))
        self.statusBar().showMessage(
            f"Temperature: {temperature:.2f} °C   |   Door stage: {door_stage}   |   Samples: {len(times)}"
        )
        self.canvas.draw_idle()


class SerialMonitor(QObject):
    """Read SimpleBus packets without blocking the Qt event loop."""

    def __init__(self, device: serial.Serial, window: TemperatureWindow) -> None:
        super().__init__(window)
        self.device = device
        self.window = window
        self.decoder = TelemetryDecoder()
        self.collector = TelemetryFrameCollector()
        self.store = TelemetryStore()
        self._packet_buffer = bytearray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(10)

    def poll(self) -> None:
        try:
            waiting = self.device.in_waiting
            if not waiting:
                return
            self._packet_buffer.extend(self.device.read(waiting))
            self._process_packets()
        except serial.SerialException as exc:
            self.timer.stop()
            self.window.statusBar().showMessage(f"Serial error: {exc}")

    def _process_packets(self) -> None:
        while True:
            start = self._packet_buffer.find(proto.CR)
            if start < 0:
                self._packet_buffer.clear()
                return
            if start > 0:
                del self._packet_buffer[:start]
            if len(self._packet_buffer) < 5:
                return

            packet = bytes(self._packet_buffer[:5])
            if not packet.endswith(proto.LF):
                del self._packet_buffer[0]
                continue
            del self._packet_buffer[:5]

            try:
                address, data = proto.decode_packet(packet)
            except ValueError:
                continue
            if address != proto.COMPUTER_ADDR:
                continue

            if self.collector.process_byte(data[0]):
                payload = self.collector.get_buffer()
                sample = self.decoder.decode_capture(payload)
                self.collector.reset()
                if sample is None:
                    self.window.statusBar().showMessage(
                        f"Unexpected telemetry payload: {payload.hex().upper()}"
                    )
                    continue

                (
                    hall,
                    reed,
                    inside_temperature,
                    outside_temperature,
                    rpm1,
                    rpm2,
                    rpm3,
                    rpm4,
                    reference_temperature,
                    door_stage,
                ) = sample
                print(
                    f"Telemetry: hall={hall}, reed={reed}, inside={inside_temperature:.2f} C, "
                    f"outside={outside_temperature:.2f} C, rpm=({rpm1}, {rpm2}, {rpm3}, {rpm4}), "
                    f"reference={reference_temperature:.2f} C, door_stage={door_stage}"
                )
                self.store.add(
                    inside_temperature,
                    outside_temperature,
                    rpm1,
                    rpm2,
                    rpm3,
                    rpm4,
                    reference_temperature,
                    door_stage,
                )
                self.window.update_telemetry(
                    self.store,
                    inside_temperature,
                    outside_temperature,
                    rpm1,
                    rpm2,
                    rpm3,
                    rpm4,
                    reference_temperature,
                    door_stage,
                )

    def close(self) -> None:
        self.timer.stop()
        if self.device.is_open:
            self.device.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SimpleBus PyQt temperature monitor.")
    parser.add_argument("--port", help="Initially selected serial port, e.g. COM13")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = TemperatureWindow(args.baud, args.port)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
