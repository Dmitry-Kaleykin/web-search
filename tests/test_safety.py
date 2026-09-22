from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from web_research.safety.urls import (
    UnsafeUrlError,
    canonicalize_url,
    validate_public_url,
)


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_canonicalize_preserves_path_and_query_but_removes_fragment(self) -> None:
        value = canonicalize_url(
            "HTTPS://Example.COM:443/products/?utm_source=test&b=2&a=1#details"
        )
        self.assertEqual(value, "https://example.com/products/?utm_source=test&b=2&a=1")

    async def test_private_ip_is_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url("http://127.0.0.1/secret")

    async def test_private_ip_can_be_enabled_for_development(self) -> None:
        result = await validate_public_url("http://127.0.0.1/test", allow_private=True)
        self.assertEqual(result.host, "127.0.0.1")

    async def test_proxy_fake_ip_can_be_enabled_for_a_hostname(self) -> None:
        with patch(
            "web_research.safety.urls._resolve",
            new=AsyncMock(return_value=("198.18.0.49",)),
        ):
            result = await validate_public_url(
                "https://example.com/article",
                allow_proxy_fake_ips=True,
            )
        self.assertEqual(result.addresses, ("198.18.0.49",))

    async def test_disabled_proxy_compatibility_reports_specific_configuration(self) -> None:
        with (
            patch(
                "web_research.safety.urls._resolve", new=AsyncMock(return_value=("198.18.0.49",))
            ),
            self.assertRaisesRegex(UnsafeUrlError, "WEB_SEARCH_ALLOW_PROXY_FAKE_IPS=true"),
        ):
            await validate_public_url("https://example.com/article")

    async def test_literal_proxy_fake_ip_remains_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url(
                "http://198.18.0.49/secret",
                allow_proxy_fake_ips=True,
            )

    async def test_proxy_fake_ip_option_does_not_allow_private_networks(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url(
                "http://192.168.1.1/secret",
                allow_proxy_fake_ips=True,
            )

    async def test_non_http_scheme_is_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            await validate_public_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
