from __future__ import annotations

import unittest

from web_research.citations import CitationError, append_sources, validate_citations
from web_research.models import Source, SourceClass


class CitationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = [
            Source(
                id="S1",
                url="https://example.com/a",
                title="Source A",
                domain="example.com",
                source_class=SourceClass.PRIMARY,
                retrieved_at="2026-08-22T00:00:00Z",
            )
        ]

    def test_known_citation_is_valid(self) -> None:
        validate_citations("The specification is documented. [S1]", self.sources)

    def test_unknown_citation_is_rejected(self) -> None:
        with self.assertRaises(CitationError):
            validate_citations("Unsupported. [S2]", self.sources)

    def test_source_section_is_deterministic(self) -> None:
        answer = append_sources("Answer [S1]", self.sources)
        self.assertIn("[Source A](https://example.com/a)", answer)


if __name__ == "__main__":
    unittest.main()
