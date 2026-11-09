# FSK-bit-decoder

Bit-by-bit FSK analysis from PCM WAV recordings, with a plugin interface for protocol decoders.

## MDC1200 decoder

A protocol decoder is included at `decoders/mdc1200.py`. It follows the implementation in [TrueMDC UV-K5 firmware](https://github.com/IvanMaestroGTR/TrueMDC-9M2RTX-UVK5-Firmware):

- Differential bit recovery and 40-bit MDC sync search (`07 09 2A 44 6F`)
- Both sync polarities and both analyzer input bit mappings
- 112-bit encoded payload extraction
- 7-byte data + 7-byte rate-1/2, K=7 FEC decoding
- De-interleaving, firmware-style error correction, and CRC-16/XMODEM validation
- Reports opcode, argument, and 16-bit unit ID only when the packet CRC validates

### Install / use

1. Copy `decoders/mdc1200.py` into the `decoders` folder beside `tmid_analyzer.py`.
2. Open the WAV in FSK-bit-decoder and set the correct Mark, Space, and baud rate (MDC1200 is 1200 baud, normally 1200/1800 Hz).
3. Keep the full recording in the analysis. The MDC decoder searches the complete symbol stream, including the warble/preamble; it does not rely on the analyzer's estimated data-start marker.
4. Click **Analyze**, then choose **MDC1200 – sync + FEC + CRC** in the Decoder list.

A successful result is labelled **MDC1200 PACKET VALID**. A sync match without a passing FEC/CRC is not reported as a valid packet. The analyzer's data-start estimate can be several symbols later than the actual MDC sync, so the decoder intentionally reads `all_bits` rather than only `bits`. This is an audio-side decoder; it does not emulate BK4819 hardware sync/FIFO/IRQ behavior.

## Decoder plugin interface

A plugin is a Python file in the `decoders` folder that defines `decode(ctx)`. It receives the context documented in `tmid_analyzer.py` and returns a string, a `(value, debug)` pair, or a dictionary with `value` and `debug`.
