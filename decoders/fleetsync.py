"""Kenwood FleetSync I and II ANI decoder."""

NAME = "FleetSync – ANI + block check + FS-II ECC"

_SYNC_WORDS = (0xAAAA23EB, 0xAAAA052B)
_SYNC_BITS = 32
_SYNC_MAX_ERRORS = 2
_FS1_BITS = 64
_FS2_BITS = 256


def _bits_of(value, width):
    return tuple((value >> shift) & 1 for shift in range(width - 1, -1, -1))


def _read_uint(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | (int(bit) & 1)
    return value


def _find_syncs(bits):
    source = [int(bit) & 1 for bit in bits]
    patterns = tuple(_bits_of(word, _SYNC_BITS) for word in _SYNC_WORDS)
    found = []

    for inverted in (False, True):
        stream = [bit ^ int(inverted) for bit in source]
        for word, pattern in zip(_SYNC_WORDS, patterns):
            for start in range(len(stream) - _SYNC_BITS + 1):
                errors = sum(a != b for a, b in zip(
                    stream[start:start + _SYNC_BITS], pattern
                ))
                if errors <= _SYNC_MAX_ERRORS:
                    found.append({
                        "start": start,
                        "payload_start": start + _SYNC_BITS,
                        "errors": errors,
                        "inverted": inverted,
                        "sync": word,
                    })

    found.sort(key=lambda item: (item["errors"], item["start"]))
    unique = []
    for candidate in found:
        if any(candidate["start"] == old["start"] and
               candidate["inverted"] == old["inverted"] for old in unique):
            continue
        unique.append(candidate)
    return unique


def _fsync_block_check(word1, word2):
    lfsr = 0
    parity = 0

    for index in range(48):
        if index < 32:
            bit = (word1 >> (31 - index)) & 1
        else:
            bit = (word2 >> (63 - index)) & 1

        parity ^= bit
        feedback = bit ^ ((lfsr >> 15) & 1)
        if feedback:
            lfsr ^= 0x6815
        lfsr = (lfsr << 1) & 0xFFFF

    for index in range(48, 63):
        parity ^= (word2 >> (63 - index)) & 1

    return ((lfsr ^ 0x0002) + parity) & 0xFFFF


def _gf256_next(value):
    value <<= 1
    if value & 0x100:
        value ^= 0x117
    return value & 0xFF


def _fs2_syndrome_columns():
    columns = []
    value = 1
    for _ in range(15):
        columns.append(value)
        value = _gf256_next(value)
    return tuple(columns)


_FS2_COLUMNS = _fs2_syndrome_columns()


def _fs2_ecc_repair(codeword):
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
    if len(frame_bits) < _FS2_BITS:
        return None, {"reason": f"FS-II needs {_FS2_BITS} bits; received {len(frame_bits)}."}

    nibbles = []
    corrected_count = 0

    for block in range(4):
        chunk = frame_bits[block * 64:(block + 1) * 64]
        halves = [
            _read_uint(chunk[index * 16:(index + 1) * 16])
            for index in range(4)
        ]

        for index in (1, 0, 3, 2):
            codeword = halves[index] & 0x7FFF
            repaired = _fs2_ecc_repair(codeword)
            if repaired is None:
                return None, {
                    "reason": f"FS-II ECC failed in block {block + 1}, codeword {index + 1}.",
                    "corrected_codewords": corrected_count,
                }

            corrected_count += repaired != codeword
            nibbles.append((repaired >> 8) & 0x0F)

    raw = bytes(
        (nibbles[index] << 4) | nibbles[index + 1]
        for index in range(0, 16, 2)
    )
    word1 = int.from_bytes(raw[:4], "big")
    word2 = int.from_bytes(raw[4:], "big")

    if _fsync_block_check(word1, word2) != (word2 & 0xFFFF):
        return None, {
            "reason": "FS-II block check failed after ECC.",
            "corrected_codewords": corrected_count,
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
        "Decoded bytes: " + " ".join(f"{byte:02X}" for byte in raw),
        f"Fleet: {fleet:03d}",
        f"Unit: {unit:04d}",
        "Block check: PASS",
    ]
    if extra:
        debug.extend(extra)

    return {"value": value, "debug": "\n".join(debug)}


def decode(ctx):
    bits = list(ctx.get("all_bits") or ctx.get("bits") or [])
    if not bits:
        return {
            "value": "No data bits supplied.",
            "debug": "Analyze the recording with the complete transmission included.",
        }

    baud = float(ctx.get("baud", 1200))
    if baud not in (1200.0, 2400.0):
        return {
            "value": "Unsupported FleetSync baud rate",
            "debug": f"Expected 1200 or 2400 baud; received {baud:g}.",
        }

    candidates = _find_syncs(bits)
    if not candidates:
        return {
            "value": "FleetSync sync not found",
            "debug": (
                f"Searched {len(bits)} symbols for 0xAAAA23EB and 0xAAAA052B, "
                f"allowing up to {_SYNC_MAX_ERRORS} bit errors and either polarity."
            ),
        }

    report = [
        f"Input symbols: {len(bits)}",
        f"Configured baud: {baud:g}",
        "Sync candidates (offset, errors, polarity, sync):",
    ]
    for candidate in candidates[:8]:
        report.append(
            f"  {candidate['start']}, {candidate['errors']}, "
            f"{'inverted' if candidate['inverted'] else 'normal'}, "
            f"{candidate['sync']:08X}"
        )

    for candidate in candidates:
        stream = [int(bit) & 1 for bit in bits]
        if candidate["inverted"]:
            stream = [bit ^ 1 for bit in stream]

        frame = stream[candidate["payload_start"]:]
        if len(frame) < _FS1_BITS:
            continue

        word1 = _read_uint(frame[:32])
        word2 = _read_uint(frame[32:64])
        calculated = _fsync_block_check(word1, word2)
        received = word2 & 0xFFFF

        if calculated == received:
            result = _parse_words(word1, word2, "I", [
                f"Selected sync offset: {candidate['start']}",
                f"Sync errors: {candidate['errors']}",
                f"Sync word: {candidate['sync']:08X}",
                f"Calculated check: {calculated:04X}; received: {received:04X}",
            ])
            result["debug"] = "\n".join(report + [result["debug"]])
            return result

        if len(frame) >= _FS2_BITS:
            words, info = _decode_fs2(frame)
            if words is not None:
                result = _parse_words(words[0], words[1], "II", [
                    f"Selected sync offset: {candidate['start']}",
                    f"Sync errors: {candidate['errors']}",
                    f"Sync word: {candidate['sync']:08X}",
                    f"Corrected codewords: {info['corrected_codewords']}",
                ])
                result["debug"] = "\n".join(report + [result["debug"]])
                return result

    candidate = candidates[0]
    available = len(bits) - candidate["payload_start"]
    return {
        "value": "FleetSync sync found, but no frame passed validation",
        "debug": "\n".join(report + [
            f"Bits after sync: {available}",
            f"FleetSync I requires {_FS1_BITS} bits; FleetSync II requires {_FS2_BITS}.",
            "The frame failed its block check or ECC validation.",
        ]),
    }
