#!/usr/bin/env python3
"""SimpleBus serial terminal for communicating with a board.

Protocol:
    [CR] [addr1] [addr2] [data] [LF]

    [addr1] – старший полу-байт адреса в HEX-представлении
    [addr2] – младший полу-байт адреса в HEX-представлении
    [data] – символ полезной нагрузки
    [CR] [LF] – соответствующие управляющие символы

Computer address is fixed to 0x99.
"""

import argparse
import sys

try:
    import serial
except ImportError:  # pragma: no cover
    print("Missing dependency: pyserial. Install with `pip install pyserial`.")
    sys.exit(1)

try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None

import simplebus_protocol as proto
import time
from collections import deque


_FIG = None
_AX = None
_LINE = None
_START_TIME = None
_XS = deque()
_YS = deque()
_MAX_POINTS = 2000
_WINDOW = 10.0
_LAST_PLOT = 0.0
_PLOT_INTERVAL = 0.05

def setup_plot() -> None:
    global _FIG, _AX, _LINE, _START_TIME, _XS, _YS, _LAST_PLOT
    if plt is None:
        return
    plt.ion()
    _FIG, _AX = plt.subplots()
    _LINE, = _AX.plot([], [], '-')
    _AX.set_title('Captured data')
    _AX.set_xlabel('time (s)')
    _AX.set_ylabel('value')
    _START_TIME = time.time()
    _XS.clear()
    _YS.clear()
    _LAST_PLOT = 0.0


def update_plot(xs, ys) -> None:
    global _LAST_PLOT
    if plt is None or _FIG is None:
        return
    now = time.time()
    if now - _LAST_PLOT < _PLOT_INTERVAL:
        return
    _LAST_PLOT = now
    _LINE.set_xdata(xs)
    _LINE.set_ydata(ys)
    # set window on x axis
    try:
        if len(xs) > 0:
            _AX.set_xlim(max(xs[0], xs[-1] - _WINDOW), xs[-1])
    except Exception:
        pass
    _AX.relim()
    _AX.autoscale_view()
    _FIG.canvas.draw_idle()
    _FIG.canvas.flush_events()


def plot_buffer(buf: bytes) -> None:
    if plt is None:
        print("matplotlib not available; can't plot")
        return
    # static final plot using index as time when no timestamps available
    fig, ax = plt.subplots()
    ax.plot(list(range(len(buf))), [b for b in buf], '-o')
    ax.set_title('Captured data (final)')
    ax.set_xlabel('sample')
    ax.set_ylabel('value')
    plt.show()


def read_packet(ser: serial.Serial, timeout: float | None = None) -> bytes:
    ser.timeout = timeout
    packet = bytearray()
    while True:
        byte = ser.read(1)
        if not byte:
            raise TimeoutError("Timeout waiting for packet.")
        packet += byte
        if packet.endswith(proto.LF):
            break
    return bytes(packet)


def send_packet(ser: serial.Serial, address: int, data: str) -> None:
    packet = proto.encode_packet(address, data)
    ser.write(packet)
    ser.flush()
    print(f"Sent: {packet!r}")


