"""LeadPlan — nouveaux leads beauté / bien-être dans le Var, envoyés par email chaque jour.

Trois sources :
  - Google Places : fiches apparues depuis le dernier passage (diff de place_id).
  - SIRENE via l'API Recherche d'entreprises (gratuite, sans clé) : établissements créés récemment
    en coiffure / soins de beauté / entretien corporel.
  - BODACC (gratuit, sans clé) : annonces de création et de reprise dont l'activité décrite est une
    prestation beauté / bien-être — capte les indépendants multi-activités et le bien-être que SIRENE rate.

Usage :
  python leadplan.py --baseline        indexe l'existant (Google + SIRENE), n'envoie rien
  python leadplan.py                   run quotidien : détecte, enrichit, envoie, sauvegarde
  python leadplan.py --dry-run         idem sans envoi ni sauvegarde, écrit preview.html
  python leadplan.py --test-email      envoie un email [TEST] : 3 fiches Google connues + 5 créations récentes
                                       (avec --dry-run : écrit preview.html sans envoyer)
  --bbox S,W,N,E                       limite la zone Google (tests)
  --log FICHIER                        ajoute la sortie à ce fichier (tâche planifiée via pythonw)
"""

import argparse
import csv
import io
import json
import math
import os
import re
import smtplib
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "seen.json"
SIRENE_STATE_FILE = ROOT / "data" / "sirene_seen.json"
BODACC_STATE_FILE = ROOT / "data" / "bodacc_seen.json"
USAGE_FILE = ROOT / "data" / "usage.json"
# "2026-10-29" = email du jour parti ; "2026-10-29 sirene" = parti sans SIRENE (API trop lente), la relance
# horaire suivante refait SIRENE seul et envoie un complément.
LAST_SENT_FILE = ROOT / "data" / "last_sent.txt"

# (requête texte, type Google Table A) — filtrage strict sur le type.
QUERIES = [
    ("coiffeur", "hair_salon"),
    ("barbier", "barber_shop"),
    ("institut de beauté", "beauty_salon"),
    ("esthéticienne", "beautician"),
    ("onglerie", "nail_salon"),
    ("massage", "massage"),
    ("spa", "spa"),
    ("maquilleuse", "makeup_artist"),
    ("spa massage", "massage_spa"),
    ("soins du visage", "skin_care_clinic"),
    ("bronzage", "tanning_studio"),
    ("bien-être", "wellness_center"),
    ("sauna", "sauna"),
    ("tatoueur", "body_art_service"),
    ("piercing", "body_art_service"),
    # Bien-être Planity : Google les classe en "health"/"service" → texte seul, sans filtre de type.
    ("réflexologue", None),
    ("massothérapeute", None),
    ("sophrologue", None),
    ("hypnothérapeute", None),
    ("naturopathe", None),
    ("coach de vie", None),
]

# Catégorie principale Google acceptée. Le reste (hôtel, camping, restaurant, pisciniste, clinique,
# magasin...) remonte via un type secondaire "spa"/"bien-être" → écarté. "health"/"service" = bien-être.
ALLOWED_TYPES = {
    "hair_salon", "hair_care", "barber_shop", "beauty_salon", "beautician", "nail_salon", "makeup_artist",
    "massage", "massage_spa", "spa", "sauna", "wellness_center", "skin_care_clinic", "tanning_studio",
    "body_art_service", "foot_care", "health", "service",
}
# Professions de santé que les recherches bien-être ramènent sous "health"/"service" : hors cible Planity.
HEALTH_NAME_RE = re.compile(r"osteo|sage-femme|sage femme|infirmi|\bkine\b|kinesitherap|psycholog|psychothera|"
                            r"psychiatr|medecin|docteur|\bdr\b|dentist|orthophon|podolog|pharmac|clinique|"
                            r"hopital|ehpad|veterin|acupunct|aide a domicile")

VAR_BBOX = (42.97, 5.65, 43.82, 6.95)  # sud, ouest, nord, est
CELL_DEG = 0.1          # grille initiale ~8 x 11 km
SATURATED = 60          # max de résultats par requête Text Search (3 pages de 20)
MAX_PAGES = 5           # garde-fou : 3 pages suffisent aujourd'hui
MAX_DEPTH = 5           # 0.1° / 2^5 ≈ 300 m
GOOGLE_MAX_AGE_DAYS = 60  # premier avis visible plus ancien → fiche qui n'est pas nouvelle, pas un lead
WORKERS = 3            # ~450 req/min, sous le quota par défaut de ~600/min

# SKU "Text Search Essentials (IDs Only)" = gratuit illimité.
# Ajouter un seul champ ici fait basculer en SKU payant. Ne pas toucher.
SEARCH_FIELDS = "places.id,nextPageToken"
# Place Details en deux temps : Pro (5 000 gratuits/mois) pour filtrer, puis seulement pour les fiches
# retenues téléphone, site et avis (les avis font passer en SKU « Enterprise + Atmosphere », 1 000
# gratuits/mois, compté sous "enterprise"). Budget mensuel compté dans data/usage.json, avec marge.
PRO_FIELDS = ("id,displayName,formattedAddress,addressComponents,primaryType,primaryTypeDisplayName,"
              "businessStatus,googleMapsUri")
