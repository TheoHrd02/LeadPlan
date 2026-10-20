# LeadPlan — Spécification

Email hebdomadaire listant les nouvelles fiches Google (coiffure, esthétique, massage, beauté) apparues dans le Var (83). Pas d'interface. Coût cible : 0 €.

---

## 1. Fonctionnel

**Entrée** : rien (tâche planifiée).
**Sortie** : 1 email par semaine, lundi ~8h (heure de Paris).

Contenu de l'email :
- Objet : `LeadPlan — 23 nouveaux leads Var (sem. 43)`
- Section **Nouvelles fiches probables** (≤ 5 avis Google) : tableau Nom · Catégorie · Commune · Téléphone · Site · Nb avis · Lien Google Maps.
- Section **À vérifier** (> 5 avis) : fiches détectées pour la première fois mais visiblement anciennes (changement de catégorie, nouvelle place ID, trou de couverture).
- Pièce jointe `leads-2026-W43.csv` (import CRM / tableur).
- Si 0 lead : email envoyé quand même (« 0 nouveau lead ») → preuve que le pipeline tourne.

**Secteurs couverts** (couples requête texte / type Google, filtrage strict) :

| Requête | `includedType` |
|---|---|
| coiffeur | `hair_salon` |
| barbier | `barber_shop` |
| institut de beauté | `beauty_salon` |
| esthéticienne | `beautician` |
| onglerie | `nail_salon` |
| massage | `massage` |
| spa | `spa` |
| maquilleuse | `makeup_artist` |
| spa massage | `massage_spa` |
| soins du visage | `skin_care_clinic` |
| bronzage | `tanning_studio` |
| bien-être | `wellness_center` |
| sauna | `sauna` |
| tatoueur | `body_art_service` |
| piercing | `body_art_service` |

Liste modifiable en tête de script. Autres types dispo si besoin : `hair_care`, `foot_care`.

---

## 2. Contrainte clé : Google ne donne pas la date de création d'une fiche

Aucune API Google n'expose la date de création d'une fiche. Donc :

**Méthode = diff de snapshots.** Chaque semaine on liste *toutes* les fiches des secteurs ciblés dans le Var, on compare aux `place_id` déjà connus → les nouveaux `place_id` = leads.

Conséquences :
- **Semaine 0 = baseline** : indexe l'existant (~5 000 à 10 000 fiches estimées), aucun lead envoyé.
- « Nouveau » = *apparu dans Google depuis la semaine dernière*, pas forcément *entreprise créée cette semaine*. Une fiche créée en retard par un pro installé depuis 2 ans remontera.
- Faux positifs possibles (fiche ancienne nouvellement classée, changement de place ID) → filtrés par le nombre d'avis (§3.4).

Si la vraie date de création légale compte → module SIRENE (§6, prévu en V2).

---

## 3. Technique

### 3.1 Stack

| Brique | Choix | Pourquoi |
|---|---|---|
| Langage | Python 3.12, **stdlib uniquement** (`urllib`, `json`, `smtplib`, `email`, `csv`) | Zéro dépendance à installer/maintenir |
| Données Google | **Places API (New)** — Text Search + Place Details | Seule API Google officielle pour les fiches |
| Stockage état | `data/seen.json` commité dans le repo | Pas de BDD ; Google autorise le stockage illimité des `place_id` |
| Planification + exécution | **GitHub Actions** (cron hebdo), repo privé | Gratuit (2 000 min/mois en privé, on en consomme ~60) |
| Email | **Gmail SMTP** + mot de passe d'application | Gratuit, rien à configurer côté domaine |

### 3.2 Coût Google — pourquoi c'est gratuit

Tarifs Places API (New), vérifiés le 20/10/2026 :

| SKU | Gratuit / mois | Usage LeadPlan |
|---|---|---|
| Text Search **Essentials (IDs Only)** | **Illimité** | Le balayage hebdo complet (~2 500–5 000 requêtes) |
| Place Details **Enterprise** | 1 000 | Détails des nouvelles fiches uniquement (~20–100 / sem.) |

Règle d'or : le balayage demande **uniquement** `places.id,nextPageToken` dans le field mask. Ajouter un seul champ (ex. `displayName`) bascule en SKU Pro payant au-delà de 5 000/mois. → constante figée + test.