def interactive_loop(ser: serial.Serial, target_addr: int, collector: proto.DataCollector) -> None:
    print("SimpleBus interactive mode")
    print("Type a single character to send, or Ctrl-C to exit.")
    try:
        while True:
            payload = input("> ").strip()
            if not payload:
                continue
            if len(payload) != 1:
                print("Please enter exactly one character.")
                continue
            send_packet(ser, target_addr, payload)
            try:
                reply = read_packet(ser, timeout=2.0)
                addr, data = proto.decode_packet(reply)
                # print(f"Received packet from {addr:02X}: data={data!r} bytes={data.hex().upper()}")
                if addr == proto.COMPUTER_ADDR:
                    # feed marker-based collector (if used)
                    b = data[0]
                    finished_capture = collector.process_byte(b)
                    # only record timeseries samples for non-marker bytes
                    if b != proto.MARKER:
                        ts = time.time()
                        if _START_TIME is None:
                            # safety: set start if not initialized
                            setup_plot()
                        t = ts - _START_TIME if _START_TIME is not None else 0.0
                        _XS.append(t)
                        _YS.append(b)
                        # keep arrays bounded by count
                        while len(_XS) > _MAX_POINTS:
                            _XS.popleft()
                            _YS.popleft()
                        # trim older than window
                        while _XS and (_XS[0] < t - _WINDOW):
                            _XS.popleft()
                            _YS.popleft()
                        if plt is not None:
                            try:
                                update_plot(_XS, _YS)
                            except Exception:
                                pass
                    finished = finished_capture
            except TimeoutError:
                print("No reply received.")
            except ValueError as exc:
                print(f"Unable to decode reply: {exc}")
    except KeyboardInterrupt:
        print("\nExiting interactive mode.")


def main() -> int:
    parser = argparse.ArgumentParser(description="SimpleBus serial communication script.")
    parser.add_argument("--port", required=True, help="Serial port name, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    parser.add_argument("--target", type=lambda v: int(v, 0), default=0x00, help="Target address to send to (hex or decimal). Default 0x00.")
    parser.add_argument("--data", help="Single character payload to send.")
    parser.add_argument("--recv", action="store_true", help="Receive packets and process captures.")
    parser.add_argument("--once", action="store_true", help="Exit after the first completed capture (only with --recv).")
    parser.add_argument("--timeout", type=float, default=2.0, help="Read timeout in seconds (default: 2.0)")
    parser.add_argument("--window", type=float, default=10.0, help="Time window (seconds) shown on the live plot")
    args = parser.parse_args()

    print(f"Available serial ports: {proto.list_serial_ports()}")
    print(f"Using computer address: {proto.COMPUTER_ADDR:02X}")
    print(f"Opening port {args.port} at {args.baud} baud")

    # apply runtime window
    global _WINDOW
    _WINDOW = float(args.window)

    try:
        with serial.Serial(args.port, baudrate=args.baud, timeout=args.timeout) as ser:
            if args.recv:
                collector = proto.DataCollector()
                # prepare live plot if available
                if plt is not None:
                    setup_plot()
                last_len = -1
                # continuous loop: process captures repeatedly until interrupted
                while True:
                    packet = read_packet(ser, timeout=args.timeout)
                    addr, data = proto.decode_packet(packet)
                    print(f"Received packet: address={addr:02X}, data={data!r}, bytes={data.hex().upper()}")
                    if addr == proto.COMPUTER_ADDR:
                        b = data[0]
                        finished_capture = collector.process_byte(b)
                        # only append non-marker bytes to timeseries
                        if b != proto.MARKER:
                            ts = time.time()
                            if _START_TIME is None:
                                setup_plot()
                            t = ts - _START_TIME if _START_TIME is not None else 0.0
                            _XS.append(t)
                            _YS.append(b)
                            while len(_XS) > _MAX_POINTS:
                                _XS.popleft()
                                _YS.popleft()
                            while _XS and (_XS[0] < t - _WINDOW):
                                _XS.popleft()
                                _YS.popleft()
                            if plt is not None:
                                try:
                                    update_plot(_XS, _YS)
                                except Exception:
                                    pass
                        if finished_capture:
                            buf = collector.get_buffer()
                            print(f"Final capture ({len(buf)} bytes): {buf.hex().upper()}")
                            if plt is None:
                                plot_buffer(buf)
                            if args.once:
                                return 0
                            collector.reset()
                            # continue receiving

            if args.data is not None:
                if len(args.data) != 1:
                    raise ValueError("--data requires a single character.")
                send_packet(ser, args.target, args.data)
                return 0

            collector = proto.DataCollector()
            if plt is not None:
                setup_plot()
            interactive_loop(ser, args.target, collector)
            return 0
    except serial.SerialException as exc:
        print(f"Serial error: {exc}")
        return 2
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