ENTERPRISE_FIELDS = "nationalPhoneNumber,websiteUri,userRatingCount,reviews"
MONTHLY_BUDGET = {"pro": 4500, "enterprise": 900}

# SIRENE. L'API n'accepte que les codes NAF 2008 en filtre ; à la bascule NAF 2025 (janvier 2027)
# elle renverra une erreur, visible dans l'email → remplacer par les codes NAF25 ci-dessous.
SIRENE_URL = "https://recherche-entreprises.api.gouv.fr/search"
NAF_CODES = {"96.02A": "Coiffure", "96.02B": "Soins de beauté", "96.04Z": "Entretien corporel"}
NAF25_CODES = {"96.21G": "Coiffure", "96.21H": "Coiffure", "96.22Y": "Soins de beauté", "96.23Y": "Entretien corporel"}
SIRENE_MAX_AGE_DAYS = 120  # SIRET jamais vu mais créé avant → pas un lead (changement d'activité, etc.)
# Source principale : API Sirene de l'INSEE (clé gratuite INSEE_API_KEY, 30 req/min) → une seule requête
# filtrée sur le Var, les codes NAF et la date de création. Secours sans clé : API Recherche d'entreprises.
INSEE_URL = "https://api.insee.fr/api-sirene/3.11/siret"
# L'API Recherche d'entreprises n'a pas de filtre par date → ~260 pages par passage. Elle répond en ~1,5 s,
# mais par moments en 20-35 s par page ou en 429 (surcharge de leur côté, sans en-tête Retry-After) :
# on échoue vite au lieu d'attendre, et l'email part sans SIRENE (rattrapé par la relance horaire).
SIRENE_MAX_SECONDS = 8 * 60   # durée maximale du balayage SIRENE
SIRENE_PAGE_TIMEOUT = 15      # secondes par page, un seul nouvel essai

# BODACC : pas de code NAF, seulement l'activité en texte libre.
BODACC_URL = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
BODACC_DAYS = 14  # fenêtre de parution relue chaque jour (PC éteint quelques jours, diff sur l'id d'annonce)
# Recherche plein texte côté API, volontairement large ; le tri fin est fait par SERVICE_RE / RETAIL_RE.
BODACC_WORDS = ["coiffure", "coiffeur", "coiffeuse", "barbier", "esthétique", "esthéticienne", "beauté", "onglerie",
                "ongles", "ongulaire", "manucure", "cils", "épilation", "maquillage", "massage", "drainage",
                "réflexologie", "sophrologie", "naturopathie", "hypnose", "spa", "sauna", "tatouage", "piercing",
                "bronzage"]
# Texte sans accents. Un morceau d'activité compte si une prestation y est citée avant toute vente ou travaux :
# « coiffure en salon, achat vente d'accessoires » → oui ; « vente en ligne de produits de beauté, maquillage » → non.
SERVICE_RE = re.compile(
    r"coiff|barbi|barber|esthetici|(soins?|prestations?|institut|salon|centre)\s+(de\s+)?(technico-)?esthet|"
    r"esthetique en institut|institut de beaute|(soins?|prestations?) de beaute|beaute des (ongles|mains|pieds)|"
    r"onglerie|ongulaire|manucur|pedicur|\bongles\b|\bcils\b|sourcils|epilation|maquill|massage|drainage lymph|"
    r"reflexolog|sophrolog|naturopath|hypno|\bspa\b|sauna|tatou|piercing|percage|dermopigment|bronzage|"
    r"coach(ing)? de vie")
RETAIL_RE = re.compile(r"vente|commerc|achat|negoce|distribution|intermediaire|en ligne|a distance|internet|"
                       r"materiel|accessoire|produit|article|fabrication|import|installation|maintenance|"
                       r"depannage|reparation|piscine|travaux")


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Variable d'environnement manquante : {name}")
    return value


def http_json(req, timeout=30, attempts=8):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                print(f"HTTP {e.code} {req.full_url}\n{e.read().decode(errors='replace')[:800]}", file=sys.stderr)
                raise
        except OSError:  # réseau : URLError, TimeoutError en lecture, connexion coupée
            if attempt == attempts - 1:
                raise
        time.sleep(min(2 ** attempt, 60))  # 429 = quota/minute dépassé


def api(url, fields, body=None):
    return http_json(urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"X-Goog-Api-Key": env("API_KEY_GOOGLE_CLOUD"), "X-Goog-FieldMask": fields,
                 "Content-Type": "application/json"},
    ))


# --- Google : balayage -----------------------------------------------------

