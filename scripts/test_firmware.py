#!/usr/bin/env python3
"""Compile/run independent native firmware tests, then consume golden wire vectors."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    compiler = shutil.which("g++")
    if not compiler:
        raise SystemExit("g++ is required (for example: nix shell nixpkgs#gcc)")
    sources = [
        ROOT / "firmware/tests/protocol_test.cpp",
        ROOT.parent / "arduino-actuator/test/hardware_api_test.cpp",
        ROOT.parent / "arduino-sensor/test/hardware_api_test.cpp",
        ROOT.parent / "arduino-sensor/test/diagnostics_test.cpp",
        ROOT.parent / "arduino-sensor/test/bmp280_test.cpp",
        ROOT.parent / "arduino-sensor/test/dht11_test.cpp",
        ROOT.parent / "arduino-sensor/test/dust_test.cpp",
        ROOT.parent / "arduino-sensor/test/dht_gpio_test.cpp",
        ROOT.parent / "arduino-sensor/test/voc_sensor_test.cpp",
        ROOT.parent / "arduino-sensor/test/sensor_board_test.cpp",
        ROOT.parent / "arduino-sensor/test/softwarewire_test.cpp",
        ROOT / "firmware/tests/gateway_arduino_test.cpp",
        ROOT.parent / "tjc-sketch/tests/gateway_test.cpp",
        ROOT.parent / "tjc-sketch/tests/hub_test.cpp",
        ROOT / "firmware/tests/hub_gateway_arduino_test.cpp",
    ]
    canonical = (ROOT / "firmware/common/HardwareProtocol.h").read_bytes()
    for repository in ("arduino-actuator", "arduino-sensor", "tjc-sketch"):
        folder = "firmware" if repository == "tjc-sketch" else "src"
        header = ROOT.parent / repository / folder / "common/HardwareProtocol.h"
        if header.read_bytes() != canonical:
            raise SystemExit(f"common header differs in {repository}")
    with tempfile.TemporaryDirectory(prefix="hardware-api-tests-") as directory:
        binaries = []
        flags = ["-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic"]
        environment = os.environ.copy()
        if args.sanitize:
            flags += ["-fsanitize=address,undefined", "-g"]
            # LeakSanitizer cannot run under ptrace; tests allocate no device heap.
            environment["ASAN_OPTIONS"] = "detect_leaks=0"
        for index, source in enumerate(sources):
            binary = str(Path(directory) / f"suite-{index}")
            extra = []
            if source.name == "softwarewire_test.cpp":
                extra = ["-I" + str(source.parent / "mock"),
                         str(source.parents[1] / "src/softwarewire/SoftwareWire.cpp")]
            if source.name == "dht_gpio_test.cpp":
                extra = ["-I" + str(source.parent / "mock"),
                         str(source.parents[1] / "src/dht/DHT.cpp")]
            if source.name in ("voc_sensor_test.cpp", "sensor_board_test.cpp", "dust_test.cpp"):
                c_compiler = shutil.which("gcc")
                if not c_compiler:
                    raise SystemExit("gcc is required for the Sensirion C algorithm")
                algorithm = source.parents[1] / "src/sensirion/sensirion_gas_index_algorithm.c"
                object_file = str(Path(directory) / "sensirion.o")
                c_flags = [flag for flag in flags if not flag.startswith("-std=")]
                subprocess.run([c_compiler, "-std=c99", *c_flags, "-c", str(algorithm),
                                "-o", object_file], check=True)
                extra = [object_file]
            if source.name == "sensor_board_test.cpp":
                extra += ["-I" + str(source.parent / "mock"),
                          '-DHARDWARE_API_BOARD_HEADER="test/compile_only_sensors.h"']
            if source.name == "dust_test.cpp":
                extra += ["-I" + str(source.parent / "mock"),
                          '-DHARDWARE_API_BOARD_HEADER="test/compile_only_dust.h"']
            subprocess.run([compiler, *flags, *extra, str(source), "-o", binary], check=True)
            subprocess.run([binary], check=True, env=environment)
            if source.name == "dust_test.cpp":
                conflict = [arg for arg in extra if not arg.startswith("-DHARDWARE_API_BOARD_HEADER")]
                subprocess.run([compiler, *flags, *conflict,
                                '-DHARDWARE_API_BOARD_HEADER="test/conflicting_dust.h"',
                                "-DTEST_DUST_CONFLICT", str(source), "-o", binary + "-conflict"], check=True)
                subprocess.run([binary + "-conflict"], check=True, env=environment)
            if source.name == "sensor_board_test.cpp":
                conflict_binary = binary + "-conflict"
                conflict_extra = [arg for arg in extra if not arg.startswith("-DHARDWARE_API_BOARD_HEADER")]
                conflict_extra += ['-DHARDWARE_API_BOARD_HEADER="test/conflicting_sensors.h"',
                                   "-DTEST_CONFLICT", "-DTEST_EXPECT_BUS_COUNT=3"]
                subprocess.run([compiler, *flags, *conflict_extra, str(source),
                                "-o", conflict_binary], check=True)
                subprocess.run([conflict_binary], check=True, env=environment)
                unconfigured_binary = binary + "-unconfigured"
                subprocess.run([compiler, *flags, object_file,
                                "-I" + str(source.parent / "mock"), "-DTEST_UNCONFIGURED",
                                '-DHARDWARE_API_BOARD_HEADER="test/unconfigured.h"',
                                str(source), "-o", unconfigured_binary], check=True)
                subprocess.run([unconfigured_binary], check=True, env=environment)
                uno_binary = binary + "-uno-map"
                subprocess.run([compiler, *flags, object_file,
                                "-I" + str(source.parent / "mock"), "-DTEST_UNO_MAP",
                                str(source), "-o", uno_binary], check=True)
                subprocess.run([uno_binary], check=True, env=environment)
                for name, header, variant_flags in (
                    ("dht", "compile_only_dht.h", []),
                    ("dht-conflict", "conflicting_dht.h", ["-DTEST_DHT_CONFLICT"]),
                    ("dht-voc-conflict", "conflicting_dht_voc.h", ["-DTEST_DHT_VOC_CONFLICT"]),
                ):
                    variant_binary = binary + "-" + name
                    subprocess.run([compiler, *flags, object_file,
                                    "-I" + str(source.parent / "mock"),
                                    f'-DHARDWARE_API_BOARD_HEADER="test/{header}"',
                                    *variant_flags, str(source), "-o", variant_binary], check=True)
                    subprocess.run([variant_binary], check=True, env=environment)
            binaries.append(binary)
        vectors = json.loads((ROOT / "docs/protocol/hardware-v2-vectors.json").read_text())
        for case in vectors["round_trips"]:
            subprocess.run([binaries[0], case["request_hex"], case["response_hex"]],
                           check=True, env=environment)
        print(f"{len(vectors['round_trips'])} shared golden codec round trips passed.")
        print("Golden bytes are codec examples, NOT old hardware-map handler expectations.")


if __name__ == "__main__":
    main()
