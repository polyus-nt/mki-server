#!/usr/bin/env python3
"""Build hub and gateways from the local runtime (Windows/Linux/macOS)."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("all", "hub", "gateways", "buttons", "lmp8"), default="all")
    parser.add_argument("--toolchain-bin", type=Path, help="ARM GCC bin directory, otherwise PATH")
    parser.add_argument("--out", type=Path, help="build output root, otherwise build/")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    scripts = Path(__file__).resolve().parent
    targets = ("hub", "gateways", "buttons", "lmp8") if args.target == "all" else (args.target,)
    for target in targets:
        builder = target
        command = [sys.executable, str(scripts / f"build_{builder}.py")]
        if args.toolchain_bin:
            command += ["--toolchain-bin", str(args.toolchain_bin.resolve())]
        if args.out:
            command += ["--out", str(args.out.resolve() / target)]
        if args.prepare_only:
            command += ["--prepare-only"]
        subprocess.run(command, check=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