def grid(bbox, step=CELL_DEG):
    s, w, n, e = bbox
    rows, cols = math.ceil(round((n - s) / step, 6)), math.ceil(round((e - w) / step, 6))
    return [(round(s + i * step, 6), round(w + j * step, 6),
             round(min(s + (i + 1) * step, n), 6), round(min(w + (j + 1) * step, e), 6))
            for i in range(rows) for j in range(cols)]


def split(cell):
    s, w, n, e = cell
    mlat, mlng = (s + n) / 2, (w + e) / 2
    return [(s, w, mlat, mlng), (s, mlng, mlat, e), (mlat, w, n, mlng), (mlat, mlng, n, e)]


def search_cell(query, place_type, cell):
    s, w, n, e = cell
    body = {
        "textQuery": query, "languageCode": "fr", "regionCode": "FR", "pageSize": 20,
        "locationRestriction": {"rectangle": {"low": {"latitude": s, "longitude": w},
                                              "high": {"latitude": n, "longitude": e}}},
    }
    if place_type:
        body |= {"includedType": place_type, "strictTypeFiltering": True}
    ids, requests = set(), 0
    # Les requêtes texte seul peuvent renvoyer un nextPageToken indéfiniment sans nouvelle fiche (boucle
    # sans fin constatée le 28/10/2026) → arrêt dès qu'une page n'apporte rien, et MAX_PAGES au plus.
    for _ in range(MAX_PAGES):
        resp = api("https://places.googleapis.com/v1/places:searchText", SEARCH_FIELDS, body)
        requests += 1
        new = {p["id"] for p in resp.get("places", [])} - ids
        ids |= new
        if not new or not resp.get("nextPageToken"):
            break
        body["pageToken"] = resp["nextPageToken"]
    return ids, requests


def sweep_query(query, place_type, bbox):
    ids, requests, stack = set(), 0, [(cell, 0) for cell in grid(bbox)]
    while stack:
        cell, depth = stack.pop()
        found, n = search_cell(query, place_type, cell)
        ids |= found
        requests += n
        if len(found) >= SATURATED:
            if depth < MAX_DEPTH:
                stack += [(sub, depth + 1) for sub in split(cell)]
            else:
                print(f"  ! cellule saturée à profondeur max : {query} {cell}")
    print(f"  {query:<20} {place_type or '(texte seul)':<18} {len(ids):>5} fiches  {requests:>4} requêtes")
    return ids


def sweep(bbox):
    with ThreadPoolExecutor(WORKERS) as pool:
        results = pool.map(lambda q: sweep_query(*q, bbox), QUERIES)
        return set().union(*results)


# --- Google : détails & filtres --------------------------------------------

def details(place_id, fields):
    try:
        return api(f"https://places.googleapis.com/v1/places/{place_id}?languageCode=fr&regionCode=FR", fields)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def component(place, kind):
    for c in place.get("addressComponents", []):
        if kind in c.get("types", []):
            return c.get("longText", "")
    return ""


def postal_code(place):
    if code := component(place, "postal_code"):
        return code
    match = re.search(r"\b\d{5}\b", place.get("formattedAddress", ""))
    return match.group() if match else ""


def classify(place):
    if place is None:
        return "gone"
    if not postal_code(place).startswith("83"):
        return "out_of_zone"
    if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
        return "closed"
    if place.get("primaryType", "service") not in ALLOWED_TYPES \
            or HEALTH_NAME_RE.search(plain(place.get("displayName", {}).get("text", ""))):
        return "off_target"
    return "sent"


def first_review(place):
    """Date du plus ancien avis visible (l'API en renvoie au plus 5) : borne basse de l'âge de la fiche."""
    return min((r.get("publishTime", "")[:10] for r in place.get("reviews", [])), default="")


def too_old(place, today):
    oldest = first_review(place)
    return bool(oldest) and oldest < (today - timedelta(days=GOOGLE_MAX_AGE_DAYS)).isoformat()


def lookup(place_id, used, today):
    """Pro pour filtrer ; téléphone, site et avis seulement si la fiche est retenue, puis filtre d'âge."""
    used["pro"] += 1
    place = details(place_id, PRO_FIELDS)
    status = classify(place)
    if status == "sent":
        used["enterprise"] += 1
        place |= details(place_id, ENTERPRISE_FIELDS) or {}
        if too_old(place, today):
            status = "old"
    return status, place


# --- SIRENE ----------------------------------------------------------------

def sirene_fetch(today):
    """Établissements SIRENE ciblés, indexés par SIRET : INSEE si la clé existe, sinon balayage de secours."""
    return sirene_insee(today) if os.environ.get("INSEE_API_KEY") else sirene_sweep()