Garde-fous :
1. Plafond code : max **200** Place Details par exécution (≤ 870/mois < 1 000). Le surplus reste `pending` pour la semaine suivante.
2. Quota Google Cloud : limiter « Place Details » à ~250 req/jour.
3. Alerte budget Google Cloud à 1 €.

### 3.3 Balayage (Text Search IDs-only)

Limite API : 20 résultats/page, **60 max** par requête (3 pages). Une requête « coiffeur » sur Toulon sature → il faut quadriller.

- Bounding box du Var : lat 42.97 → 43.82, lng 5.65 → 6.95.
- Grille initiale 0,1° × 0,1° (~8 × 11 km) → ~117 cellules.
- Pour chaque cellule × chaque requête : `POST places:searchText` avec `locationRestriction.rectangle`, `includedType`, `strictTypeFiltering: true`, `languageCode: fr`, `regionCode: FR`, `pageSize: 20`, pagination via `nextPageToken`.
- **Découpage adaptatif** : si une cellule renvoie 60 résultats (saturée) → split en 4, on recommence. Profondeur max 5 (~250 m de côté).
- Dédoublonnage par `place_id` toutes requêtes confondues.
- Les cellules de bord débordent sur 13 / 06 / 04 → filtrées au code postal à l'étape détails.

Estimation (15 requêtes × ~117 cellules + pagination + découpages) : ~2 500–5 000 requêtes, ~10–25 min d'exécution, 0 €.

### 3.4 Enrichissement + filtres (Place Details)

Pour chaque `place_id` nouveau (absent de `seen.json`) :

`GET places/{id}` avec field mask :
`displayName,formattedAddress,addressComponents,primaryTypeDisplayName,businessStatus,googleMapsUri,nationalPhoneNumber,websiteUri,userRatingCount`

Filtres :
1. Code postal ne commence pas par `83` → `out_of_zone`, pas envoyé.
2. `businessStatus` ≠ `OPERATIONAL` → `closed`, pas envoyé.
3. `userRatingCount` ≤ 5 → section **Nouvelles fiches probables** ; sinon → **À vérifier**. Seuil configurable.


### 3.5 État — `data/seen.json`

```json
{
  "google": {
    "ChIJ...": { "first_seen": "2026-10-26", "status": "baseline" },
    "ChIJ...": { "first_seen": "2026-11-02", "status": "sent" }
  }
}
```

Statuts : `baseline`, `pending` (détails pas encore récupérés, plafond atteint), `sent`, `out_of_zone`, `closed`.
On ne stocke **que** `place_id` + date + statut (conforme aux CGU Google, cf. §7). Les détails sont relus à chaque envoi, jamais archivés.

### 3.6 Exécution — GitHub Actions

