#!/usr/bin/env python3
"""Build standalone lmp8 usercode, with safely inactive active-low outputs."""
import argparse
from pathlib import Path
from build_gateways import FILES, REVISION, find_tools, prepare_runtime, replace_once, run

LED_FILES = {
    "UART.hpp": "12bf84a3687f2838944acad694746b505e5798a2e4ecbcad229c1093fe1b3cbf",
    "system.c": "a04e5eb8b55814c272930d279b1884dfa491c723d1b96058ef9e7ac875da3928",
    "LED.h": "1324d384516ca36349a47394200138ea1b8e5ff9414f8090c15f24f53a26a436",
    "LEDController.hpp": "16a2441e6133ea4a9ab7e7cd07328dcd6baed943ed6796b5b6e9ccbc653b99e7",
    "const.h": "97991054de8cee12825d04b00822d0348e64953d95f81119447cd941dcc01e38",
}


def patch_controller(text):
    # Set each output's inactive latch BEFORE switching MODER to output mode.
    # Do not call upstream initPin_PP: it first enables a default LOW latch.
    old = """            initPin_PP(leds[i].port, leds[i].pin);
            setPin_PP(leds[i].port, leds[i].pin, OFF);"""
    text = replace_once(text, '#include "const.h"', '#include "const.h"\n#include "BoardConfig.h"')
    new = """            const uint32_t pin = leds[i].pin;
            GPIO_TypeDef* port = leds[i].port;
            const bool high = tjc_lmp8::inactiveHigh(i);
            port->BSRR = 1UL << (high ? pin : pin + 16);  // latch BEFORE output mode
            port->PUPDR &= ~(3UL << (pin * 2));
            port->OTYPER &= ~(1UL << pin);
            port->MODER = (port->MODER & ~(3UL << (pin * 2))) | (1UL << (pin * 2));
            leds[i].value = high ? MAX_LED_BRIGHTNESS : 0;"""
    return replace_once(text, old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler-root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--toolchain-bin", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = (args.out or root / "build/lmp8").resolve()
    platform = out / "platform"
    shared = {name: sha for name, sha in FILES.items()
              if name not in ("ArduinoAdapter.hpp", "arduino_link.c") and name not in LED_FILES}
    external = (args.compiler_root.resolve() / "compiler/library/tjc-ms1-lmp8-a1/1.0/source"
                if args.compiler_root else None)
    prepare_runtime(external or root / "firmware/runtime", platform, shared)
    prepare_runtime(external or root / "firmware/runtime-lmp8", platform, LED_FILES)
    controller = platform / "LEDController.hpp"
    controller.write_text(patch_controller(controller.read_text(encoding="utf-8")), encoding="utf-8")
    if args.prepare_only:
        print("Verified lmp8 runtime with safe output startup in", platform)
        return
    tools = find_tools(args.toolchain_bin)
    elf = out / "sketch.elf"
    run([tools["arm-none-eabi-g++"], "-std=gnu++11", "-mcpu=cortex-m0plus",
         "-mthumb", "-DSTM32G030x6", '-D_FIR_GIT_REV="' + REVISION + '"',
         "-I", platform, "-I", root / "firmware/lmp8", "-T", platform / "tjc-ms1-b4272ba421624bbe-usercode.ld",
         platform / "startup_stm32g030xx.s", root / "firmware/lmp8/sketch.cpp",
         "--specs=nosys.specs", "-Os", "-g", "-fno-exceptions", "-fno-rtti",
         "-ffunction-sections", "-fdata-sections", "-fno-strict-aliasing",
         "-Wl,--gc-sections", "-Wl,-Map=" + str(out / "sketch.map"), "-o", elf])
    run([tools["arm-none-eabi-objcopy"], "-Obinary", elf, out / "sketch.bin"])
    run([tools["arm-none-eabi-size"], elf])


if __name__ == "__main__":
    main()
