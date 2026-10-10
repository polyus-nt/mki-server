"""Native regression checks; needs g++ (or --cxx) and Python 3."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def compile_test(source, out, cxx="g++", extra=()):
    subprocess.run([cxx, "-std=c++11", "-Wall", "-Wextra", *extra,
                    str(source), "-o", str(out)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = (root / "firmware/common/HardwareProtocol.h").read_bytes()
    for name in ("arduino-sensors", "arduino-actuator"):
        assert (root / name / "src/common/HardwareProtocol.h").read_bytes() == protocol
    with tempfile.TemporaryDirectory(prefix="chem-firmware-") as directory:
        for name in ("firmware_chain", "actuator_board_test", "sensor_board_test", "bmp280_test", "dht11_test"):
            binary = Path(directory) / (name + ".exe")
            extra = ("-I", str(root / "tests/actuator-stubs")) if name == "actuator_board_test" else ()
            if name == "sensor_board_test":
                extra = ("-I", str(root / "tests/sensor-stubs"))
            compile_test(root / "tests" / (name + ".cpp"), binary, args.cxx, extra)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
