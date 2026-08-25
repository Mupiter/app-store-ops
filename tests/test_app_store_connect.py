import unittest

from scripts import app_store_connect


class AppStoreConnectTests(unittest.TestCase):
    def assert_invalid_signature(self, der):
        with self.assertRaisesRegex(ValueError, "invalid ECDSA signature"):
            app_store_connect.der_to_jose(der)

    def test_b64url_removes_padding(self):
        self.assertEqual(app_store_connect.b64url(b"a"), "YQ")

    def test_der_to_jose_converts_ecdsa_integers_to_fixed_width_values(self):
        der = b"\x30\x08\x02\x01\x01\x02\x03\x01\x02\x03"

        signature = app_store_connect.der_to_jose(der)

        self.assertEqual(signature[:32], b"\0" * 31 + b"\x01")
        self.assertEqual(signature[32:], b"\0" * 29 + b"\x01\x02\x03")

    def test_der_to_jose_strips_required_sign_padding(self):
        der = b"\x30\x07\x02\x02\x00\x80\x02\x01\x01"

        signature = app_store_connect.der_to_jose(der)

        self.assertEqual(signature[:32], b"\0" * 31 + b"\x80")

    def test_der_to_jose_rejects_malformed_structure(self):
        invalid_signatures = (
            b"not DER",
            b"\x30\x06\x02\x01\x01\x02\x01",
            b"\x30\x06\x02\x01\x01\x02\x01\x01\x00",
            b"\x30\x81\x06\x02\x01\x01\x02\x01\x01",
            b"\x30\x05\x02\x00\x02\x01\x01",
        )

        for der in invalid_signatures:
            with self.subTest(der=der):
                self.assert_invalid_signature(der)

    def test_der_to_jose_rejects_noncanonical_or_invalid_scalars(self):
        invalid_signatures = (
            b"\x30\x06\x02\x01\x80\x02\x01\x01",
            b"\x30\x07\x02\x02\x00\x01\x02\x01\x01",
            b"\x30\x06\x02\x01\x00\x02\x01\x01",
            b"\x30\x26\x02\x21\x01" + b"\0" * 32 + b"\x02\x01\x01",
        )

        for der in invalid_signatures:
            with self.subTest(der=der):
                self.assert_invalid_signature(der)


if __name__ == "__main__":
    unittest.main()