`.github/workflows/weekly.yml` :
- `schedule: cron "0 6 * * 1"` (lundi 06:00 UTC = 7h/8h Paris) + `workflow_dispatch` pour lancer à la main.
- Steps : checkout → `python leadplan.py` → commit/push de `data/seen.json`.
- Secrets : `API_KEY_GOOGLE_CLOUD`, `APP_PASSWORD_GOOGLE`, `GMAIL_ADDRESS`, `MAIL_TO` (optionnel, défaut = `GMAIL_ADDRESS`).
- Échec du workflow → GitHub envoie un email d'échec automatiquement.
- Le commit hebdo de `seen.json` maintient le repo actif (GitHub coupe les crons après 60 jours d'inactivité).

### 3.7 Arborescence

```
LeadPlan/
├── leadplan.py              # tout le pipeline (~250 lignes)
├── test_leadplan.py         # tests minimaux (field mask, tiling, filtres)
├── data/seen.json
├── .github/workflows/weekly.yml
├── .env.example             # noms des variables, sans valeurs
└── SPEC.md
```

Modes CLI : `--baseline` (indexe sans envoyer), `--dry-run` (calcule, affiche, n'envoie pas, n'écrit pas l'état).

---

## 4. Ce dont j'ai besoin (prérequis à créer par toi)

**Ne colle jamais une clé ou un mot de passe dans le chat.** Tu les mets dans un `.env` local (ignoré par git) pour les tests, puis dans les Secrets GitHub.

1. **Google Cloud** (~15 min)
   - Créer un projet sur console.cloud.google.com.
   - Activer la facturation (**carte bancaire obligatoire** même pour l'usage gratuit).
   - Activer **Places API (New)** (pas l'ancienne « Places API »).
   - Créer une clé API, la **restreindre** à Places API (New).
   - Quotas : Place Details ≤ 250/jour. Budget : alerte à 1 €.
2. **Gmail** (~5 min)
   - Validation en 2 étapes activée sur le compte expéditeur.
   - Créer un **mot de passe d'application** (myaccount.google.com → Sécurité → Mots de passe des applications).
   - Adresse destinataire des emails.
3. **GitHub** (~5 min)
   - Repo **privé** `leadplan`.
   - Secrets (Settings → Secrets and variables → Actions), ou `gh secret set -f .env`.
4. **Python 3.12** installé en local pour les tests.

---

## 5. Plan d'implémentation

| # | Étape | Qui | Durée | Done quand |
|---|---|---|---|---|
| 0 | Prérequis §4 | Toi | 30 min | `.env` rempli, clé testée |
| 1 | Balayage Text Search IDs-only + tiling adaptatif, `--dry-run` | Moi | 2 h | Nb d'IDs uniques affiché, aucune cellule saturée |
| 2 | Contrôle couverture : comparer avec Google Maps à la main sur 3 communes (Toulon, Brignoles, Saint-Tropez) | Toi + moi | 30 min | ≥ 95 % des fiches visibles retrouvées |
| 3 | `seen.json` + `--baseline` | Moi | 30 min | Baseline commitée |
| 4 | Place Details + filtres + plafond 200 | Moi | 1 h | Leads filtrés corrects sur un échantillon |
| 5 | Email HTML + CSV via Gmail SMTP | Moi | 1 h | Email reçu, lisible sur mobile |
| 6 | Workflow GitHub Actions + secrets | Moi + toi (secrets) | 30 min | Run manuel `workflow_dispatch` vert |
| 7 | Rodage 2–3 semaines : ajuster seuil d'avis, requêtes | Toi + moi | — | Taux de faux positifs acceptable |

Total dev : ~1 journée. Premier email utile : semaine suivant la baseline.

---

## 6. V2 : module SIRENE (INSEE)

Décidé : hors V1. Prérequis le moment venu : compte portail-api.insee.fr + clé API Sirene (gratuite, 30 req/min), secret `INSEE_API_KEY`. ~1 h de dev → section « Créations officielles » dans le même email.

Google ne sait pas *quand* une entreprise est créée ; SIRENE si, officiellement, gratuitement.

- API Sirene (INSEE), requête hebdo unique :
  `periode(activitePrincipaleEtablissement:(96.02A OR 96.02B OR 96.04Z)) AND codePostalEtablissement:83* AND dateCreationEtablissement:[J-60 TO J]`
  - 96.02A Coiffure · 96.02B Soins de beauté · 96.04Z Entretien corporel (spa, massage non médical).
  - Fenêtre 60 jours + diff sur SIRET déjà vus (même mécanique que Google) → absorbe le délai d'enregistrement.
- **Bascule NAF 2025 début janvier 2027** : les codes changent (aucun code commun avec NAF 2008). Filtrer aussi sur le groupe NAF 2025 `96.2x` (coiffure / soins de beauté / spa) — codes exacts à confirmer sur la table de correspondance INSEE avant le 1er janvier 2027.
- Limites : pas de téléphone ; certains entrepreneurs individuels en diffusion partielle (nom/adresse masqués) ; beaucoup d'esthéticiennes à domicile n'ont jamais de fiche Google → SIRENE les capte, Google non.
- V2 possible : rapprocher un lead SIRENE d'une fiche Google (nom + commune) pour récupérer le téléphone.

---

## 7. Risques & conformité

- **CGU Google Maps Platform** : stockage des `place_id` autorisé sans limite ; stockage/export du reste du contenu (nom, téléphone…) interdit hors usage Maps. Un email de leads est une zone grise → on n'archive rien d'autre que les IDs. Risque réaliste : suspension de la clé, pas plus.
- **RGPD / prospection** : une esthéticienne en micro-entreprise = personne physique → données personnelles. Prospection B2B tolérée si le message concerne son activité pro et propose une désinscription. Ne pas revendre la liste.
- **Faux positifs** semaines 1–2 (trous de couverture du baseline) → le seuil d'avis et le rodage les absorbent.
- **Évolution API** : la limite de 60 résultats est « susceptible de changer » selon Google → le découpage adaptatif s'adapte tout seul.
- **Coûts** : le seul vrai risque financier est un field mask modifié par erreur → test automatisé + quotas + alerte budget.
