#!/usr/bin/env python3
"""Build three handwritten ARM gateways using the local runtime."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess

REVISION = "c0e235f364f21b1a7ba8dbb598ef0bc983511af6"
FILES = {
    "UART.hpp": "ac3d1f1c16e96824dd83ecfe43440a1265876167416b7ef290744a85b59790c2",
    "ArduinoAdapter.hpp": "b6506c23574467dbbdc2b1853c16bd5504a104b36d3e2f385696805d7b0fbeb2",
    "arduino_link.c": "3bd34c785c07eb16b9aa5fe97eff3c1c4277c7e9fafb0f6132fe28d3115135c2",
    "system.c": "eaf8aa991ce47925d0b8ab2114fb587d2c4bbf1c7a66c31a40bf5146e0af1bd5",
    "startup_stm32g030xx.s": "0ef60de1588bb6ed9f6c0f7b50b6385c1439b2da608e3094ce11c769d9194b9a",
    "tjc-ms1-b4272ba421624bbe-usercode.ld": "22d9152fcb2396187a887ab0b91ed8d80059aaf33a8952ead6f7d4f5945e04bb",
    "SimpleBus.hpp": "039c5929e658cc56949b0def47efc19d022fc63d78a18e56375d99cb4fcd2a6a",
    "stm32g030xx.h": "c8cb9ab3c1ad98fd52076cbd87d7c042fd3414143ef09f06c15454efa75594e4",
    "Pins.hpp": "7ea95a468f0aa748d9cb80d83a104c9cc2f3af19a9bcddeb235e8ecbfa49252c",
    "macros.hpp": "8b7f730ad4ec0cdb60652913d2d40b26edaf55ad2e6962e1851874b800645b7a",
    "core_cm0plus.h": "1f0f7859d6355a7569799a9802560c55bc6a660be501ecbc45dedb77d85be3c9",
    "core_cmInstr.h": "c9b8e36d05097da5d493f197aa7f81ce517f4532536925b568ccbd6e2c01eb8a",
    "cmsis_gcc.h": "6c7e75d09887cb3035f18bc6f4fdbd7392a50379cd556315deb3bcb762d99078",
    "core_cmFunc.h": "1fdd226d82bde2a90231ba47a59377158b8907e4cada2a6215f32dcf13ab35cd",
    "system_stm32g0xx.h": "f0017b79121eeed68005c49f3efdf57865142495db48e841b966af086fda915b",
}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Pinned runtime patch context mismatch: " + old[:65])
    return text.replace(old, new, 1)


def patch_uart(text):
    text = replace_once(text, "const uint8_t SIZESimpleBus = 5;",
                        "const uint8_t SIZESimpleBus = 64;")
    text = replace_once(text, "uint8_t l = 0, r = 0;",
                        "volatile uint8_t l = 0, r = 0;\n        volatile bool overflow = false;")
    start = text.index("            Pair extractValue() {")
    end = text.index("\n        }\n    }", start)
    text = text[:start] + """            Pair extractValue() {
                const uint32_t saved = __get_PRIMASK();
                __disable_irq();
                Pair result{};
                if (l != r) {
                    result.ok = true;
                    result.value = userBuffer[l];
                    shiftLPointer();
                }
                __set_PRIMASK(saved);
                return result;
            }

            void putValue(uint8_t value) {
                const uint8_t next = (r + 1u) % SIZESimpleBus;
                if (next == l) { overflow = true; return; }
                userBuffer[r] = value;
                r = next;
            }

            bool takeOverflow() {
                const uint32_t saved = __get_PRIMASK();
                __disable_irq();
                const bool result = overflow;
                if (result) { overflow = false; l = r; }
                __set_PRIMASK(saved);
                return result;
            }
""" + text[end:]
    # The upstream sliding window casts unaligned byte pointers to uint16_t*.
    # Cortex-M0+ must not perform these unaligned halfword accesses.
    start = text.index("                        *reinterpret_cast<uint16_t* const>(&interruptBuffer[0])")
    end = text.index("                        --packetIterator;", start)
    text = text[:start] + """                        for (uint8_t i = 0; i < 4; ++i)
                            interruptBuffer[i] = interruptBuffer[i + 1];
""" + text[end:]
    return text


def prepare_runtime(source, dest, files=None):
    dest.mkdir(parents=True, exist_ok=True)
    for filename, expected in (FILES if files is None else files).items():
        # Git Windows может заменить LF на CRLF; проверяем содержание, не ОС.
        contents = (source / filename).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError("Wrong upstream revision or modified file: " + filename)
        if filename == "UART.hpp":
            contents = patch_uart(contents.decode()).encode()
        elif filename == "ArduinoAdapter.hpp":
            text = contents.decode()
            text = replace_once(text, "_isQueueOverflow = false;\n            return;",
                                "_isQueueOverflow = true;\n            return;")
            text = replace_once(text, "        _isQueueOverflow = true;\n    }",
                                "        // Preserve the latched failure until consumed.\n    }")
            contents = text.encode()
        (dest / filename).write_bytes(contents)


def run(command):
    print(" ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), check=True)


def find_tools(directory=None):
    tools = {}
    for tool in ("arm-none-eabi-g++", "arm-none-eabi-objcopy", "arm-none-eabi-size"):
        if directory:
            located = next((str(directory / name) for name in (tool, tool + ".exe")
                            if (directory / name).is_file()), None)
        else:
            located = shutil.which(tool)
        if not located:
            raise SystemExit("Missing ARM target tool: " + tool + "; use --toolchain-bin or PATH")
        tools[tool] = located
    return tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler-root", type=Path,
                        help="optional external runtime override; default: firmware/runtime")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--toolchain-bin", type=Path,
                        help="Directory of ARM GCC tools, otherwise use PATH")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--gateway", choices=("all", "sensors", "actuator"), default="all")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = (args.out or root / "build" / "gateways").resolve()
    source = (args.compiler_root.resolve() / "compiler/library/tjc-ms1-ard-a1/1.0/source"
              if args.compiler_root else root / "firmware/runtime")
    platform = out / "platform"
    prepare_runtime(source, platform)
    if args.prepare_only:
        print("Verified and patched pinned runtime in", platform)
        return
    tools = find_tools(args.toolchain_bin)
    for name, node in (("sensors", 13), ("actuator", 14)):
        if args.gateway != "all" and args.gateway != name:
            continue
        target = out / name
        target.mkdir(parents=True, exist_ok=True)
        elf = target / "sketch.elf"
        run([tools["arm-none-eabi-g++"], "-std=gnu++11", "-mcpu=cortex-m0plus",
             "-mthumb", "-DSTM32G030x6", '-D_FIR_GIT_REV="' + REVISION + '"',
             "-DGATEWAY_NODE=" + str(node), "-I", platform,
             "-T", platform / "tjc-ms1-b4272ba421624bbe-usercode.ld",
             platform / "startup_stm32g030xx.s", root / "firmware/gateway/sketch.cpp",
             "--specs=nosys.specs", "-Os", "-g", "-fno-exceptions", "-fno-rtti",
             "-ffunction-sections", "-fdata-sections", "-fno-strict-aliasing",
             "-Wl,--gc-sections", "-Wl,-Map=" + str(target / "sketch.map"),
             "-o", elf])
        run([tools["arm-none-eabi-objcopy"], "-Obinary", elf, target / "sketch.bin"])
        run([tools["arm-none-eabi-size"], elf])


if __name__ == "__main__":
    main()
