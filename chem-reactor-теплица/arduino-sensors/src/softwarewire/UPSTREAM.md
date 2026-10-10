# SoftwareWire 1.6.0

Source: [Testato/SoftwareWire](https://github.com/Testato/SoftwareWire), version
1.6.0 as published in its Arduino/PlatformIO package. Original Licence.md retained.

Local changes: a latched timeout flag, one deadline across a register write and
repeated-start read, early exit after timeout, bounded receive buffer. All clock
stretch loops share that deadline; STOP releases lines even on failure.
Diagnostic register-pointer formatting uses uintptr_t (also permits native tests).
BMP280SoftwareBus configures 50kHz, 20ms, internal pullups disabled and checks
the timeout flag. This fixes upstream's per-bit wait followed by silent success.
