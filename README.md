# FSK-bit-decoder

Bit-by-bit FSK analysis from PCM WAV recordings, with a plugin interface for protocol decoders.

## MDC1200 decoder

A protocol decoder is included at `decoders/mdc1200.py`. It follows the implementation in [TrueMDC UV-K5 firmware](https://github.com/IvanMaestroGTR/TrueMDC-9M2RTX-UVK5-Firmware):

- Differential bit recovery and 40-bit MDC sync search (`07 09 2A 44 6F`)
- Both sync polarities and both analyzer input bit mappings
- 112-bit encoded payload extraction
- 7-byte data + 7-byte rate-1/2, K=7 FEC decoding
- De-interleaving, firmware-style error correction, and an emulation of the DP32G030 hardware CRC configuration used by the firmware
- Reports opcode, argument, and 16-bit unit ID only when the packet CRC validates

### Install / use

1. Copy `decoders/mdc1200.py` into the `decoders` folder beside `tmid_analyzer.py`.
2. Open the WAV in FSK-bit-decoder. Frequency mode now defaults to Auto and estimates two recurring tones from overlapping 20 ms windows. For MDC1200 and FleetSync, expect the standard 1200/1800 Hz pair; Bell 202 commonly uses 1200/2200 Hz. Confirm the detected pair in the settings before decoding. Select Manual only when you need to override the estimates.
3. Keep the full recording in the analysis. The MDC decoder searches the complete symbol stream, including the warble/preamble; it does not rely on the analyzer's estimated data-start marker.
4. Click **Analyze**, then choose **MDC1200 – sync + FEC + CRC** in the Decoder list.

A successful result is labelled **MDC1200 PACKET VALID**. A sync match without a passing FEC/CRC is not reported as a valid packet. The analyzer's data-start estimate can be several symbols later than the actual MDC sync, so the decoder intentionally reads `all_bits` rather than only `bits`. This is an audio-side decoder; it does not emulate BK4819 hardware sync/FIFO/IRQ behavior.

## FleetSync decoder

A separate implementation is included at `decoders/fleetsync.py`. It is independent of the MDC1200 plugin and does not import or modify it.

- Searches the complete symbol stream, including the preamble, and tries both bit polarities.
- Detects the alternating preamble and FleetSync sync patterns, including the 24-bit preamble / `0xA23E` form and legacy `0x23EB` / `0x052B` sync forms.
- Validates FleetSync I using the bit-serial `0x6815` block check and parity.
- Decodes FleetSync II's 16 nibble codewords, repairs single-bit errors, then validates the reconstructed block check.
- Reports the 3-digit fleet number, 4-digit unit number, raw words, and validation diagnostics.
- Accepts analyzer symbol streams configured for 1200 or 2400 baud.

Keep the full PTT burst in the analysis and select **FleetSync – ANI + block check + FS-II ECC** in the Decoder list. The current plugin focuses on ANI (fleet/unit identity); it does not yet interpret every FleetSync status, text, GPS, or transparent-data message type. A frame is only labelled valid when its block check passes.

### FleetSync tests

Run the protocol tests with:

```sh
python -m unittest discover -s tests -v
```

The tests use synthetic frames, including the published Fleet 107 / Unit 1772 example, and exercise all 15 single-bit correction positions. Real WAV captures are still needed to validate the analyzer's tone slicing and timing against your radio recordings.

### Protocol references

- [KENWOOD glossary – FleetSync uses MSK](https://comms.kenwood.com/en/support/glossary/)
- [KENWOOD TK-2180/TK-3180 function reference – fleet and unit ID format, 1200/2400 baud](https://www.repeater-builder.com/kenwood/pdfs/tk-7180-8180-function-reference.pdf)
- [FleetSync frame and ANI field analysis](https://gophertrunk.org/blog/deep-dives/beyond-voice-04-fleetsync-wav-that-lied/)
- [russinnes/fsync-mdc1200-decode](https://github.com/russinnes/fsync-mdc1200-decode) was consulted as a protocol cross-reference; no C source files were copied into this plugin.

## Decoder plugin interface

A plugin is a Python file in the `decoders` folder that defines `decode(ctx)`. It receives the context documented in `tmid_analyzer.py` and returns a string, a `(value, debug)` pair, or a dictionary with `value` and `debug`.
