import io
import unittest

from client.core.protocol.packet import Packet, PacketError, _read_exact, read_packet


class _ShortReadStream:
    """Mimic ssl.SSLSocket: each read() returns at most ``chunk`` bytes."""

    def __init__(self, data: bytes, chunk: int = 3) -> None:
        self._data = data
        self._offset = 0
        self._chunk = max(1, chunk)

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        if size is None or size < 0:
            size = len(self._data) - self._offset
        take = min(size, self._chunk, len(self._data) - self._offset)
        start = self._offset
        self._offset += take
        return self._data[start : self._offset]


class PacketTest(unittest.TestCase):
    def test_round_trip(self):
        packet = Packet(1, 0, 0x1001, 42, 3, b"hello")
        encoded = packet.encode()
        self.assertEqual(Packet.decode(encoded), packet)
        self.assertEqual(read_packet(io.BytesIO(encoded)), packet)

    def test_read_packet_tolerates_short_ssl_reads(self):
        # Large-ish payload (update chunks) plus tiny reads like TLS records.
        payload = b"x" * 4096
        packet = Packet(1, 0, 0x0209, 1, 0, payload)
        encoded = packet.encode()
        decoded = read_packet(_ShortReadStream(encoded, chunk=7))
        self.assertEqual(decoded, packet)

    def test_read_exact_raises_on_true_eof(self):
        with self.assertRaises(EOFError):
            _read_exact(io.BytesIO(b"MKCB"), 16)

    def test_rejects_bad_magic(self):
        with self.assertRaises(PacketError):
            Packet.decode(b"NOPE" + bytes(12))

    def test_rejects_trailing_bytes(self):
        packet = Packet(1, 0, 1, 1, 0).encode()
        with self.assertRaises(PacketError):
            Packet.decode(packet + b"extra")


if __name__ == "__main__":
    unittest.main()
