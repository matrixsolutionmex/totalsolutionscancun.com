import unittest

from app.services.enrichment_service import EnrichmentError, extract_social_links, normalize_site_url


class EnrichmentServiceTest(unittest.TestCase):
    def test_extracts_social_links_from_anchors_and_open_graph_meta(self):
        html = """
        <html>
          <head>
            <meta property="og:see_also" content="https://www.facebook.com/acme/" />
          </head>
          <body>
            <a href="https://instagram.com/acme/?utm_source=site">Instagram</a>
            <a href="https://www.linkedin.com/company/acme/">LinkedIn</a>
          </body>
        </html>
        """

        result = extract_social_links(html, "https://acme.example")

        self.assertEqual(result["instagram"], "https://instagram.com/acme")
        self.assertEqual(result["linkedin"], "https://www.linkedin.com/company/acme")
        self.assertEqual(result["facebook"], "https://www.facebook.com/acme")

    def test_ignores_social_share_links(self):
        html = """
        <a href="https://www.facebook.com/sharer/sharer.php?u=https://acme.example">Share</a>
        <a href="https://www.linkedin.com/sharing/share-offsite/?url=https://acme.example">Share</a>
        """

        self.assertEqual(extract_social_links(html, "https://acme.example"), {})

    def test_adds_https_when_site_has_no_scheme(self):
        self.assertEqual(normalize_site_url("acme.example"), "https://acme.example")

    def test_rejects_non_http_url(self):
        with self.assertRaises(EnrichmentError):
            normalize_site_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
