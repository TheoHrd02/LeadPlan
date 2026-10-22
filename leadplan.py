"""LeadPlan — nouveaux leads beauté / bien-être dans le Var, envoyés par email chaque jour.

Deux sources :
  - Google Places : fiches apparues depuis le dernier passage (diff de place_id).
  - SIRENE via l'API Recherche d'entreprises (gratuite, sans clé) : établissements créés récemment
    en coiffure / soins de beauté / entretien corporel.

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
import time
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
USAGE_FILE = ROOT / "data" / "usage.json"

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

VAR_BBOX = (42.97, 5.65, 43.82, 6.95)  # sud, ouest, nord, est
CELL_DEG = 0.1          # grille initiale ~8 x 11 km
SATURATED = 60          # max de résultats par requête Text Search (3 pages de 20)
MAX_DEPTH = 5           # 0.1° / 2^5 ≈ 300 m
NEW_MAX_REVIEWS = 5     # au-delà → section « À vérifier »
WORKERS = 3             # ~450 req/min, sous le quota par défaut de ~600/min

# SKU "Text Search Essentials (IDs Only)" = gratuit illimité.
# Ajouter un seul champ ici fait basculer en SKU payant. Ne pas toucher.
SEARCH_FIELDS = "places.id,nextPageToken"
# Place Details en deux temps : Pro (5 000 gratuits/mois) pour filtrer, Enterprise (1 000/mois)
# seulement pour les fiches retenues. Budget mensuel compté dans data/usage.json, avec marge.
PRO_FIELDS = ("id,displayName,formattedAddress,addressComponents,primaryType,primaryTypeDisplayName,"
              "businessStatus,googleMapsUri")
ENTERPRISE_FIELDS = "nationalPhoneNumber,websiteUri,userRatingCount"
MONTHLY_BUDGET = {"pro": 4500, "enterprise": 900}

# SIRENE. L'API n'accepte que les codes NAF 2008 en filtre ; à la bascule NAF 2025 (janvier 2027)
# elle renverra une erreur, visible dans l'email → remplacer par les codes NAF25 ci-dessous.
SIRENE_URL = "https://recherche-entreprises.api.gouv.fr/search"
NAF_CODES = {"96.02A": "Coiffure", "96.02B": "Soins de beauté", "96.04Z": "Entretien corporel"}
NAF25_CODES = {"96.21G": "Coiffure", "96.21H": "Coiffure", "96.22Y": "Soins de beauté", "96.23Y": "Entretien corporel"}
SIRENE_MAX_AGE_DAYS = 120  # SIRET jamais vu mais créé avant → pas un lead (changement d'activité, etc.)


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


def http_json(req):
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 7:
                print(f"HTTP {e.code} {req.full_url}\n{e.read().decode(errors='replace')[:800]}", file=sys.stderr)
                raise
        except urllib.error.URLError:
            if attempt == 7:
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
    while True:
        resp = api("https://places.googleapis.com/v1/places:searchText", SEARCH_FIELDS, body)
        requests += 1
        ids.update(p["id"] for p in resp.get("places", []))
        if not resp.get("nextPageToken"):
            return ids, requests
        body["pageToken"] = resp["nextPageToken"]


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
    if place.get("primaryType", "service") not in ALLOWED_TYPES:
        return "off_target"
    return "sent"


def lookup(place_id, used):
    """Pro pour filtrer ; Enterprise (téléphone, site, avis) seulement si la fiche est retenue."""
    used["pro"] += 1
    place = details(place_id, PRO_FIELDS)
    status = classify(place)
    if status == "sent":
        used["enterprise"] += 1
        place |= details(place_id, ENTERPRISE_FIELDS) or {}
    return status, place


# --- SIRENE ----------------------------------------------------------------

def sirene_sweep():
    """Établissements actifs du Var dont l'activité est ciblée, indexés par SIRET."""
    found = {}
    for code in NAF_CODES:
        page, pages = 1, 1
        while page <= pages:
            query = urllib.parse.urlencode({
                "activite_principale": code, "departement": "83", "etat_administratif": "A",
                "per_page": 25, "page": page, "minimal": "true",
                "include": "matching_etablissements", "limite_matching_etablissements": 100,
            })
            data = http_json(urllib.request.Request(f"{SIRENE_URL}?{query}", headers={"User-Agent": "LeadPlan"}))
            pages = data.get("total_pages", 0)
            for company in data.get("results", []):
                for etab in company.get("matching_etablissements") or []:
                    # Une société de coiffure peut avoir une holding ou un local commercial dans le Var.
                    targeted = (etab.get("activite_principale") in NAF_CODES
                                or etab.get("activite_principale_naf25") in NAF25_CODES)
                    if targeted and etab.get("etat_administratif") == "A" \
                            and str(etab.get("code_postal") or "").startswith("83"):
                        found[etab["siret"]] = {**etab, "nom_complet": company.get("nom_complet") or ""}
            page += 1
            time.sleep(0.2)  # limite de l'API : 7 requêtes/s
    print(f"  SIRENE {len(found)} établissements")
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


