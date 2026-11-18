import argparse
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import leadplan


class LeadPlanTest(unittest.TestCase):
    def test_search_stays_on_free_sku(self):
        # Tout champ en plus fait passer le balayage en SKU payant.
        self.assertEqual(leadplan.SEARCH_FIELDS, "places.id,nextPageToken")

    def test_grid_and_split_cover_bbox(self):
        cells = leadplan.grid(leadplan.REGIONS["83"][1])
        self.assertEqual(len(cells), 9 * 13)
        self.assertEqual(cells[0][:2], leadplan.REGIONS["83"][1][:2])
        self.assertEqual(cells[-1][2:], leadplan.REGIONS["83"][1][2:])
        subs = leadplan.split((43.0, 6.0, 43.1, 6.1))
        self.assertEqual({s[:2] for s in subs}, {(43.0, 6.0), (43.0, 6.05), (43.05, 6.0), (43.05, 6.05)})

    def test_classify(self):
        def place(cp, status="OPERATIONAL"):
            return {"addressComponents": [{"types": ["postal_code"], "longText": cp}], "businessStatus": status}

        self.assertEqual(leadplan.classify(place("83000"), "83"), "sent")
        self.assertEqual(leadplan.classify(place("13600"), "83"), "out_of_zone")
        self.assertEqual(leadplan.classify(place("06400"), "06"), "sent")
        self.assertEqual(leadplan.classify(place("83000"), "06"), "out_of_zone")
        self.assertEqual(leadplan.classify(place("83400", "CLOSED_PERMANENTLY"), "83"), "closed")
        self.assertEqual(leadplan.classify({"formattedAddress": "1 Rue X, 83990 Saint-Tropez"}, "83"), "sent")
        self.assertEqual(leadplan.classify(None, "83"), "gone")
        for off in ("physiotherapist", "hotel", "campground", "restaurant", "general_contractor"):
            self.assertEqual(leadplan.classify({**place("83000"), "primaryType": off}, "83"), "off_target")
        self.assertEqual(leadplan.classify({**place("83000"), "primaryType": "body_art_service"}, "83"), "sent")
        for name in ("Maureen Erhardy Ostéopathe D.O.", "Cabinet Sage Femme CASTILLO", "CABINET INFIRMIER ESCRIHUELA",
                     "Fayoux Caroline - Cabinet de psychothérapie", "Provencianne aide à domicile"):
            self.assertEqual(leadplan.classify({**place("83000"), "displayName": {"text": name}}, "83"), "off_target", name)
        self.assertEqual(leadplan.classify({**place("83000"), "displayName": {"text": "Myriam Castrec Réflexologue"}}, "83"),
                         "sent")

    def test_too_old(self):
        today = date(2026, 10, 28)
        reviews = lambda *days: {"reviews": [{"publishTime": f"{d}T10:00:00Z"} for d in days]}
        self.assertTrue(leadplan.too_old(reviews("2026-10-20", "2021-03-18"), today))  # un avis de 2021 suffit
        self.assertTrue(leadplan.too_old(reviews("2025-12-29"), today))   # envoyé à tort le 29/10 (seuil d'un an)
        self.assertTrue(leadplan.too_old(reviews("2026-07-20"), today))   # 3 mois : pas « nouveau »
        self.assertFalse(leadplan.too_old(reviews("2026-10-24", "2026-10-02"), today))
        self.assertFalse(leadplan.too_old({}, today))  # aucun avis : âge inconnu, gardé

    def test_sirene_diff(self):
        today = date(2026, 10, 22)
        found = {"1": {"date_creation": "2026-10-01"}, "2": {"date_creation": "2010-01-01"}}
        seen = {}
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [])  # premier passage = baseline
        found |= {"3": {"date_creation": "2026-10-21"}, "4": {"date_creation": "2015-03-01"}}
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [found["3"]])
        self.assertEqual(seen["4"]["status"], "old")
        self.assertEqual(leadplan.sirene_diff(found, seen, today), [])  # déjà vus

    def test_insee_etab(self):
        raw = {"siret": "12345678900011", "dateCreationEtablissement": "2026-10-20",
               "activitePrincipaleNAF25Etablissement": "96.22Y",
               "uniteLegale": {"denominationUniteLegale": None, "prenom1UniteLegale": "JULIE", "nomUniteLegale": "MARTIN"},
               "adresseEtablissement": {"numeroVoieEtablissement": "12", "typeVoieEtablissement": "RUE",
                                        "libelleVoieEtablissement": "DES LILAS", "codePostalEtablissement": "83000",
                                        "libelleCommuneEtablissement": "TOULON"},
               "periodesEtablissement": [{"etatAdministratifEtablissement": "A", "activitePrincipaleEtablissement": "96.02B",
                                          "enseigne1Etablissement": "JULIE BEAUTE"}]}
        etab = leadplan.insee_etab(raw)
        self.assertEqual((etab["nom_commercial"], etab["nom_complet"], etab["adresse"]),
                         ("JULIE BEAUTE", "JULIE MARTIN", "12 RUE DES LILAS 83000 TOULON"))
        self.assertEqual(leadplan.sirene_row(etab)["activite"], "Soins de beauté")
        raw["periodesEtablissement"][0]["etatAdministratifEtablissement"] = "F"  # fermé depuis
        self.assertIsNone(leadplan.insee_etab(raw))

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

    def test_sweep_refines_unsaturated_cells(self):
        # Google tronque sans prévenir : la fiche peu visible ne sort que dans une cellule assez petite.
        def search_cell(query, place_type, cell):
            s, w, n, e = cell
            small = n - s < 0.03
            ids = {f"{cell}-{i}" for i in range(2 if small else 10)}
            return ids | ({"cachée"} if small and s <= 43.01 < n and w <= 6.01 < e else set()), 1

        top = [((43.0, 6.0, 43.1, 6.1), 0)]
        with mock.patch.object(leadplan, "search_cell", search_cell):
            ids, requests, todo = leadplan.sweep_query("q", None, top)  # balayage de base : 10 < 60, pas de découpage
            self.assertEqual((len(ids), requests, len(todo)), (10, 1, 4))
            more, requests, left = leadplan.sweep_query("q", None, todo, refine=True)
            self.assertIn("cachée", more)
            self.assertEqual((requests, left), (4 + 16, []))  # s'arrête sous SPLIT_AT fiches
            # Limite de temps dépassée (API lente) : rien n'est fait, les cellules restent à faire.
            self.assertEqual(leadplan.sweep_query("q", None, todo, refine=True, deadline=0), (set(), 0, todo))

    def test_changed_sweep_indexes_silently(self):
        # Un balayage modifié fait remonter d'un coup des fiches qui existaient déjà : ni leads, ni appels payants.
        sent, found, complete = [], {"connue", "a", "b"}, [False, True, True]
        lookup = mock.Mock(return_value=("sent", {"displayName": {"text": "Salon neuf"}}))
        args = argparse.Namespace(bbox=None, test_email=False, baseline=False, dry_run=False)
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.dict("os.environ", {"GMAIL_ADDRESS": "x@example.com"}),                 mock.patch.multiple(leadplan, DATA=Path(tmp), USAGE_FILE=Path(tmp) / "usage.json", lookup=lookup,
                                    sweep=lambda *a: (set(found), complete.pop(0)), sirene_fetch=lambda *a: {},
                                    bodacc_fetch=lambda *a: {}, send=sent.append):
            leadplan.save_json(leadplan.state_file("83", "seen.json"), {"connue": {"status": "baseline"}})
            used, today = {"pro": 0, "enterprise": 0}, date(2026, 11, 19)
            leadplan.run_region("83", args, today, {}, used)  # état écrit par l'ancien balayage
            seen = leadplan.load_json(leadplan.state_file("83", "seen.json"))
            self.assertEqual({seen[k]["status"] for k in "ab"}, {"baseline"})
            found.add("c")  # affinage coupé par la limite de temps la veille : le reste remonte, toujours en silence
            leadplan.run_region("83", args, today + timedelta(days=1), {}, used)
            lookup.assert_not_called()
            self.assertIn("0 fiche Google", sent[1]["Subject"])

            found.add("d")  # balayage inchangé et complet la veille : une vraie nouveauté
            leadplan.run_region("83", args, today + timedelta(days=2), {}, used)
            lookup.assert_called_once()
            self.assertIn("1 fiche Google", sent[2]["Subject"])


if __name__ == "__main__":
    unittest.main()
