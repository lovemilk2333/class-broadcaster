import io
import unittest

from client.core.protocol.packet import Packet, PacketError, read_packet


class PacketTest(unittest.TestCase):
    def test_round_trip(self):
        packet = Packet(1, 0, 0x1001, 42, 3, b"hello")
        encoded = packet.encode()
        self.assertEqual(Packet.decode(encoded), packet)
        self.assertEqual(read_packet(io.BytesIO(encoded)), packet)

    def test_rejects_bad_magic(self):
        with self.assertRaises(PacketError):
            Packet.decode(b"NOPE" + bytes(12))

    def test_rejects_trailing_bytes(self):
        packet = Packet(1, 0, 1, 1, 0).encode()
        with self.assertRaises(PacketError):
            Packet.decode(packet + b"extra")


if __name__ == "__main__":
    unittest.main()