# --- Email -----------------------------------------------------------------

def google_row(place):
    return {
        "nom": place.get("displayName", {}).get("text", ""),
        "categorie": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "commune": component(place, "locality"),
        "code_postal": postal_code(place),
        "telephone": place.get("nationalPhoneNumber", ""),
        "site": place.get("websiteUri", ""),
        "avis": place.get("userRatingCount", 0),
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
        "recherche_google": "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{name} {commune}"),
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
]
SIRENE_COLUMNS = [
    ("Nom", lambda r: link(r["fiche_officielle"], r["nom"])),
    ("Activité", lambda r: escape(r["activite"])),
    ("Adresse", lambda r: escape(r["adresse"])),
    ("Créé le", lambda r: escape("/".join(reversed(r["cree_le"].split("-"))))),  # date future = ouverture prévue
    ("Google", lambda r: link(r["recherche_google"], "chercher")),
]


def html_table(rows, columns):
    if not rows:
        return "<p style='color:#666'>Aucune.</p>"
    cell = "padding:6px;border-bottom:1px solid #eee"
    head = "".join(f"<th style='text-align:left;padding:6px;border-bottom:2px solid #ddd'>{h}</th>" for h, _ in columns)
    body = "".join("<tr>" + "".join(f"<td style='{cell}'>{fn(r)}</td>" for _, fn in columns) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse;font-size:14px'><tr>{head}</tr>{body}</table>"


def csv_bytes(rows, section=None):
    buf = io.StringIO()
    fields = (["section"] if section else []) + list(rows[0])
    writer = csv.DictWriter(buf, fields, delimiter=";")
    writer.writeheader()
    for r in rows:
        writer.writerow({"section": section(r), **r} if section else r)
    return buf.getvalue().encode("utf-8-sig")  # Excel FR


def plural(n, word):
    return f"{n} {word}{'s' if n > 1 else ''}"


def build_email(places, etabs, today, test=False, sirene_error=None):
    rows = sorted((google_row(p) for p in places), key=lambda r: (r["commune"], r["nom"]))
    fresh = [r for r in rows if r["avis"] <= NEW_MAX_REVIEWS]
    check = [r for r in rows if r["avis"] > NEW_MAX_REVIEWS]
    created = sorted((sirene_row(e) for e in etabs), key=lambda r: r["cree_le"], reverse=True)

    msg = EmailMessage()
    msg["Subject"] = (f"{'[TEST] ' if test else ''}LeadPlan {today:%d/%m} — {plural(len(created), 'création')}, "
                      f"{plural(len(fresh), 'fiche')} Google")
    msg["From"] = env("GMAIL_ADDRESS")
    msg["To"] = os.environ.get("MAIL_TO") or env("GMAIL_ADDRESS")
    warning = (f"<p style='color:#b00'>⚠️ SIRENE indisponible aujourd'hui : {escape(sirene_error)}</p>"
               if sirene_error else "")
    html = (f"<div style='font-family:Arial,sans-serif'>{warning}"
            f"<h2>Créations d'entreprises ({len(created)})</h2>{html_table(created, SIRENE_COLUMNS)}"
            f"<h2>Nouvelles fiches Google probables ({len(fresh)})</h2>{html_table(fresh, GOOGLE_COLUMNS)}"
            f"<h2>Fiches Google à vérifier — déjà des avis ({len(check)})</h2>{html_table(check, GOOGLE_COLUMNS)}"
            f"<p style='color:#888;font-size:12px'>Créations : établissements immatriculés depuis moins de "
            f"{SIRENE_MAX_AGE_DAYS} jours (source SIRENE / INSEE), apparus depuis hier. Pas de téléphone dans "
            f"SIRENE : « chercher » ouvre Google Maps. Fiches Google : apparues depuis le dernier passage ; "
            f"« À vérifier » = plus de {NEW_MAX_REVIEWS} avis, probablement une fiche ancienne.</p></div>")
    msg.set_content(f"{len(created)} créations, {len(fresh)} fiches Google, {len(check)} à vérifier. "
                    f"Voir la version HTML ou les CSV joints.")
    msg.add_alternative(html, subtype="html")

    if created:
        msg.add_attachment(csv_bytes(created), maintype="text", subtype="csv", filename=f"creations-{today}.csv")
    if rows:
        section = lambda r: "nouveau" if r["avis"] <= NEW_MAX_REVIEWS else "a_verifier"
        msg.add_attachment(csv_bytes(fresh + check, section), maintype="text", subtype="csv",
                           filename=f"google-{today}.csv")
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
    google_seen, sirene_seen, usage = load_json(STATE_FILE), load_json(SIRENE_STATE_FILE), load_json(USAGE_FILE)
    used = usage.setdefault(today.strftime("%Y-%m"), {"pro": 0, "enterprise": 0})

    if args.test_email:
        places = []
        for pid in [pid for pid, v in google_seen.items() if v["status"] == "baseline"][:3]:
            places.append((details(pid, PRO_FIELDS) or {}) | (details(pid, ENTERPRISE_FIELDS) or {}))
            used["pro"] += 1
            used["enterprise"] += 1
        save_json(USAGE_FILE, usage)
        etabs = sorted(sirene_sweep().values(), key=lambda e: e.get("date_creation") or "", reverse=True)[:5]
        msg, html = build_email(places, etabs, today, test=True)
        if args.dry_run:
            (ROOT / "preview.html").write_text(html, "utf-8")
            print("Aperçu : preview.html (rien envoyé)")
            return
        send(msg)
        print(f"Email de test envoyé à {msg['To']}")
        return

    if not google_seen and not args.baseline:
        sys.exit("Aucun état : lance d'abord `python leadplan.py --baseline`.")

    started = time.time()
    with ThreadPoolExecutor(1) as pool:  # SIRENE (~6 min) en parallèle du balayage Google (~6 min)
        sirene_job = pool.submit(sirene_sweep)
        found = sweep(args.bbox)
        print(f"{len(found)} fiches Google uniques en {time.time() - started:.0f}s")
        try:
            sirene_found, sirene_error = sirene_job.result(), None
        except Exception as e:  # SIRENE en panne ne doit pas bloquer les leads Google
            sirene_found, sirene_error = {}, f"{type(e).__name__}: {e}"
            print(f"SIRENE indisponible : {sirene_error}")

    if args.baseline:
        for pid in found:
            google_seen.setdefault(pid, {"first_seen": today.isoformat(), "status": "baseline"})
        for siret in sirene_found:
            sirene_seen.setdefault(siret, {"first_seen": today.isoformat(), "status": "baseline"})
        if not args.dry_run:
            save_json(STATE_FILE, google_seen)
            save_json(SIRENE_STATE_FILE, sirene_seen)
        print(f"Baseline : {len(google_seen)} fiches Google, {len(sirene_seen)} établissements SIRENE")
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
        status, place = lookup(pid, used)
        google_seen[pid]["status"] = status
        if status == "sent":
            leads.append(place)
    save_json(USAGE_FILE, usage)  # appels réellement consommés, même en dry-run

    creations = sirene_diff(sirene_found, sirene_seen, today)
    msg, html = build_email(leads, creations, today, sirene_error=sirene_error)
    print(f"{msg['Subject']} | usage du mois : {used}")
    if args.dry_run:
        (ROOT / "preview.html").write_text(html, "utf-8")
        print("Aperçu : preview.html (rien envoyé, état non sauvegardé)")
        return
    send(msg)
    save_json(STATE_FILE, google_seen)
    save_json(SIRENE_STATE_FILE, sirene_seen)


if __name__ == "__main__":
    main()
