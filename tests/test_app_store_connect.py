import unittest

from scripts import app_store_connect


class AppStoreConnectTests(unittest.TestCase):
    def test_b64url_removes_padding(self):
        self.assertEqual(app_store_connect.b64url(b"a"), "YQ")

    def test_der_to_jose_converts_ecdsa_integers_to_fixed_width_values(self):
        der = b"\x30\x09\x02\x02\x00\x01\x02\x03\x01\x02\x03"

        signature = app_store_connect.der_to_jose(der)

        self.assertEqual(signature[:32], b"\0" * 31 + b"\x01")
        self.assertEqual(signature[32:], b"\0" * 29 + b"\x01\x02\x03")

    def test_der_to_jose_rejects_a_malformed_signature(self):
        with self.assertRaisesRegex(ValueError, "invalid ECDSA signature"):
            app_store_connect.der_to_jose(b"not DER")


if __name__ == "__main__":
    unittest.main()
