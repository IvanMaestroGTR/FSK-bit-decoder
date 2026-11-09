import unittest

from decoders import fleetsync


def bits(value, width):
    return [(value >> shift) & 1 for shift in range(width - 1, -1, -1)]


def valid_fs1_frame(word1=0xFE800830, word2=0x53059B6C):
    assert fleetsync._fsync_block_check(word1, word2) == (word2 & 0xFFFF)
    return bits(0xAAAA23EB, 32) + bits(word1, 32) + bits(word2, 32)


def make_codeword(nibble):
    data = (nibble & 0xF) << 8
    for parity in range(1 << 11):
        candidate = data | parity
        syndrome = 0
        for position, column in enumerate(fleetsync._FS2_COLUMNS):
            if candidate & (1 << (14 - position)):
                syndrome ^= column
        if syndrome == 0:
            return candidate
    raise AssertionError("No codeword found")


def valid_fs2_frame(word1=0xFE800830, word2=0x53059B6C, flip_one=False):
    assert fleetsync._fsync_block_check(word1, word2) == (word2 & 0xFFFF)
    data = word1.to_bytes(4, "big") + word2.to_bytes(4, "big")
    nibbles = []
    for byte in data:
        nibbles.extend((byte >> 4, byte & 0xF))

    frame = bits(0xAAAA23EB, 32)
    for block in range(4):
        logical = [make_codeword(n) for n in nibbles[block * 4:(block + 1) * 4]]
        chronological = [logical[1], logical[0], logical[3], logical[2]]
        if flip_one and block == 1:
            chronological[2] ^= 1 << 9
        for codeword in chronological:
            frame.extend(bits(codeword, 16))
    return frame


class FleetSyncTests(unittest.TestCase):
    def test_fleetsync_i_ani_and_block_check(self):
        result = fleetsync.decode({"all_bits": valid_fs1_frame(), "baud": 1200})
        self.assertIn("FleetSync I ANI", result["value"])
        self.assertIn("Fleet: 107", result["value"])
        self.assertIn("Unit: 1772", result["value"])
        self.assertIn("Block check: VALID", result["value"])

    def test_fleetsync_ii_ecc_repairs_one_bit(self):
        result = fleetsync.decode({"all_bits": valid_fs2_frame(flip_one=True), "baud": 1200})
        self.assertIn("FleetSync II ANI", result["value"])
        self.assertIn("Fleet: 107", result["value"])
        self.assertIn("Unit: 1772", result["value"])
        self.assertIn("Corrected codewords: 1", result["debug"])

    def test_fs2_single_bit_repair_for_every_codeword_position(self):
        codeword = make_codeword(0xA)
        for bit_position in range(15):
            with self.subTest(bit_position=bit_position):
                repaired = fleetsync._fs2_ecc_repair(codeword ^ (1 << bit_position))
                self.assertEqual(repaired, codeword)

    def test_both_sync_words(self):
        for sync in (0xAAAA23EB, 0xAAAA052B):
            frame = bits(sync, 32) + bits(0xFE800830, 32) + bits(0x53059B6C, 32)
            result = fleetsync.decode({"all_bits": frame, "baud": 1200})
            self.assertIn("Fleet: 107", result["value"])

    def test_sync_error_limit(self):
        frame = valid_fs1_frame()
        frame[2] ^= 1
        frame[20] ^= 1
        result = fleetsync.decode({"all_bits": frame, "baud": 1200})
        self.assertIn("Fleet: 107", result["value"])
        self.assertIn("errors: 2", result["debug"])

    def test_inverted_input_polarity(self):
        frame = [bit ^ 1 for bit in valid_fs1_frame()]
        result = fleetsync.decode({"all_bits": frame, "baud": 1200})
        self.assertIn("Fleet: 107", result["value"])
        self.assertIn("Unit: 1772", result["value"])

    def test_no_false_valid_packet_on_truncated_frame(self):
        result = fleetsync.decode({"all_bits": valid_fs1_frame()[:60], "baud": 1200})
        self.assertNotIn("Block check: VALID", result["value"])

    def test_bad_fs1_block_check_is_not_reported_valid(self):
        frame = valid_fs1_frame()
        frame[-1] ^= 1
        result = fleetsync.decode({"all_bits": frame, "baud": 1200})
        self.assertNotIn("Block check: VALID", result["value"])

    def test_reject_unsupported_baud(self):
        result = fleetsync.decode({"all_bits": valid_fs1_frame(), "baud": 4800})
        self.assertIn("Unsupported FleetSync baud", result["value"])


if __name__ == "__main__":
    unittest.main()