def sirene_insee(today):
    """Créations des SIRENE_MAX_AGE_DAYS derniers jours (et ouvertures prévues) dans le Var, via l'API INSEE."""
    # Codes NAF 2008 et NAF 2025 : à la bascule de janvier 2027, la même requête continue de fonctionner.
    naf = " OR ".join(f"activitePrincipaleEtablissement:{code}" for code in [*NAF_CODES, *NAF25_CODES])
    q = (f"codePostalEtablissement:83* AND dateCreationEtablissement:"
         f"[{today - timedelta(days=SIRENE_MAX_AGE_DAYS)} TO {today + timedelta(days=365)}] "
         f"AND periode(({naf}) AND etatAdministratifEtablissement:A)")
    headers = {"X-INSEE-Api-Key-Integration": os.environ["INSEE_API_KEY"], "Accept": "application/json"}
    found, cursor = {}, "*"
    for _ in range(10):  # ~200 résultats aujourd'hui : une page de 1 000 suffit, le curseur est une sécurité
        query = urllib.parse.urlencode({"q": q, "nombre": 1000, "curseur": cursor})
        data = http_json(urllib.request.Request(f"{INSEE_URL}?{query}", headers=headers), attempts=3)
        for raw in data.get("etablissements", []):
            if etab := insee_etab(raw):
                found[etab["siret"]] = etab
        if data["header"].get("curseurSuivant", cursor) == cursor:
            break
        cursor = data["header"]["curseurSuivant"]
    print(f"  SIRENE (INSEE) {len(found)} établissements créés depuis {SIRENE_MAX_AGE_DAYS} jours")
    return found


def insee_etab(raw):
    """Établissement INSEE → même forme que l'API Recherche d'entreprises ; None si la période en cours
    (la première de la liste) n'est plus active ou plus dans une activité ciblée."""
    period = (raw.get("periodesEtablissement") or [{}])[0]
    activity = period.get("activitePrincipaleEtablissement")
    if period.get("etatAdministratifEtablissement") != "A" or (activity not in NAF_CODES and activity not in NAF25_CODES):
        return None
    unit, addr = raw.get("uniteLegale") or {}, raw.get("adresseEtablissement") or {}
    person = " ".join(filter(None, [unit.get("prenomUsuelUniteLegale") or unit.get("prenom1UniteLegale"),
                                    unit.get("nomUsageUniteLegale") or unit.get("nomUniteLegale")]))
    commune = addr.get("libelleCommuneEtablissement") or ""
    street = " ".join(filter(None, [addr.get(k) for k in ("numeroVoieEtablissement", "indiceRepetitionEtablissement",
                                                           "typeVoieEtablissement", "libelleVoieEtablissement")]))
    return {
        "siret": raw["siret"],
        "nom_commercial": period.get("enseigne1Etablissement") or period.get("denominationUsuelleEtablissement"),
        "liste_enseignes": None,
        "nom_complet": unit.get("denominationUniteLegale") or person,
        "activite_principale": period.get("activitePrincipaleEtablissement"),
        "activite_principale_naf25": raw.get("activitePrincipaleNAF25Etablissement"),
        "etat_administratif": "A",
        "code_postal": addr.get("codePostalEtablissement") or "",
        "libelle_commune": commune,
        "adresse": " ".join(filter(None, [street, addr.get("codePostalEtablissement"), commune])),
        "date_creation": raw.get("dateCreationEtablissement") or "",
    }


def sirene_sweep():
    """Secours sans clé : établissements actifs du Var dont l'activité est ciblée, indexés par SIRET.
    Lève TimeoutError si l'API est trop lente (SIRENE_MAX_SECONDS) : jamais de balayage partiel utilisé."""
    found, started, done = {}, time.time(), 0
    for code in NAF_CODES:
        page, pages = 1, 1
        while page <= pages:
            if time.time() - started > SIRENE_MAX_SECONDS:
                raise TimeoutError(f"API trop lente : {done} pages lues en {SIRENE_MAX_SECONDS // 60} min "
                                   f"(~260 nécessaires), abandon")
            query = urllib.parse.urlencode({
                "activite_principale": code, "departement": "83", "etat_administratif": "A",
                "per_page": 25, "page": page, "minimal": "true",
                "include": "matching_etablissements", "limite_matching_etablissements": 100,
            })
            data = http_json(urllib.request.Request(f"{SIRENE_URL}?{query}", headers={"User-Agent": "LeadPlan"}),
                             timeout=SIRENE_PAGE_TIMEOUT, attempts=2)
            pages = data.get("total_pages", 0)
            done += 1
            if done % 50 == 0:
                print(f"  SIRENE {done} pages en {time.time() - started:.0f}s")
            for company in data.get("results", []):
                for etab in company.get("matching_etablissements") or []:
                    # Une société de coiffure peut avoir une holding ou un local commercial dans le Var.
                    targeted = (etab.get("activite_principale") in NAF_CODES
                                or etab.get("activite_principale_naf25") in NAF25_CODES)
                    if targeted and etab.get("etat_administratif") == "A" \
                            and str(etab.get("code_postal") or "").startswith("83"):
                        found[etab["siret"]] = {**etab, "nom_complet": company.get("nom_complet") or ""}
            page += 1
            time.sleep(0.5)  # limite officielle 7 req/s, mais l'API renvoie des 429 si on la sollicite trop souvent
    print(f"  SIRENE {len(found)} établissements ({done} pages en {time.time() - started:.0f}s)")
    return found


