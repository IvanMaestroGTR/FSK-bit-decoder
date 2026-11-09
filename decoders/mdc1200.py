"""MDC1200 decoder for the FSK-bit-decoder plugin interface.

Implements the same sync, differential bit recovery, de-interleaving,
K=7 rate-1/2 error correction, and CRC-16/XMODEM validation used by
IvanMaestroGTR/TrueMDC-9M2RTX-UVK5-Firmware.
"""
NAME = "MDC1200 – sync + FEC + CRC"

SYNC = (0x07, 0x09, 0x2A, 0x44, 0x6F)
SYNC_BITS = tuple((b >> shift) & 1 for b in SYNC for shift in range(7, -1, -1))
PAYLOAD_BITS = 14 * 8


def _crc16_firmware(data):
    # Emulate compute_crc() in driver/crc.c on the DP32G030:
    # input bytes are bit-reversed, then the CRC-16/CCITT result is
    # bit-reversed and complemented by the hardware output configuration.
    def reverse8(value):
        value = ((value & 0x55) << 1) | ((value >> 1) & 0x55)
        value = ((value & 0x33) << 2) | ((value >> 2) & 0x33)
        return ((value & 0x0F) << 4) | ((value >> 4) & 0x0F)

    def reverse16(value):
        value = ((value & 0x5555) << 1) | ((value >> 1) & 0x5555)
        value = ((value & 0x3333) << 2) | ((value >> 2) & 0x3333)
        value = ((value & 0x0F0F) << 4) | ((value >> 4) & 0x0F0F)
        return ((value & 0x00FF) << 8) | ((value >> 8) & 0x00FF)

    crc = 0
    for byte in data:
        crc ^= reverse8(byte) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return reverse16(crc) ^ 0xFFFF


def _bytes_msb(bits):
    return [sum((bits[i + j] & 1) << (7 - j) for j in range(8))
            for i in range(0, len(bits) - 7, 8)]


def _deinterleave(encoded):
    # Exact inverse of encode_data()'s 112-bit interleaver.
    stream = []
    for byte in encoded:
        stream.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    deinterleaved = [0] * 112
    k = 0
    for i in range(16):
        for m in range(7):
            n = m * 16 + i
            deinterleaved[k] = stream[n]
            k += 1
    decoded = []
    pos = 0
    for _ in range(14):
        value = 0
        for bit_num in range(8):
            if deinterleaved[pos]:
                value |= 1 << bit_num
            pos += 1
        decoded.append(value)
    return decoded


def _error_correct(data):
    # Port of error_correction() in app/mdc1200.c (greedy syndrome correction).
    data = list(data)
    shift_reg = 0
    syn = 0
    for i in range(7):
        byte = data[i]
        for bit_num in range(8):
            shift_reg = ((shift_reg << 1) | ((byte >> bit_num) & 1)) & 0xFF
            b = ((shift_reg >> 6) ^ (shift_reg >> 5) ^
                 (shift_reg >> 2) ^ shift_reg) & 1
            syn = ((syn << 1) | (b ^ ((data[i + 7] >> bit_num) & 1))) & 0xFF
            weight = ((syn >> 7) & 1) + ((syn >> 5) & 1) + ((syn >> 2) & 1) + ((syn >> 1) & 1)
            if weight >= 3:
                ii = i
                bn = bit_num - 7
                if bn < 0:
                    bn += 8
                    ii -= 1
                if ii >= 0:
                    data[ii] ^= 1 << bn
                syn ^= 0xA6
    return data


def _decode_fec(encoded):
    original = _deinterleave(encoded)
    attempts = [None] + list(range(112))
    for bit_to_flip in attempts:
        candidate = list(original)
        if bit_to_flip is not None:
            candidate[bit_to_flip >> 3] ^= 1 << (bit_to_flip & 7)
        corrected = _error_correct(candidate)
        payload = corrected[:7]
        received_crc = corrected[4] | (corrected[5] << 8)
        calculated_crc = _crc16_firmware(payload[:4])
        if received_crc == calculated_crc:
            return corrected, received_crc, calculated_crc, bit_to_flip
    return None, None, None, None


