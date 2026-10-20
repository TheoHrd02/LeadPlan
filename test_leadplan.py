import unittest

import leadplan


class LeadPlanTest(unittest.TestCase):
    def test_search_stays_on_free_sku(self):
        # Tout champ en plus fait passer le balayage en SKU payant.
        self.assertEqual(leadplan.SEARCH_FIELDS, "places.id,nextPageToken")

    def test_grid_and_split_cover_bbox(self):
        cells = leadplan.grid(leadplan.VAR_BBOX)
        self.assertEqual(len(cells), 9 * 13)
        self.assertEqual(cells[0][:2], leadplan.VAR_BBOX[:2])
        self.assertEqual(cells[-1][2:], leadplan.VAR_BBOX[2:])
        subs = leadplan.split((43.0, 6.0, 43.1, 6.1))
        self.assertEqual({s[:2] for s in subs}, {(43.0, 6.0), (43.0, 6.05), (43.05, 6.0), (43.05, 6.05)})

    def test_classify(self):
        def place(cp, status="OPERATIONAL"):
            return {"addressComponents": [{"types": ["postal_code"], "longText": cp}], "businessStatus": status}

        self.assertEqual(leadplan.classify(place("83000")), "sent")
        self.assertEqual(leadplan.classify(place("13600")), "out_of_zone")
        self.assertEqual(leadplan.classify(place("83400", "CLOSED_PERMANENTLY")), "closed")
        self.assertEqual(leadplan.classify({"formattedAddress": "1 Rue X, 83990 Saint-Tropez"}), "sent")
        self.assertEqual(leadplan.classify(None), "gone")
        for off in ("physiotherapist", "hotel", "campground", "restaurant", "general_contractor"):
            self.assertEqual(leadplan.classify({**place("83000"), "primaryType": off}), "off_target")
        self.assertEqual(leadplan.classify({**place("83000"), "primaryType": "body_art_service"}), "sent")


if __name__ == "__main__":
    unittest.main()
