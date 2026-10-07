#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "../common/HardwareProtocol.h"
using namespace hardware_api;
static void fromHex(const char* text, uint8_t* bytes) {
  uint8_t count = 0;
  while (*text) {
    if (*text == ' ') { ++text; continue; }
    char* end; const unsigned long value = strtoul(text, &end, 16);
    // A vector uses space-separated bytes, not a packed hex string.
    assert(end == text + 2 && count < 20 && value <= 255);
    bytes[count++] = static_cast<uint8_t>(value); text = end;
  }
  assert(count == 20);
}
int main(int argc, char** argv) {
  if (argc == 3) {
    uint8_t requestBytes[20], replyBytes[20], encoded[20];
    fromHex(argv[1], requestBytes); fromHex(argv[2], replyBytes);
    Frame request, reply;
    assert(decode(requestBytes, request) && decode(replyBytes, reply));
    assert(sameRequest(request, reply));
    encode(request, encoded); assert(memcmp(encoded, requestBytes, 20) == 0);
    encode(reply, encoded); assert(memcmp(encoded, replyBytes, 20) == 0);
    StreamParser parser; Frame received;
    for (uint8_t i = 0; i < 20; ++i)
      assert(parser.feed(requestBytes[i], i * 2UL, received) == (i == 19));
    encode(received, encoded); assert(memcmp(encoded, requestBytes, 20) == 0);
    return 0;
  }
  assert(crc16(reinterpret_cast<const uint8_t*>("123456789"), 9) == 0x29b1);
  Frame frame = {}; frame.version = 1; frame.kind = 1; frame.tx_id = 65535;
  frame.node = 14; frame.command = kSetPwm; frame.idx = 5; frame.data[0] = 255;
  uint8_t bytes[20]; encode(frame, bytes);
  StreamParser parser; Frame received;
  for (uint8_t i = 0; i < 11; ++i) assert(!parser.feed(bytes[i], i, received));
  parser.tick(261);
  for (uint8_t i = 11; i < 20; ++i) assert(!parser.feed(bytes[i], 262 + i, received));
  uint8_t damaged[20]; memcpy(damaged, bytes, 20); damaged[12] ^= 1;
  for (uint8_t i = 0; i < 20; ++i) assert(!parser.feed(damaged[i], 300 + i, received));
  for (uint8_t i = 0; i < 20; ++i)
    assert(parser.feed(bytes[i], 400 + i, received) == (i == 19));
  // Lost byte followed by a valid candidate, including embedded magic.
  frame.data[1] = 0xa5; frame.data[2] = 0x5a; encode(frame, bytes);
  parser.reset();
  for (uint8_t i = 0; i < 20; ++i)
    if (i != 5) assert(!parser.feed(bytes[i], 500 + i, received));
  for (uint8_t i = 0; i < 20; ++i)
    assert(parser.feed(bytes[i], 600 + i, received) == (i == 19));
  // Timers across uint32 overflow.
  parser.reset(); const uint32_t start = 0xfffffffeUL;
  for (uint8_t i = 0; i < 20; ++i)
    assert(parser.feed(bytes[i], static_cast<uint32_t>(start + i), received) == (i == 19));
  ReplyBuffer output;
  assert(output.enqueue(response(frame)) && !output.enqueue(response(frame)));
  for (uint8_t i = 0; i < 20; ++i) { assert(output.pending()); output.accepted(); }
  assert(!output.pending());
  // Unknown kind gets no response; a slow drip cannot extend frame life forever.
  frame.kind = 3; encode(frame, bytes); parser.reset();
  for (uint8_t i = 0; i < 20; ++i) assert(!parser.feed(bytes[i], 1000 + i, received));
  frame.kind = 1; encode(frame, bytes); parser.reset();
  for (uint8_t i = 0; i < 20; ++i)
    assert(!parser.feed(bytes[i], 2000 + i * 60UL, received));
  for (uint8_t i = 0; i < 20; ++i)
    assert(parser.feed(bytes[i], 4000 + i, received) == (i == 19));
  // Bounded junk-stream exercise for ASan/UBSan.
  uint32_t noise = 0x12345678;
  for (uint32_t i = 0; i < 10000; ++i) {
    noise ^= noise << 13; noise ^= noise >> 17; noise ^= noise << 5;
    parser.feed(static_cast<uint8_t>(noise), 5000 + i, received);
  }
  puts("protocol: CRC, golden codec, resync, timeout, wrap, bounded TX passed");
}