def sirene_diff(found, seen, today):
    """SIRET jamais vus → leads s'ils ont été créés récemment. Premier passage = baseline silencieuse."""
    first = not seen
    cutoff = (today - timedelta(days=SIRENE_MAX_AGE_DAYS)).isoformat()
    leads = []
    for siret, etab in found.items():
        if siret in seen:
            continue
        status = "baseline" if first else "sent" if (etab.get("date_creation") or "") >= cutoff else "old"
        seen[siret] = {"first_seen": today.isoformat(), "status": status}
        if status == "sent":
            leads.append(etab)
    return leads


def sirene_leads(found, sirene_seen, bodacc_seen, today):
    """Créations SIRENE du jour, hors sociétés déjà envoyées via BODACC (qui a parfois quelques jours d'avance)."""
    creations = sirene_diff(found, sirene_seen, today)
    bodacc_sent = {v.get("siren") for v in bodacc_seen.values() if v["status"] == "sent"}
    for etab in [e for e in creations if e["siret"][:9] in bodacc_sent]:
        sirene_seen[etab["siret"]]["status"] = "dup"
        creations.remove(etab)
    return creations


def sirene_complement(sirene_seen, bodacc_seen, today):
    """Relance horaire après un email parti sans SIRENE : SIRENE seul, complément envoyé s'il y a des créations.
    Si l'API est encore trop lente, on abandonne en ~8 min et l'heure suivante réessaie."""
    try:
        found = sirene_fetch(today)
    except Exception as e:
        print(f"Complément SIRENE reporté à l'heure suivante : {type(e).__name__}: {e}")
        return
    creations = sirene_leads(found, sirene_seen, bodacc_seen, today)
    if creations:
        msg, _ = build_email([], creations, [], today, complement=True)
        send(msg)
        print(msg["Subject"])
    else:
        print("Complément SIRENE : aucune nouvelle création")
    save_json(SIRENE_STATE_FILE, sirene_seen)
    LAST_SENT_FILE.write_text(today.isoformat())


# --- BODACC ----------------------------------------------------------------

def bodacc_fetch(today):
    """Annonces de création / reprise du Var parues sur la fenêtre, indexées par id d'annonce."""
    since = (today - timedelta(days=BODACC_DAYS)).isoformat()
    where = (f'numerodepartement="83" and dateparution>="{since}" '
             f'and familleavis in ("creation","immatriculation","vente") and ('
             + " or ".join(f'search(listeetablissements,"{w}")' for w in BODACC_WORDS) + ")")
    records, offset = [], 0
    while True:
        query = urllib.parse.urlencode({"where": where, "limit": 100, "offset": offset})
        page = http_json(urllib.request.Request(f"{BODACC_URL}?{query}", headers={"User-Agent": "LeadPlan"}))
        records += page.get("results", [])
        offset += 100
        if offset >= page.get("total_count", 0):
            print(f"  BODACC {len(records)} annonces")
            return {r["id"]: r for r in records}


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def bodacc_relevant(activity):
    """Au moins une activité (morceaux séparés par « - », « ; » ou « . ») commence par une prestation ciblée."""
    for chunk in re.split(r"\s[-–]\s|;|\.(?:\s|$)", plain(activity)):
        service, retail = SERVICE_RE.search(chunk), RETAIL_RE.search(chunk)
        if service and (not retail or service.start() < retail.start()):
            return True
    return False


def first(value):  # BODACC : un objet seul ou une liste d'objets
    return (value[0] if isinstance(value, list) else value) or {}


def bodacc_row(record):
    etab = first(json.loads(record.get("listeetablissements") or "{}").get("etablissement"))
    person = first(json.loads(record.get("listepersonnes") or "{}").get("personne"))
    acte = json.loads(record.get("acte") or "{}")
    siren = re.sub(r"\D", "", person.get("numeroImmatriculation", {}).get("numeroIdentification", ""))
    if not siren:
        siren = (re.findall(r"\d{9}", str(record.get("registre") or "")) or [""])[0]
    prenom = person.get("prenom") or ""
    prenom = prenom[0] if isinstance(prenom, list) else prenom.split(",")[0]  # texte "A, B" ou liste ["A", "B"]
    name = (etab.get("enseigne") or etab.get("nomCommercial") or person.get("denomination")
            or " ".join(filter(None, [prenom, person.get("nom")])))
    commune = record.get("ville") or ""
    return {
        "nom": name,
        "type": "Reprise" if record.get("familleavis") == "vente" else "Création",
        "activite": etab.get("activite") or "",
        "commune": commune,
        "code_postal": record.get("cp") or "",
        "debut_activite": acte.get("dateCommencementActivite") or "",
        "publie_le": record.get("dateparution") or "",
        "siren": siren,
        "annonce": record.get("url_complete") or "",
        "recherche_google": maps_search(name, commune),
    }


