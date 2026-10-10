"""Build the chemreactor main-a4 hub using the local verified runtime."""
import argparse
from pathlib import Path
from build_gateways import FILES, REVISION, find_tools, prepare_runtime, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--toolchain-bin", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = (args.out or root / "build/hub").resolve()
    platform = out / "platform"
    files = {name: sha for name, sha in FILES.items()
             if name not in ("ArduinoAdapter.hpp", "arduino_link.c")}
    prepare_runtime(root / "firmware/runtime", platform, files)
    if args.prepare_only:
        print("Verified and patched hub runtime in", platform)
        return
    tools = find_tools(args.toolchain_bin)
    elf = out / "sketch.elf"
    run([tools["arm-none-eabi-g++"], "-std=gnu++11", "-mcpu=cortex-m0plus",
         "-mthumb", "-DSTM32G030x6", '-D_FIR_GIT_REV="' + REVISION + '"',
         "-I", platform, "-T", platform / "tjc-ms1-b4272ba421624bbe-usercode.ld",
         platform / "startup_stm32g030xx.s", root / "firmware/hub/sketch.cpp",
         "--specs=nosys.specs", "-Os", "-g", "-fno-exceptions", "-fno-rtti",
         "-ffunction-sections", "-fdata-sections", "-fno-strict-aliasing",
         "-Wl,--gc-sections", "-Wl,-Map=" + str(out / "sketch.map"), "-o", elf])
    run([tools["arm-none-eabi-objcopy"], "-Obinary", elf, out / "sketch.bin"])
    run([tools["arm-none-eabi-size"], elf])


if __name__ == "__main__":
    main()
