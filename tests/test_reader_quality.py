from __future__ import annotations

import unittest

from web_research.readers.quality import (
    assess_html_quality,
    page_diagnostics,
    rendering_signals,
)


class HTMLQualityAssessmentTests(unittest.TestCase):
    def test_short_static_notice_is_complete(self) -> None:
        assessment = assess_html_quality(
            "<html><main><p>Version 4.2 released today.</p></main></html>",
            "Version 4.2 released today.",
        )

        self.assertFalse(assessment.browser_recommended)

    def test_noscript_javascript_requirement_recommends_browser(self) -> None:
        assessment = assess_html_quality(
            "<html><body><div id='root'></div><noscript>Please enable JavaScript.</noscript>"
            "<script src='/app.js'></script></body></html>",
            "Example application",
        )

        self.assertIn("javascript_required", assessment.browser_reasons)
        self.assertIn("empty_app_shell", assessment.browser_reasons)

    def test_empty_next_root_recommends_browser(self) -> None:
        assessment = assess_html_quality(
            "<html><body><nav>Products</nav><div id='__next'></div>"
            "<script src='/bundle.js'></script></body></html>",
            "Products",
        )

        self.assertEqual(assessment.browser_reasons, ("empty_app_shell",))

    def test_script_only_body_recommends_browser_even_when_title_was_extracted(self) -> None:
        assessment = assess_html_quality(
            "<html><head><title>Application</title></head><body>"
            "<custom-shell></custom-shell><script src='/bundle.js'></script></body></html>",
            "Application",
            title="Application",
            status_code=200,
        )

        self.assertEqual(assessment.browser_reasons, ("script_only_page",))

    def test_server_rendered_app_root_does_not_recommend_browser(self) -> None:
        assessment = assess_html_quality(
            "<html><body><div id='__next'><main><h1>Release 4.2</h1>"
            "<p>Available now.</p></main></div><script src='/bundle.js'></script></body></html>",
            "# Release 4.2\n\nAvailable now.",
        )

        self.assertFalse(assessment.browser_recommended)

    def test_article_discussing_javascript_is_not_mistaken_for_shell(self) -> None:
        content = (
            "Modern browsers enable JavaScript optimizations automatically. This article explains "
            "how a framework requires JavaScript developers to configure the compiler."
        )

        self.assertEqual(rendering_signals(content), ())

    def test_loading_placeholder_recommends_browser(self) -> None:
        assessment = assess_html_quality(
            "<html><body><div id='app'>Loading...</div>"
            "<script src='/app.js'></script></body></html>",
            "Loading...",
        )

        self.assertIn("loading_placeholder", assessment.browser_reasons)

    def test_waiting_for_javascript_placeholder_recommends_browser(self) -> None:
        assessment = assess_html_quality(
            "<html><main><h1>Waiting for JavaScript</h1></main><script>render()</script></html>",
            "Waiting for JavaScript",
        )

        self.assertIn("loading_placeholder", assessment.browser_reasons)

    def test_static_empty_page_has_no_extracted_content(self) -> None:
        assessment = assess_html_quality("<html><body></body></html>", "")

        self.assertEqual(assessment.browser_reasons, ("no_extracted_content",))

    def test_cloudflare_525_page_is_diagnosed_despite_success_status(self) -> None:
        diagnostics = page_diagnostics(
            "pi.dev | 525: SSL handshake failed",
            "Cloudflare is unable to establish an SSL connection to the origin server.",
            status_code=200,
        )

        self.assertEqual(diagnostics, ("cloudflare_525",))

    def test_soft_404_title_is_diagnosed(self) -> None:
        diagnostics = page_diagnostics(
            "Documentation | Page Not Found",
            "Return to the documentation home page.",
            status_code=200,
        )

        self.assertEqual(diagnostics, ("soft_404",))

    def test_article_about_server_errors_is_not_diagnosed_from_body_alone(self) -> None:
        diagnostics = page_diagnostics(
            "Troubleshooting reverse proxies",
            "This guide explains 502 Bad Gateway and 525 SSL handshake failures.",
            status_code=200,
        )

        self.assertEqual(diagnostics, ())


if __name__ == "__main__":
    unittest.main()
