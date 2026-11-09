"""Independent Kenwood FleetSync I / FleetSync II ANI decoder.

Consumes the analyzer's already-sliced symbol stream.  It does not change or
import the MDC1200 decoder.  The implementation is self-contained and uses
the published frame description: alternating preamble + sync, 4-bit lead-in,
two 32-bit words, a bit-serial block check, and FleetSync II's single-bit-
correcting nibble code.
"""

NAME = "FleetSync – ANI + block check + FS-II ECC"

# Publicly documented wire-level sync forms.  The first form uses a 24-bit
# alternating preamble and 0xA23E; legacy detectors use 0xAAAA + 0x23EB or
# 0xAAAA + 0x052B.  These are protocol constants, not copied decoder code.
_SYNC_FORMS = (
    (0xAAAAAA, 24, 0xA23E, 16, "24-bit preamble / A23E"),
    (0xAAAA, 16, 0x23EB, 16, "legacy 23EB"),
    (0xAAAA, 16, 0x052B, 16, "legacy 052B"),
)
_PREAMBLE_MAX_ERRORS = 3
_SYNC_MAX_ERRORS = 1
_FRAME_PREFIX_BITS = 4
_FS1_BITS = 68
_FS2_BITS = 260


def _bits_of(value, width):
    return tuple((value >> shift) & 1 for shift in range(width - 1, -1, -1))


