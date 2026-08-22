from __future__ import annotations

import unittest

from web_research.safety.urls import (
    UnsafeUrlError,
    canonicalize_url,
    registrable_domain,
    validate_public_url,
)


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_canonicalize_removes_tracking_and_fragment(self) -> None:
        value = canonicalize_url(
            "HTTPS://Example.COM:443/products/?utm_source=test&b=2&a=1#details"
        )
        self.assertEqual(value, "https://example.com/products?a=1&b=2")

    async def test_private_ip_is_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url("http://127.0.0.1/secret")

    async def test_private_ip_can_be_enabled_for_development(self) -> None:
        result = await validate_public_url("http://127.0.0.1/test", allow_private=True)
        self.assertEqual(result.host, "127.0.0.1")

    async def test_non_http_scheme_is_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url("file:///etc/passwd")

    def test_source_family_uses_registrable_domain(self) -> None:
        self.assertEqual(
            registrable_domain("https://support.example.co.uk/article"), "example.co.uk"
        )
        self.assertEqual(registrable_domain("https://www.example.co.uk/shop"), "example.co.uk")


if __name__ == "__main__":
    unittest.main()
