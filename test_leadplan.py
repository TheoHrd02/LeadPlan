import json
import unittest
from datetime import date

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

    def test_sirene_diff(self):
        today = date(2026, 10, 22)
        found = {"1": {"date_creation": "2026-10-01"}, "2": {"date_creation": "2010-01-01"}}
        seen = {}
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [])  # premier passage = baseline
        found |= {"3": {"date_creation": "2026-10-21"}, "4": {"date_creation": "2015-03-01"}}
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [found["3"]])
        self.assertEqual(seen["4"]["status"], "old")
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [])  # déjà vus

    def test_bodacc_relevant(self):
        # Activités réelles parues au BODACC dans le Var (octobre 2026).
        leads = [
            "Coiffure mixte en salon (hommes, femmes, enfants) et barbier : coupe, coiffage, coloration",
            "Coiffure en salon, Achat vente d'accessoires et produits liés à l'activité",
            "coiffure - vente de produits et accessoires",
            "achat et vente de véhicules légers d'occasion - prothésiste ongulaire sans acte de manucure",
            "Pose et entretien d'extensions de cils. - Coursiers à vélo - livraisons de repas a domicile",
            "Pratique de techniques manuelles de bien-être et de lâcher-prise, drainage lymphatique",
            "Épilation au laser à visée non thérapeutique réalisée par des infirmières diplômées d'État",
            "Esthétique en institut et vente de produits de beauté",
            "Perçage corporel - Vente de bijoux, piercings, objets tibétains",
            "fabrication et vente d'articles textiles - prothesie ongulaire pose de vernis et beauté des ongles a domicile",
            "Vente à distance avec Closers Group en spécialité de closing par téléphone et visio - Naturopathie accompagnement",
        ]
        noise = [
            "commerce de détail en ligne, vente à distance de produits de beauté, cosmétiques, maquillage, accessoires de coiffure",
            "Création et commercialisation en ligne de contenus numériques dans le domaine du maquillage et de la beauté",
            "conseil en design et architecture d'intérieur, décoration, agencement d'espaces ; suivi esthétique",
            "Tous travaux de pavage ; La préparation, le terrassement, l'assainissement, le drainage et tous travaux",
            "Vente au détail de produits de mode, beauté, accessoires et autres produits de marques",
            "Vente à distance au détail de produits de beauté, d'hygiène et de bien-être (cosmétiques)",
            "Achat et vente au détail sur internet d'accessoires sportifs, de matériel de massage",
            "Intermédiaire de commerce de détail de ligne, parfums, hygiène beauté, produits ménagers",
            "Vente produits de beauté et bien être - E commerce",
            "Coach sportif",
        ]
        for activity in leads:
            self.assertTrue(leadplan.bodacc_relevant(activity), activity)
        for activity in noise:
            self.assertFalse(leadplan.bodacc_relevant(activity), activity)

    def test_bodacc_diff_dedup(self):
        def record(siren, famille="creation", activity="coiffure en salon"):
            return {"familleavis": famille, "listeetablissements": json.dumps({"etablissement": {"activite": activity}}),
                    "listepersonnes": json.dumps({"personne": {"numeroImmatriculation": {"numeroIdentification": siren}}})}

        found = {"a": record("111 111 111"), "b": record("222 222 222"), "c": record("222 222 222"),
                 "d": record("333 333 333", "vente"), "e": record("444 444 444", activity="vente en ligne de cosmétiques")}
        seen = {}
        leads = leadplan.bodacc_diff(found, seen, date(2026, 10, 22), sirene_sirens={"111111111", "333333333"})
        self.assertEqual([r["siren"] for r in leads], ["222222222", "333333333"])  # reprise gardée même si connue
        self.assertEqual({k: v["status"] for k, v in seen.items()},
                         {"a": "dup", "b": "sent", "c": "dup", "d": "sent", "e": "off_target"})


if __name__ == "__main__":
    unittest.main()