def _find_sync(bits):
    # Firmware RX computes a running XOR of incoming demodulator bits before
    # searching for the 40-bit sync word. Try both input polarities and every
    # possible sync alignment in the supplied stream.
    found = []
    for polarity in (0, 1):
        differential = []
        state = 0
        for bit in bits:
            state ^= (int(bit) & 1) ^ polarity
            differential.append(state)
        limit = len(differential) - 40
        for start in range(max(0, limit + 1)):
            window = differential[start:start + 40]
            errors = sum(a != b for a, b in zip(window, SYNC_BITS))
            inv_errors = 40 - errors
            if min(errors, inv_errors) <= 2:
                found.append((min(errors, inv_errors), start, polarity,
                              inv_errors < errors, differential))
    return sorted(found, key=lambda item: (item[0], item[1]))


def decode(ctx):
    # MDC sync may begin before the analyzer's estimated data_start index.
    # Always search the complete symbol stream first; use data-only bits only
    # as a fallback for older plugin hosts that do not provide all_bits.
    raw = list(ctx.get("all_bits") or ctx.get("bits", []))
    if not raw:
        return {"value": "No data bits supplied.", "debug": "Select the full recording and keep preamble display enabled. Sync search uses all symbols, including symbols before the estimated data start."}

    candidates = _find_sync(raw)
    if not candidates:
        return {
            "value": "MDC1200 sync not found",
            "debug": ("Searched the differential-decoded stream for the 40-bit "
                      "MDC sync 07 09 2A 44 6F, allowing either input polarity "
                      "and up to 2 sync-bit errors.\n"
                      f"Input bits searched: {len(raw)}.\n"
                      "Ensure the analyzer includes the entire transmission and "
                      "that Mark/Space and baud are configured correctly. The estimated data-start marker is not used to trim MDC input.")
        }

    lines = [
        f"Input bits searched: {len(raw)}",
        "Sync candidates (errors, bit offset, input polarity, sync inverted):"
    ]
    for errors, start, polarity, inverted, _ in candidates[:8]:
        lines.append(f"  errors={errors}, offset={start}, polarity={'normal' if polarity == 0 else 'inverted'}, inverted_sync={inverted}")

    # Decode the strongest/earliest candidate first; firmware allows normal or
    # inverted sync indication but normalizes FIFO data before FEC processing.
    for errors, start, polarity, inverted, differential in candidates:
        payload_start = start + 40
        if len(differential) - payload_start < PAYLOAD_BITS:
            continue
        payload_bits = differential[payload_start:payload_start + PAYLOAD_BITS]
        # If the sync matched inverted, normalize the following FIFO bits too.
        if inverted:
            payload_bits = [1 - b for b in payload_bits]
        encoded = _bytes_msb(payload_bits)
        corrected, received_crc, calculated_crc, flipped = _decode_fec(encoded)
        if corrected is None:
            continue
        op, arg, unit_hi, unit_lo = corrected[:4]
        unit_id = (unit_hi << 8) | unit_lo
        value = (
            "MDC1200 PACKET VALID\n"
            f"Opcode: 0x{op:02X}\n"
            f"Argument: 0x{arg:02X}\n"
            f"Unit ID: {unit_id:04X} ({unit_id})\n"
            f"CRC: {received_crc:04X} (firmware CRC: {calculated_crc:04X})"
        )
        debug = "\n".join(lines + [
            f"Selected sync offset: {start}",
            f"Sync bit errors: {errors}",
            f"Input polarity tried: {'normal' if polarity == 0 else 'inverted'}",
            f"Sync polarity: {'inverted' if inverted else 'normal'}",
            f"Encoded payload length: {len(encoded) * 8} bits",
            f"FEC: passed; residual single-bit retry: {'none' if flipped is None else 'coded bit ' + str(flipped)}",
            "Decoded bytes: " + " ".join(f"{b:02X}" for b in corrected[:7]),
            "Firmware CRC validation: PASS"
        ])
        return {"value": value, "debug": debug}

    available = max(0, len(raw) - (candidates[0][1] + 40))
    return {
        "value": "MDC1200 sync found, but payload could not be validated",
        "debug": "\n".join(lines + [
            f"Bits available after best sync: {available}; required: {PAYLOAD_BITS}.",
            "A complete MDC codeword contains 40 sync bits followed by 112 encoded payload bits.",
            "If at least 112 bits are present, FEC/CRC failed. Check bit mapping, symbol timing, and whether the recording contains the complete packet."
        ])
    }
