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
        ROOT.parent / "arduino-sensor/test/bmp280_test.cpp",
        ROOT.parent / "arduino-sensor/test/softwarewire_test.cpp",
        ROOT / "firmware/tests/gateway_arduino_test.cpp",
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
            subprocess.run([compiler, *flags, *extra, str(source), "-o", binary], check=True)
            subprocess.run([binary], check=True, env=environment)
            binaries.append(binary)
        vectors = json.loads((ROOT / "docs/protocol/hardware-v1-vectors.json").read_text())
        for case in vectors["round_trips"]:
            subprocess.run([binaries[0], case["request_hex"], case["response_hex"]],
                           check=True, env=environment)
        print(f"{len(vectors['round_trips'])} shared golden codec round trips passed.")
        print("Golden bytes are codec examples, NOT old hardware-map handler expectations.")


if __name__ == "__main__":
    main()
