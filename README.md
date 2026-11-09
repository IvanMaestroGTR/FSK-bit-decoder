# FSK-bit-decoder

Bit-by-bit FSK analysis from PCM WAV recordings, with a plugin interface for protocol decoders.

## How to use

### Requirements

- Python 3
- Tkinter (included with the standard Python installer on Windows and macOS; on Debian/Ubuntu install it with `sudo apt install python3-tk`)

The analyzer uses Python's standard library. No `pip install` command or third-party Python packages are required.

### Run

Open a terminal in the project folder and run:

```sh
python tmid_analyzer.py
```

On systems where Python 3 is invoked as `python3`, use:

```sh
python3 tmid_analyzer.py
```

1. Click **Open PCM WAV…** and select your recording.
2. Check the Mark, Space, and Baud settings. Frequency mode defaults to Auto; for MDC1200 and FleetSync, the expected pair is 1200/1800 Hz. Bell 202 commonly uses 1200/2200 Hz.
3. Keep the full transmission in the recording. The decoder searches the complete symbol stream; the data-start estimate is only a visual guide and does not trim the bits passed to the decoder.
4. Click **Analyze**, then choose the required decoder from the Decoder list.

## Included decoders

### MDC1200

The plugin at `decoders/mdc1200.py` follows the implementation in the [TrueMDC UV-K5 firmware](https://github.com/IvanMaestroGTR/TrueMDC-9M2RTX-UVK5-Firmware).

- Differential bit recovery and 40-bit MDC sync search.
- Both sync polarities and both analyzer input bit mappings.
- 112-bit encoded payload extraction.
- FEC decoding, de-interleaving, error correction, and firmware-compatible CRC validation.
- Reports opcode, argument, and 16-bit unit ID only when the packet CRC validates.

Select **MDC1200 – sync + FEC + CRC** in the Decoder list.

### FleetSync

The plugin at `decoders/fleetsync.py` independently handles FleetSync I and II ANI decoding.

- Searches the complete symbol stream, including the preamble, and tries both bit polarities.
- Detects the supported sync words.
- Validates FleetSync I block check and parity.
- Handles FleetSync II nibble codewords and single-bit correction.
- Reports the fleet number, unit number, raw words, and validation diagnostics.

Select **FleetSync – ANI + block check + FS-II ECC** in the Decoder list. Keep the full PTT burst in the analysis.

The current FleetSync plugin focuses on ANI (fleet/unit identity); it does not interpret every FleetSync status, text, GPS, or transparent-data message type.

## Decoder plugin interface

A decoder is a Python file in the `decoders` folder that defines `decode(ctx)`. It receives the context documented in `tmid_analyzer.py` and returns a string, a `(value, debug)` pair, or a dictionary containing `value` and `debug`.

## Protocol reference

- [russinnes/fsync-mdc1200-decode](https://github.com/russinnes/fsync-mdc1200-decode)