def bodacc_diff(found, seen, today, sirene_sirens):
    """Annonces jamais vues et ciblées. Une création déjà connue de SIRENE ou une société déjà envoyée = doublon."""
    already = {v.get("siren") for v in seen.values() if v["status"] == "sent"}
    leads = []
    for ann_id, record in found.items():
        if ann_id in seen:
            continue
        row = bodacc_row(record)
        if not bodacc_relevant(row["activite"]):
            status = "off_target"
        elif row["siren"] in already or (row["type"] == "Création" and row["siren"] in sirene_sirens):
            status = "dup"
        else:
            status = "sent"
            already.add(row["siren"])
            leads.append(row)
        seen[ann_id] = {"first_seen": today.isoformat(), "status": status, "siren": row["siren"]}
    return leads


# --- Email -----------------------------------------------------------------

def maps_search(name, commune):
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{name} {commune}")


def fr_date(iso):  # date future = ouverture prévue
    return "/".join(reversed(iso.split("-"))) if iso else ""

def google_row(place):
    return {
        "nom": place.get("displayName", {}).get("text", ""),
        "categorie": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "commune": component(place, "locality"),
        "code_postal": postal_code(place),
        "telephone": place.get("nationalPhoneNumber", ""),
        "site": place.get("websiteUri", ""),
        "avis": place.get("userRatingCount", 0),
        "premier_avis": first_review(place),
        "google_maps": place.get("googleMapsUri", ""),
    }


def sirene_row(etab):
    name = etab.get("nom_commercial") or ", ".join(etab.get("liste_enseignes") or []) or etab["nom_complet"]
    commune = (etab.get("libelle_commune") or "").title()
    code = etab.get("activite_principale")
    return {
        "nom": name,
        "societe": etab["nom_complet"],
        "activite": NAF_CODES.get(code) or NAF25_CODES.get(etab.get("activite_principale_naf25"), code or ""),
        "commune": commune,
        "adresse": etab.get("adresse") or "",
        "cree_le": etab.get("date_creation") or "",
        "siret": etab["siret"],
        "fiche_officielle": f"https://annuaire-entreprises.data.gouv.fr/etablissement/{etab['siret']}",
        "recherche_google": maps_search(name, commune),
    }


def link(url, text):
    return f"<a href='{escape(url)}'>{escape(text)}</a>" if url else ""


GOOGLE_COLUMNS = [
    ("Nom", lambda r: link(r["google_maps"], r["nom"])),
    ("Catégorie", lambda r: escape(r["categorie"])),
    ("Commune", lambda r: escape(f"{r['commune']} ({r['code_postal']})")),
    ("Téléphone", lambda r: link("tel:" + r["telephone"].replace(" ", ""), r["telephone"]) if r["telephone"] else ""),
    ("Site", lambda r: link(r["site"], "site")),
    ("Avis", lambda r: str(r["avis"])),
    ("1er avis", lambda r: escape(fr_date(r["premier_avis"]) or "aucun")),
]
SIRENE_COLUMNS = [
    ("Nom", lambda r: link(r["fiche_officielle"], r["nom"])),
    ("Activité", lambda r: escape(r["activite"])),
    ("Adresse", lambda r: escape(r["adresse"])),
    ("Créé le", lambda r: escape(fr_date(r["cree_le"]))),
    ("Google", lambda r: link(r["recherche_google"], "chercher")),
]
BODACC_COLUMNS = [
    ("Nom", lambda r: link(r["annonce"], r["nom"])),
    ("Type", lambda r: escape(r["type"])),
    ("Activité", lambda r: escape(r["activite"][:120] + ("…" if len(r["activite"]) > 120 else ""))),
    ("Commune", lambda r: escape(f"{r['commune']} ({r['code_postal']})")),
    ("Début d'activité", lambda r: escape(fr_date(r["debut_activite"]))),
    ("Google", lambda r: link(r["recherche_google"], "chercher")),
]


