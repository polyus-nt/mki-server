# Adafruit DHT sensor library 1.4.6

Source: https://github.com/adafruit/DHT-sensor-library/tree/1.4.6
Included only DHT.h/DHT.cpp, not the Unified Sensor wrapper; no external library
is required in Arduino IDE. MIT license retained in LICENSE and file headers.

Local changes: sensor-type constant DHT11 renamed DHT_TYPE_11 to avoid a name
collision with our DHT11 class; pulse capture aborts immediately at the first
timeout instead of continuing to collect the remaining 80 timing values.

readHumidity(true) is synchronous: 1+20 ms start delays and timing-critical pulse
capture with interrupts disabled, bounded by pulse loop counters. This is NOT a
fully nonblocking driver and requires transport/timing tests on actual hardware.
