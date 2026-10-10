#include <Arduino.h>
#include "BoardConfig.h"
#include "src/austere/AustereSerialMaster.h"
#include "src/BoardIO.h"
#include "src/LinkPins.h"
BoardIO board;
hardware_api::SensorApi api(board);
AustereSerialMaster link(API_LINK_MISO, API_LINK_MOSI, API_LINK_CLK, API_LINK_CS, API_LINK_IRQ);
hardware_api::StreamParser parser;
hardware_api::ReplyBuffer replies;
bool linkReady = false;
uint16_t overruns = 0;
#ifdef TEST
#include "src/TestConsole.h"
TestConsole console;
#endif
void setup() {
  Serial.begin(9600);
  if (!validLinkPins() || !board.setup()) { Serial.println(F("Invalid pin map")); return; }
  linkReady = link.setup();
#ifdef TEST
  Serial.println(F("temp 0..1 | pressure 0..1 | humidity 0..1 | gas 0..1 | help"));
#endif
}
void loop() {
  if (!linkReady) return;
#ifdef TEST
  if (Serial.available()) console.feed(static_cast<char>(Serial.read()), api);
#endif
  parser.tick(millis());
  if (overruns != link.overrunCount()) {
    overruns = link.overrunCount(); parser.reset(); link.clearRx();
  }
  bool received = false;
  if (!replies.pending()) {
    uint8_t byte; hardware_api::Frame request;
    if (link.recvByte(&byte)) {
      received = true;
      if (parser.feed(byte, millis(), request) && request.kind == hardware_api::kRequest)
        replies.enqueue(api.handle(request));
    }
  }
  if (replies.pending() && link.sendByte(replies.nextByte())) replies.accepted();
  if (!received && !replies.pending()) board.poll(millis());
}
