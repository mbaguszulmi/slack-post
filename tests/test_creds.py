import json
import unittest

from slackpost import creds


class ParseCredentialsTests(unittest.TestCase):
    def test_roundtrip(self):
        payload = json.dumps(
            {
                "Token": "xoxc-123-456-789-abc",
                "Cookie": [
                    {"Name": "d", "Value": "xyz"},
                    {"Name": "d-s", "Value": "123"},
                ],
            }
        ).encode()
        c = creds.parse_credentials(payload)
        self.assertEqual(c.token, "xoxc-123-456-789-abc")
        self.assertEqual(c.cookie_header(), "d=xyz; d-s=123")
        self.assertEqual(c.token_prefix, "xoxc")
        self.assertNotIn("xoxc-123-456-789-abc", repr(c))

    def test_missing_token(self):
        with self.assertRaises(creds.CredentialError):
            creds.parse_credentials(b'{"Cookie": []}')

    def test_bad_json(self):
        with self.assertRaises(creds.CredentialError):
            creds.parse_credentials(b"not json")


class KeyTests(unittest.TestCase):
    def test_deterministic(self):
        a = creds.derive_key("test-machine")
        b = creds.derive_key("test-machine")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)
        self.assertNotEqual(creds.derive_key("other-machine"), a)


class DecryptTests(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile
        from pathlib import Path
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

        key = creds.derive_key("test-machine")
        iv = b"\x00" * 16
        plaintext = b'{"Token":"xoxc-1","Cookie":[]}'
        enc = Cipher(algorithms.AES(key), creds.CFB(iv)).encryptor()
        data = iv + enc.update(plaintext) + enc.finalize()

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ws.bin"
            path.write_bytes(data)
            self.assertEqual(creds.decrypt_file(path, key), plaintext)

    def test_too_short(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "short.bin"
            path.write_bytes(b"abc")
            with self.assertRaises(creds.CredentialError):
                creds.decrypt_file(path, b"0" * 32)


if __name__ == "__main__":
    unittest.main()