def html_table(rows, columns):
    if not rows:
        return "<p style='color:#666'>Aucune.</p>"
    cell = "padding:6px;border-bottom:1px solid #eee"
    head = "".join(f"<th style='text-align:left;padding:6px;border-bottom:2px solid #ddd'>{h}</th>" for h, _ in columns)
    body = "".join("<tr>" + "".join(f"<td style='{cell}'>{fn(r)}</td>" for _, fn in columns) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse;font-size:14px'><tr>{head}</tr>{body}</table>"


def csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, list(rows[0]), delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # Excel FR


def plural(n, word):
    return f"{n} {word}{'s' if n > 1 else ''}"


def build_email(places, etabs, annonces, today, test=False, errors=None, complement=False):
    rows = sorted((google_row(p) for p in places), key=lambda r: (r["commune"], r["nom"]))
    created = sorted((sirene_row(e) for e in etabs), key=lambda r: r["cree_le"], reverse=True)
    annonces = sorted(annonces, key=lambda r: r["publie_le"], reverse=True)

    msg = EmailMessage()
    if complement:  # créations SIRENE rattrapées après un email du matin parti sans elles
        msg["Subject"] = f"LeadPlan {today:%d/%m} — complément : {plural(len(created), 'création')} d'entreprise"
    else:
        msg["Subject"] = (f"{'[TEST] ' if test else ''}LeadPlan {today:%d/%m} — {plural(len(created), 'création')}, "
                          f"{len(annonces)} BODACC, {plural(len(rows), 'fiche')} Google")
    msg["From"] = env("GMAIL_ADDRESS")
    msg["To"] = os.environ.get("MAIL_TO") or env("GMAIL_ADDRESS")
    warning = "".join(f"<p style='color:#b00'>⚠️ {source} indisponible aujourd'hui : {escape(error)}</p>"
                      for source, error in (errors or {}).items())
    others = "" if complement else (
        f"<h2>Annonces légales BODACC ({len(annonces)})</h2>{html_table(annonces, BODACC_COLUMNS)}"
        f"<h2>Nouvelles fiches Google ({len(rows)})</h2>{html_table(rows, GOOGLE_COLUMNS)}")
    html = (f"<div style='font-family:Arial,sans-serif'>{warning}"
            f"<h2>Créations d'entreprises ({len(created)})</h2>{html_table(created, SIRENE_COLUMNS)}{others}"
            f"<p style='color:#888;font-size:12px'>Créations : établissements immatriculés depuis moins de "
            f"{SIRENE_MAX_AGE_DAYS} jours (source SIRENE / INSEE), apparus depuis hier ; date future = ouverture "
            f"prévue. BODACC : créations et reprises de fonds publiées au journal officiel dont l'activité décrite "
            f"est une prestation beauté / bien-être, hors doublons avec SIRENE. Pas de téléphone dans ces deux "
            f"sources : « chercher » ouvre Google Maps. Fiches Google : apparues depuis le dernier passage, "
            f"hors professions de santé et hors fiches dont le premier avis a plus de {GOOGLE_MAX_AGE_DAYS} jours ; "
            f"« 1er avis » = date du plus ancien avis visible (« aucun » = fiche sans avis, souvent toute "
            f"nouvelle).</p></div>")
    msg.set_content(f"{len(created)} créations, {len(annonces)} annonces BODACC, {len(rows)} fiches Google. "
                    f"Voir la version HTML ou les CSV joints.")
    msg.add_alternative(html, subtype="html")

    if created:
        msg.add_attachment(csv_bytes(created), maintype="text", subtype="csv", filename=f"creations-{today}.csv")
    if annonces:
        msg.add_attachment(csv_bytes(annonces), maintype="text", subtype="csv", filename=f"bodacc-{today}.csv")
    if rows:
        msg.add_attachment(csv_bytes(rows), maintype="text", subtype="csv", filename=f"google-{today}.csv")
    return msg, html


def send(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(env("GMAIL_ADDRESS"), env("APP_PASSWORD_GOOGLE").replace(" ", ""))
        smtp.send_message(msg)


# --- État ------------------------------------------------------------------

def load_json(path):
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def save_json(path, state):
    path.parent.mkdir(exist_ok=True)
    lines = [f"{json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(state.items())]
    path.write_text("{\n" + ",\n".join(lines) + "\n}\n", "utf-8")


# --- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--bbox", type=lambda s: tuple(map(float, s.split(","))), default=VAR_BBOX)
    parser.add_argument("--log", type=Path, help="ajoute la sortie à ce fichier (tâche planifiée via pythonw)")
    args = parser.parse_args()
    if args.log:
        sys.stdout = sys.stderr = open(ROOT / args.log, "a", encoding="utf-8", buffering=1)
        print(f"\n=== {time.strftime('%Y-%m-%d %H:%M')} ===")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    today = date.today()
    google_seen, sirene_seen = load_json(STATE_FILE), load_json(SIRENE_STATE_FILE)
    bodacc_seen, usage = load_json(BODACC_STATE_FILE), load_json(USAGE_FILE)
    used = usage.setdefault(today.strftime("%Y-%m"), {"pro": 0, "enterprise": 0})

    if args.test_email:
        places = []
        for pid in [pid for pid, v in google_seen.items() if v["status"] == "baseline"][:3]:
            places.append((details(pid, PRO_FIELDS) or {}) | (details(pid, ENTERPRISE_FIELDS) or {}))
            used["pro"] += 1
            used["enterprise"] += 1
        save_json(USAGE_FILE, usage)
        etabs = sorted(sirene_fetch(today).values(), key=lambda e: e.get("date_creation") or "", reverse=True)[:5]
        annonces = [row for row in map(bodacc_row, bodacc_fetch(today).values()) if bodacc_relevant(row["activite"])]
        msg, html = build_email(places, etabs, annonces, today, test=True)
        if args.dry_run:
            (ROOT / "preview.html").write_text(html, "utf-8")
            print("Aperçu : preview.html (rien envoyé)")
            return
        send(msg)
        print(f"Email de test envoyé à {msg['To']}")
        return

    if not google_seen and not args.baseline:
        sys.exit("Aucun état : lance d'abord `python leadplan.py --baseline`.")
    last_sent = LAST_SENT_FILE.read_text().split() if LAST_SENT_FILE.exists() else []
    if not (args.baseline or args.dry_run) and last_sent[:1] == [today.isoformat()]:
        if "sirene" in last_sent:
            sirene_complement(sirene_seen, bodacc_seen, today)
        else:
            print("Email du jour déjà envoyé, rien à faire.")
        return

    started = time.time()
    errors = {}  # SIRENE ou BODACC en panne ne doivent pas bloquer les autres sources

    def safe(source, fetch):
        try:
            return fetch()
        except Exception as e:
            errors[source] = f"{type(e).__name__}: {e}"
            print(f"{source} indisponible : {errors[source]}")
            return {}

    # SIRENE (~6 min) en parallèle du balayage Google (~6 min), dans un thread démon pour ne jamais
    # bloquer l'envoi : l'API limite parfois fortement (429), on n'attend pas au-delà de SIRENE_MAX_SECONDS.
    sirene_result = {}
    sirene_thread = threading.Thread(target=lambda: sirene_result.update(found=safe("SIRENE", lambda: sirene_fetch(today))),
                                     daemon=True)
    sirene_thread.start()
    found = sweep(args.bbox)
    print(f"{len(found)} fiches Google uniques en {time.time() - started:.0f}s")
    bodacc_found = safe("BODACC", lambda: bodacc_fetch(today))
    sirene_thread.join(timeout=max(0, SIRENE_MAX_SECONDS + 60 - (time.time() - started)))
    if sirene_thread.is_alive():
        errors["SIRENE"] = "API trop lente aujourd'hui (limite de requêtes) : les créations arriveront au prochain email"
        print(errors["SIRENE"])
    sirene_found = sirene_result.get("found", {})
    errors_now = dict(errors)  # copie : le thread SIRENE, s'il tourne encore, peut écrire dans l'original

    if args.baseline:
        for pid in found:
            google_seen.setdefault(pid, {"first_seen": today.isoformat(), "status": "baseline"})
        for siret in sirene_found:
            sirene_seen.setdefault(siret, {"first_seen": today.isoformat(), "status": "baseline"})
        for ann_id in bodacc_found:
            bodacc_seen.setdefault(ann_id, {"first_seen": today.isoformat(), "status": "baseline"})
        if not args.dry_run:
            save_json(STATE_FILE, google_seen)
            save_json(SIRENE_STATE_FILE, sirene_seen)
            save_json(BODACC_STATE_FILE, bodacc_seen)
        print(f"Baseline : {len(google_seen)} fiches Google, {len(sirene_seen)} établissements SIRENE, "
              f"{len(bodacc_seen)} annonces BODACC")
        return

    for pid in found - google_seen.keys():
        google_seen[pid] = {"first_seen": today.isoformat(), "status": "pending"}
    pending = [pid for pid, v in google_seen.items() if v["status"] == "pending"]
    print(f"{len(pending)} nouvelles fiches Google")

    leads = []
    for pid in pending:
        if any(used[k] >= MONTHLY_BUDGET[k] for k in MONTHLY_BUDGET):
            print(f"Budget Google du mois atteint ({used}) : le reste attend le mois prochain")
            break
        status, place = lookup(pid, used, today)
        google_seen[pid]["status"] = status
        if status == "sent":
            leads.append(place)
    save_json(USAGE_FILE, usage)  # appels réellement consommés, même en dry-run

    creations = sirene_leads(sirene_found, sirene_seen, bodacc_seen, today)
    annonces = bodacc_diff(bodacc_found, bodacc_seen, today, {siret[:9] for siret in sirene_seen})

    msg, html = build_email(leads, creations, annonces, today, errors=errors_now)
    print(f"{msg['Subject']} | usage du mois : {used}")
    if args.dry_run:
        (ROOT / "preview.html").write_text(html, "utf-8")
        print("Aperçu : preview.html (rien envoyé, état non sauvegardé)")
        return
    send(msg)
    LAST_SENT_FILE.write_text(today.isoformat() + (" sirene" if "SIRENE" in errors_now else ""))
    save_json(STATE_FILE, google_seen)
    save_json(SIRENE_STATE_FILE, sirene_seen)
    save_json(BODACC_STATE_FILE, bodacc_seen)


if __name__ == "__main__":
    main()
