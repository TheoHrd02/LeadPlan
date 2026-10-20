"""LeadPlan — nouvelles fiches Google (beauté / bien-être) dans le Var, envoyées par email chaque semaine.

Usage :
  python leadplan.py --baseline        indexe l'existant, n'envoie rien
  python leadplan.py                   run hebdo : détecte, enrichit, envoie, sauvegarde
  python leadplan.py --dry-run         idem sans envoi ni sauvegarde, écrit preview.html
  python leadplan.py --test-email      envoie un email [TEST] avec 5 fiches déjà connues
  --bbox S,W,N,E                       limite la zone (tests)
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "seen.json"

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
]

VAR_BBOX = (42.97, 5.65, 43.82, 6.95)  # sud, ouest, nord, est
CELL_DEG = 0.1          # grille initiale ~8 x 11 km
SATURATED = 60          # max de résultats par requête Text Search (3 pages de 20)
MAX_DEPTH = 5           # 0.1° / 2^5 ≈ 300 m
MAX_DETAILS = 200       # Place Details Enterprise : 1 000 gratuits / mois
NEW_MAX_REVIEWS = 5     # au-delà → section « À vérifier »
WORKERS = 3             # ~450 req/min, sous le quota par défaut de ~600/min

# SKU "Text Search Essentials (IDs Only)" = gratuit illimité.
# Ajouter un seul champ ici fait basculer en SKU payant. Ne pas toucher.
SEARCH_FIELDS = "places.id,nextPageToken"
DETAIL_FIELDS = ("id,displayName,formattedAddress,addressComponents,primaryTypeDisplayName,"
                 "businessStatus,googleMapsUri,nationalPhoneNumber,websiteUri,userRatingCount")


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


def api(url, fields, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"X-Goog-Api-Key": env("API_KEY_GOOGLE_CLOUD"), "X-Goog-FieldMask": fields,
                 "Content-Type": "application/json"},
    )
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 7:
                print(f"HTTP {e.code} {url}\n{e.read().decode(errors='replace')[:800]}", file=sys.stderr)
                raise
        except urllib.error.URLError:
            if attempt == 7:
                raise
        time.sleep(min(2 ** attempt, 60))  # 429 = quota/minute (~600) dépassé


# --- Balayage -------------------------------------------------------------

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
        "textQuery": query, "includedType": place_type, "strictTypeFiltering": True,
        "languageCode": "fr", "regionCode": "FR", "pageSize": 20,
        "locationRestriction": {"rectangle": {"low": {"latitude": s, "longitude": w},
                                              "high": {"latitude": n, "longitude": e}}},
    }
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
    print(f"  {query:<20} {place_type:<18} {len(ids):>5} fiches  {requests:>4} requêtes")
    return ids


def sweep(bbox):
    with ThreadPoolExecutor(WORKERS) as pool:
        results = pool.map(lambda q: sweep_query(*q, bbox), QUERIES)
        return set().union(*results)


# --- Détails & filtres ----------------------------------------------------

def details(place_id):
    try:
        return api(f"https://places.googleapis.com/v1/places/{place_id}?languageCode=fr&regionCode=FR",
                   DETAIL_FIELDS)
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
    return "sent"


# --- Email ----------------------------------------------------------------

def row(place):
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


def html_table(rows):
    if not rows:
        return "<p style='color:#666'>Aucune.</p>"
    head = "".join(f"<th style='text-align:left;padding:6px;border-bottom:2px solid #ddd'>{h}</th>"
                   for h in ("Nom", "Catégorie", "Commune", "Téléphone", "Site", "Avis"))
    lines = []
    for r in rows:
        name = f"<a href='{escape(r['google_maps'])}'>{escape(r['nom'])}</a>"
        tel = f"<a href='tel:{escape(r['telephone'].replace(' ', ''))}'>{escape(r['telephone'])}</a>" if r["telephone"] else ""
        site = f"<a href='{escape(r['site'])}'>site</a>" if r["site"] else ""
        cells = (name, escape(r["categorie"]), escape(f"{r['commune']} ({r['code_postal']})"), tel, site, str(r["avis"]))
        lines.append("<tr>" + "".join(f"<td style='padding:6px;border-bottom:1px solid #eee'>{c}</td>"
                                      for c in cells) + "</tr>")
    return f"<table style='border-collapse:collapse;font-size:14px'><tr>{head}</tr>{''.join(lines)}</table>"


def build_email(places, today, test=False):
    rows = sorted((row(p) for p in places), key=lambda r: (r["commune"], r["nom"]))
    fresh = [r for r in rows if r["avis"] <= NEW_MAX_REVIEWS]
    check = [r for r in rows if r["avis"] > NEW_MAX_REVIEWS]
    year, week, _ = today.isocalendar()

    msg = EmailMessage()
    plural = "x leads" if len(fresh) > 1 else " lead"
    msg["Subject"] = f"{'[TEST] ' if test else ''}LeadPlan — {len(fresh)} nouveau{plural} Var (sem. {week})"
    msg["From"] = env("GMAIL_ADDRESS")
    msg["To"] = os.environ.get("MAIL_TO") or env("GMAIL_ADDRESS")
    html = (f"<div style='font-family:Arial,sans-serif'>"
            f"<h2>Nouvelles fiches probables ({len(fresh)})</h2>{html_table(fresh)}"
            f"<h2>À vérifier — déjà des avis ({len(check)})</h2>{html_table(check)}"
            f"<p style='color:#888;font-size:12px'>Fiches apparues sur Google depuis le dernier passage. "
            f"« À vérifier » = plus de {NEW_MAX_REVIEWS} avis, probablement une fiche ancienne.</p></div>")
    msg.set_content(f"{len(fresh)} nouveaux leads, {len(check)} à vérifier. Voir la version HTML ou le CSV joint.")
    msg.add_alternative(html, subtype="html")

    if rows:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, ["section", *rows[0]], delimiter=";")
        writer.writeheader()
        writer.writerows({"section": "nouveau", **r} for r in fresh)
        writer.writerows({"section": "a_verifier", **r} for r in check)
        msg.add_attachment(buf.getvalue().encode("utf-8-sig"), maintype="text", subtype="csv",
                           filename=f"leads-{year}-W{week:02d}.csv")
    return msg, html


def send(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(env("GMAIL_ADDRESS"), env("APP_PASSWORD_GOOGLE").replace(" ", ""))
        smtp.send_message(msg)


# --- État -----------------------------------------------------------------

def load_state():
    return json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    lines = [f"{json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(state.items())]
    STATE_FILE.write_text("{\n" + ",\n".join(lines) + "\n}\n", "utf-8")


# --- Main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--bbox", type=lambda s: tuple(map(float, s.split(","))), default=VAR_BBOX)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    today = date.today()
    state = load_state()

    if args.test_email:
        ids = [pid for pid, v in state.items() if v["status"] == "baseline"][:5]
        msg, _ = build_email([p for p in map(details, ids) if p], today, test=True)
        send(msg)
        print(f"Email de test envoyé à {msg['To']}")
        return

    if not state and not args.baseline:
        sys.exit("Aucun état : lance d'abord `python leadplan.py --baseline`.")

    started = time.time()
    found = sweep(args.bbox)
    print(f"{len(found)} fiches uniques en {time.time() - started:.0f}s")

    if args.baseline:
        for pid in found:
            state.setdefault(pid, {"first_seen": today.isoformat(), "status": "baseline"})
        if not args.dry_run:
            save_state(state)
        print(f"Baseline : {len(state)} fiches indexées")
        return

    for pid in found - state.keys():
        state[pid] = {"first_seen": today.isoformat(), "status": "pending"}
    pending = [pid for pid, v in state.items() if v["status"] == "pending"]
    print(f"{len(pending)} nouvelles fiches, détails pour {min(len(pending), MAX_DETAILS)}")

    leads = []
    for pid in pending[:MAX_DETAILS]:
        place = details(pid)
        state[pid]["status"] = classify(place)
        if state[pid]["status"] == "sent":
            leads.append(place)

    msg, html = build_email(leads, today)
    print(msg["Subject"])
    if args.dry_run:
        (ROOT / "preview.html").write_text(html, "utf-8")
        print("Aperçu : preview.html (rien envoyé, état non sauvegardé)")
        return
    send(msg)
    save_state(state)


if __name__ == "__main__":
    main()
