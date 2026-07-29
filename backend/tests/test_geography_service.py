import unittest

from app.services.geography_service import infer_geography


class GeographyServiceTest(unittest.TestCase):
    def test_brazil_city_and_state_code(self):
        match = infer_geography(
            "R. T - 39, 1200 - Setor Bueno, Goiania - GO, 74210-100, Brasil",
            "BR",
        )
        self.assertEqual(match.estado, "Goias")
        self.assertEqual(match.cidade, "Goiania")
        self.assertEqual(match.confidence, "high")

    def test_brazil_state_as_separate_segment(self):
        match = infer_geography(
            "Av. Trompowsky, 354 - Centro, Florianopolis, SC, 88015-300",
            "BR",
        )
        self.assertEqual(match.estado, "Santa Catarina")
        self.assertEqual(match.cidade, "Florianopolis")

    def test_mexico_postal_city_and_state(self):
        match = infer_geography(
            "Av. Bonampak 10, 77500 Puerto Cancun, Q.R., Mexico",
            "MX",
        )
        self.assertEqual(match.estado, "Quintana Roo")
        self.assertEqual(match.cidade, "Puerto Cancun")
        self.assertEqual(match.confidence, "high")

    def test_uk_known_city(self):
        match = infer_geography("22 Baker Street, London W1U 3BW, UK", "UK")
        self.assertEqual(match.estado, "England")
        self.assertEqual(match.cidade, "London")

    def test_us_city_state_zip(self):
        match = infer_geography("100 Biscayne Blvd, Miami, FL 33132, USA", "US")
        self.assertEqual(match.estado, "Florida")
        self.assertEqual(match.cidade, "Miami")


if __name__ == "__main__":
    unittest.main()