def _hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def _read_uint(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | (int(bit) & 1)
    return value


def _find_syncs(bits):
    """Find preamble+sync candidates, trying both slicer polarities."""
    source = [int(b) & 1 for b in bits]
    found = []
    for inverted in (False, True):
        stream = [b ^ int(inverted) for b in source]
        for pre_value, pre_width, sync_value, sync_width, label in _SYNC_FORMS:
            pre = _bits_of(pre_value, pre_width)
            sync = _bits_of(sync_value, sync_width)
            total = pre_width + sync_width
            for start in range(0, len(stream) - total + 1):
                pe = _hamming(stream[start:start + pre_width], pre)
                se = _hamming(stream[start + pre_width:start + total], sync)
                if pe <= _PREAMBLE_MAX_ERRORS and se <= _SYNC_MAX_ERRORS:
                    found.append({
                        "start": start,
                        "payload_start": start + total,
                        "preamble_errors": pe,
                        "sync_errors": se,
                        "inverted": inverted,
                        "form": label,
                    })
    found.sort(key=lambda c: (c["preamble_errors"] + c["sync_errors"], c["start"]))
    unique = []
    for candidate in found:
        if any(abs(candidate["start"] - old["start"]) < 8 and
               candidate["inverted"] == old["inverted"] for old in unique):
            continue
        unique.append(candidate)
    return unique


def _fsync_block_check(word1, word2):
    """Calculate the FleetSync 16-bit serial LFSR + parity block check."""
    lfsr = 0
    parity = 0
    for bit_index in range(48):
        if bit_index < 32:
            bit = (word1 >> (31 - bit_index)) & 1
        else:
            bit = (word2 >> (63 - bit_index)) & 1
        parity ^= bit
        feedback = bit ^ ((lfsr >> 15) & 1)
        if feedback:
            lfsr ^= 0x6815
        lfsr = (lfsr << 1) & 0xFFFF
    for bit_index in range(48, 63):
        parity ^= (word2 >> (63 - bit_index)) & 1
    return ((lfsr ^ 0x0002) + parity) & 0xFFFF


def _gf256_next(value):
    """Multiply by x in GF(256), primitive polynomial x^8+x^4+x^2+x+1."""
    value <<= 1
    if value & 0x100:
        value ^= 0x117
    return value & 0xFF


def _fs2_syndrome_columns():
    # Generate the 15 SEC syndrome columns as consecutive powers of x in
    # GF(256), instead of embedding a copied parity-mask lookup table.
    columns = []
    value = 1
    for _ in range(15):
        columns.append(value)
        value = _gf256_next(value)
    return tuple(columns)


_FS2_COLUMNS = _fs2_syndrome_columns()


def _fs2_ecc_repair(codeword):
    """Correct one flipped bit in a 15-bit FleetSync II nibble codeword."""
    codeword &= 0x7FFF
    syndrome = 0
    for position, column in enumerate(_FS2_COLUMNS):
        if codeword & (1 << (14 - position)):
            syndrome ^= column
    if syndrome == 0:
        return codeword
    try:
        position = _FS2_COLUMNS.index(syndrome)
    except ValueError:
        return None
    return codeword ^ (1 << (14 - position))


def _decode_fs2(frame_bits):
    """Decode 16 nibble codewords carried in four 64-bit blocks."""
    if len(frame_bits) < _FS2_BITS:
        return None, {"reason": f"FS-II needs {_FS2_BITS} post-sync bits; only {len(frame_bits)} available."}
    body = frame_bits[_FRAME_PREFIX_BITS:_FS2_BITS]
    nibbles = []
    corrected_count = 0
    for block in range(4):
        chunk = body[block * 64:(block + 1) * 64]
        for word_index in range(4):
            half = _read_uint(chunk[word_index * 16:(word_index + 1) * 16])
            codeword = half & 0x7FFF
            repaired = _fs2_ecc_repair(codeword)
            if repaired is None:
                return None, {
                    "reason": f"FS-II ECC failed in block {block + 1}, codeword {word_index + 1}.",
                    "corrected_codewords": corrected_count,
                }
            if repaired != codeword:
                corrected_count += 1
            nibbles.append((repaired >> 8) & 0x0F)
    if len(nibbles) != 16:
        return None, {"reason": "Internal FS-II nibble count mismatch."}
    octets = [(nibbles[i] << 4) | nibbles[i + 1] for i in range(0, 16, 2)]
    word1 = int.from_bytes(bytes(octets[:4]), "big")
    word2 = int.from_bytes(bytes(octets[4:]), "big")
    if _fsync_block_check(word1, word2) != (word2 & 0xFFFF):
        return None, {
            "reason": "FS-II ECC repaired the codewords, but the reconstructed block check did not validate.",
            "corrected_codewords": corrected_count,
            "word1": word1,
            "word2": word2,
        }
    return (word1, word2), {"corrected_codewords": corrected_count}


def _parse_words(word1, word2, variant, extra=None):
    raw = word1.to_bytes(4, "big") + word2.to_bytes(4, "big")
    fleet = raw[2] + 99
    unit = ((raw[3] << 4) | (raw[4] >> 4)) + 999
    value = (
        f"FleetSync {variant} ANI\n"
        f"Fleet: {fleet:03d}\n"
        f"Unit: {unit:04d}\n"
        "Block check: VALID\n"
        f"Raw words: {word1:08X} {word2:08X}"
    )
    debug = [
        f"Word 1: {word1:08X}",
        f"Word 2: {word2:08X}",
        "Decoded bytes: " + " ".join(f"{b:02X}" for b in raw),
        f"Fleet field: byte[2]={raw[2]} -> {fleet}",
        f"Unit field: (byte[3]<<4 | byte[4]>>4) -> {unit}",
        "Block check: PASS",
    ]
    if extra:
        debug.extend(extra)
    return {"value": value, "debug": "\n".join(debug)}


def decode(ctx):
    """Decode FleetSync I or II from the analyzer's complete sliced bitstream."""
    bits = list(ctx.get("all_bits") or ctx.get("bits") or [])
    if not bits:
        return {
            "value": "No symbol bits supplied.",
            "debug": "Analyze the WAV first and keep the complete transmission, including its preamble. FleetSync sync search uses all_bits rather than the estimated data-start slice.",
        }
    baud = float(ctx.get("baud", 1200))
    if baud not in (1200.0, 2400.0):
        return {"value": "Unsupported FleetSync baud rate", "debug": f"This decoder currently supports the documented 1200/2400 baud ANI modes; analyzer reports {baud:g} baud."}

    candidates = _find_syncs(bits)
    if not candidates:
        return {
            "value": "FleetSync sync not found",
            "debug": (
                f"Searched {len(bits)} sliced symbols at {baud:g} baud. Tried both bit polarities, "
                "24-bit alternating preamble + 0xA23E, and the 16-bit alternating preamble "
                "with legacy sync words 0x23EB / 0x052B. Limits: 3 preamble errors, 1 sync error.\n"
                "Check that the full PTT burst is included, the detected tones are correct "
                "(normally 1200/1800 Hz), and the symbol timing / bit mapping is reasonable."
            ),
        }

    report = [
        f"Input symbols: {len(bits)}",
        f"Configured baud: {baud:g}",
        "Sync candidates (start, preamble errors, sync errors, polarity, form):",
    ]
    for c in candidates[:8]:
        report.append(
            f"  {c['start']}, {c['preamble_errors']}, {c['sync_errors']}, "
            f"{'inverted' if c['inverted'] else 'normal'}, {c['form']}"
        )

    for candidate in candidates:
        payload_start = candidate["payload_start"]
        stream = [int(b) & 1 for b in bits]
        if candidate["inverted"]:
            stream = [b ^ 1 for b in stream]
        available = stream[payload_start:]
        if len(available) < _FS1_BITS:
            continue
        fs1_body = available[_FRAME_PREFIX_BITS:_FS1_BITS]
        word1 = _read_uint(fs1_body[:32])
        word2 = _read_uint(fs1_body[32:64])
        calculated = _fsync_block_check(word1, word2)
        received = word2 & 0xFFFF
        if calculated == received:
            result = _parse_words(word1, word2, "I", [
                f"Selected sync offset: {candidate['start']}",
                f"Preamble errors: {candidate['preamble_errors']}",
                f"Sync errors: {candidate['sync_errors']}",
                f"Sync form: {candidate['form']}",
                f"Calculated check: {calculated:04X}; received: {received:04X}",
                "FleetSync I validation: PASS",
            ])
            result["debug"] = "\n".join(report + [result["debug"]])
            return result

        if len(available) >= _FS2_BITS:
            fs2_words, fs2_info = _decode_fs2(available)
            if fs2_words is not None:
                w1, w2 = fs2_words
                result = _parse_words(w1, w2, "II", [
                    f"Selected sync offset: {candidate['start']}",
                    f"Preamble errors: {candidate['preamble_errors']}",
                    f"Sync errors: {candidate['sync_errors']}",
                    f"Sync form: {candidate['form']}",
                    f"FS-II corrected codewords: {fs2_info['corrected_codewords']}",
                    "FleetSync II ECC + block check: PASS",
                ])
                result["debug"] = "\n".join(report + [result["debug"]])
                return result

    best = candidates[0]
    available = max(0, len(bits) - best["payload_start"])
    return {
        "value": "FleetSync sync found, but no frame passed validation",
        "debug": "\n".join(report + [
            f"Bits after best sync: {available}",
            f"FleetSync I requires {_FS1_BITS} post-sync bits; FleetSync II requires {_FS2_BITS}.",
            "The FleetSync I block check did not validate. FleetSync II was attempted when enough bits were present; its ECC or reconstructed block check did not validate.",
            "This is not reported as a valid ANI. Check symbol timing, tone mapping, capture completeness, and baud selection.",
        ]),
    }
