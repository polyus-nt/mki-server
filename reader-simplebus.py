import serial


def ascii_byte_to_nibble(b: int) -> int:
    if 48 <= b <= 57:  # '0'–'9'
        return b - 48
    # 'A'–'F'
    return b - 55


# МС-ТЮК занимает четыре порта (у меня это COM10; COM11; COM9; COM8)
# У меня заработало только с COM8
port = "COM8"
baudrate = 9600
device = serial.Serial(port, baudrate=baudrate)

print(f"start listen {port}...")
while True:
    message = device.read(5)
    hex_str = message[1:3].decode("ascii")
    address = int(hex_str, 16)
    print("addr: ", address)
    data = message[3]
    print("data: ", data)
    print(f"read byte {message}")
