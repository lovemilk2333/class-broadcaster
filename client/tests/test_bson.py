import unittest

from client.core.protocol.bson import BSONError, decode, encode


class BSONTest(unittest.TestCase):
    def test_round_trip_protocol_values(self):
        value = {"nonce": b"abc", "major": 1, "enabled": True, "name": "屏幕", "items": [1, None]}
        self.assertEqual(decode(encode(value)), value)

    def test_rejects_trailing_data(self):
        with self.assertRaises(BSONError):
            decode(encode({"x": 1}) + b"x")


if __name__ == "__main__":
    unittest.main()
