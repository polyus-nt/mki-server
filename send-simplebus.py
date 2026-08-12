import time

import serial


def byte_to_ascii(value: int):
    if value < 10:
        return value + 48
    else:
        return value + 55


def addr_to_ascii(addr: int):
    left = (addr >> 4) & 0x0F
    right = addr & 0x0F

    leftA = byte_to_ascii(left)
    rightA = byte_to_ascii(right)
    return (leftA << 8) | rightA


"""
    Структура пакета для протокола SimpleBus:

    [CR] [addr1] [addr2] [data] [LF]

        [addr1] – старший полу-байт адреса в HEX-представлении
        [addr2] – младший полу-байт адреса в HEX-представлении
        [data] – символ полезной нагрузки
        [CR] [LF] – соответствующие управляющие символы
"""

# МС-ТЮК занимает четыре порта (у меня это COM10; COM11; COM9; COM8)
# Но заработало только с COM8
port = "COM8"
BAUD_RATE = 9600  # бод фиксированный
device = serial.Serial(port, baudrate=BAUD_RATE)

# В SimpleBus все совсем не simple, есть протокол
# который нужно соблюдать
START_BYTE = 13
END_BYTE = 10
address = 1  # куда отправляем
# мы отправляем адрес в виде двух байт,
# каждый байт является ascii представлением числа. (например, байт 0xFF)
# упаковывается как два байта ASCII FF
addr_ascii = addr_to_ascii(address)
addr1 = (addr_ascii >> 8) & 0xFF
addr2 = addr_ascii & 0xFF
data = 1

message = bytes([START_BYTE, addr1, addr2, data, END_BYTE])

while True:
    print(f"Send {message} to SimpleBus")
    device.write(message)
    time.sleep(1)
