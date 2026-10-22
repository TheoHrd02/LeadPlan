# LeadPlan — Spécification

Email quotidien listant les nouveaux leads beauté / bien-être (métiers Planity) dans le Var (83) : créations d'entreprises officielles (SIRENE) + nouvelles fiches Google. Pas d'interface. Coût cible : 0 €.

---

## 1. Fonctionnel

**Entrée** : rien (tâche planifiée).
**Sortie** : 1 email par jour, ~8h (heure du PC).

Contenu de l'email :
- Objet : `LeadPlan 22/10 — 2 créations, 3 fiches Google`
- Section **Créations d'entreprises** (SIRENE, §6) : établissements apparus depuis hier et créés il y a moins de 120 jours. Nom (lien fiche officielle annuaire-entreprises) · Activité · Adresse · Date de création · lien « chercher » sur Google Maps (pas de téléphone dans SIRENE).
- Section **Nouvelles fiches Google probables** (≤ 5 avis Google) : tableau Nom · Catégorie · Commune · Téléphone · Site · Nb avis · Lien Google Maps.
- Section **Fiches Google à vérifier** (> 5 avis) : fiches détectées pour la première fois mais visiblement anciennes (changement de catégorie, nouvelle place ID, trou de couverture).
- Pièces jointes `creations-2026-10-22.csv` et `google-2026-10-22.csv` (import CRM / tableur).
- Si 0 lead : email envoyé quand même → preuve que le pipeline tourne.

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
| réflexologue, massothérapeute, sophrologue, hypnothérapeute, naturopathe, coach de vie | aucun (texte seul) |

Cible = métiers Planity. Les métiers bien-être sont classés « santé »/« service » par Google → requête texte sans filtre de type, et toute fiche dont la catégorie principale Google n'est pas dans la liste blanche `ALLOWED_TYPES` est écartée à l'étape détails (`off_target`) : hôtels, campings, restaurants, piscinistes, cliniques, kinés, magasins… (ils remontent via un type secondaire « spa »/« bien-être »). Tatouage/piercing conservés à la demande. Cils, épilation, manucure, drainage en institut : déjà couverts par `beauty_salon` / `beautician` / `nail_salon` / `massage` (vérifié sur Toulon). Liste modifiable en tête de script ; **tout ajout de requête impose un `--baseline`** sinon l'existant remonte comme « nouveau ». Autres types dispo si besoin : `hair_care`, `foot_care`.

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
| Planification + exécution | **Planificateur de tâches Windows** (tous les jours 8h) ; GitHub Actions prêt mais désactivé (§3.6) | Gratuit ; PC allumé requis |
| Email | **Gmail SMTP** + mot de passe d'application | Gratuit, rien à configurer côté domaine |

### 3.2 Coût Google — pourquoi c'est gratuit

Tarifs Places API (New), vérifiés le 20/10/2026 :

| SKU | Gratuit / mois | Usage LeadPlan |
|---|---|---|
| Text Search **Essentials (IDs Only)** | **Illimité** | Le balayage quotidien complet (~3 000 requêtes/jour) |
| Place Details **Pro** | 5 000 | 1er appel sur chaque nouvelle fiche : nom, adresse, catégorie → filtres |
| Place Details **Enterprise** | 1 000 | 2e appel, fiches retenues seulement : téléphone, site, avis |

Règle d'or : le balayage demande **uniquement** `places.id,nextPageToken` dans le field mask. Ajouter un seul champ (ex. `displayName`) bascule en SKU Pro payant au-delà de 5 000/mois. → constante figée + test.

Garde-fous :
1. Budget mensuel compté dans `data/usage.json` : 4 500 Pro / 900 Enterprise (marge sous les quotas gratuits). Budget atteint → les fiches restantes restent `pending` jusqu'au mois suivant. Valable quel que soit le nombre de lancements (quotidien + manuels).
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

1. `GET places/{id}` (Pro) : `displayName,formattedAddress,addressComponents,primaryType,primaryTypeDisplayName,businessStatus,googleMapsUri` → filtres ci-dessous (~50 % écartées).
2. Fiche retenue seulement : `GET places/{id}` (Enterprise) : `nationalPhoneNumber,websiteUri,userRatingCount`.

Filtres :
1. Code postal ne commence pas par `83` → `out_of_zone`, pas envoyé.
2. `businessStatus` ≠ `OPERATIONAL` → `closed`, pas envoyé.
3. `primaryType` hors `ALLOWED_TYPES` → `off_target`, pas envoyé.
4. `userRatingCount` ≤ 5 → section **Nouvelles fiches probables** ; sinon → **À vérifier**. Seuil configurable.

Bruit attendu : Google ne renvoie pas exactement les mêmes fiches d'un balayage à l'autre (mesuré : 53 → 46 → 11 → 15 « nouvelles » sur des passages rapprochés sans aucune création). Chaque passage les absorbe dans `seen.json`, le bruit décroît mais ne tombe jamais à zéro.


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

### 3.6 Exécution — Planificateur de tâches Windows (actif)

GitHub Actions bloqué (problème de facturation du compte GitHub) → exécution locale.

- Tâche `LeadPlan` : tous les jours 08:00 (heure du PC), `C:\Python314\pythonw.exe leadplan.py --log leadplan.log`, dossier du projet.
- `pythonw` → aucune fenêtre ; sortie et erreurs ajoutées à `leadplan.log` (ignoré par git).
- PC éteint ou en veille à 8h → lancée dès qu'il redémarre (« StartWhenAvailable »). Échec → 3 relances à 30 min d'intervalle.
- Pas d'email un matin = échec (l'email part même avec 0 lead) → lire `leadplan.log`.
- `data/seen.json` reste local ; le commiter de temps en temps sert de sauvegarde.
- Lancer à la main : `Start-ScheduledTask LeadPlan`. Supprimer : `Unregister-ScheduledTask LeadPlan`.

### 3.6 bis GitHub Actions (désactivé, à réactiver une fois la facturation réglée)

`.github/workflows/weekly.yml` (réactiver : `gh workflow enable weekly-leads`, puis supprimer la tâche Windows) :
- `schedule: cron "0 6 * * *"` (tous les jours 06:00 UTC = 7h/8h Paris) + `workflow_dispatch` pour lancer à la main.
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

## 6. V2 : créations d'entreprises (SIRENE) — en place

Google ne sait pas *quand* une entreprise est créée ; SIRENE (INSEE) si, officiellement. Même source que société.com, mais gratuite.

- **Source** : API [Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr) (État, celle d'annuaire-entreprises.data.gouv.fr). Gratuite, **sans clé**, 7 req/s. (L'API Sirene de l'INSEE exige une clé : écartée.)
- **Requête** : `activite_principale` ∈ {96.02A Coiffure, 96.02B Soins de beauté, 96.04Z Entretien corporel} × `departement=83` × `etat_administratif=A`, paginée (25/page, ~260 pages, ~6 min, en parallèle du balayage Google).
- **Filtre établissement** : actif, code postal 83, activité de l'*établissement* ciblée (une société de coiffure peut avoir une holding ou un local commercial dans le Var : écartés).
- **Diff** sur SIRET (`data/sirene_seen.json`) : SIRET jamais vu **et** créé il y a moins de 120 jours → lead ; jamais vu mais ancien → `old` (changement d'activité, déménagement), pas envoyé. Premier passage = baseline silencieuse.
- **Volume mesuré** (22/10/2026) : 5 876 établissements actifs ; 4 créés en 7 jours, 35 en 30 jours (12 coiffure, 17 beauté, 6 entretien corporel), 183 en 120 jours → ~1 création/jour.
- **NAF 2025 (bascule janvier 2027)** : correspondances relevées dans les données : 96.02A → 96.21G / 96.21H, 96.02B → 96.22Y, 96.04Z → 96.23Y. Le filtre établissement accepte déjà les deux. L'API refuse aujourd'hui les codes NAF25 en paramètre ; à la bascule, l'erreur apparaîtra en rouge dans l'email → remplacer `NAF_CODES` par les codes NAF25.
- **Limites** : pas de téléphone (lien « chercher » sur Google Maps à la place) ; bien-être hors NAF beauté (sophrologue, naturopathe… souvent en 86.90F) non couvert par SIRENE, seulement par Google ; délai de quelques jours à quelques semaines entre création et apparition dans SIRENE.
- SIRENE en panne → l'email part quand même avec les leads Google et un avertissement.
- V3 possible : rapprocher une création SIRENE d'une fiche Google (nom + commune) pour récupérer le téléphone.

---

## 7. Risques & conformité

- **CGU Google Maps Platform** : stockage des `place_id` autorisé sans limite ; stockage/export du reste du contenu (nom, téléphone…) interdit hors usage Maps. Un email de leads est une zone grise → on n'archive rien d'autre que les IDs. Risque réaliste : suspension de la clé, pas plus.
- **RGPD / prospection** : une esthéticienne en micro-entreprise = personne physique → données personnelles. Prospection B2B tolérée si le message concerne son activité pro et propose une désinscription. Ne pas revendre la liste.
- **Faux positifs** semaines 1–2 (trous de couverture du baseline) → le seuil d'avis et le rodage les absorbent.
- **Évolution API** : la limite de 60 résultats est « susceptible de changer » selon Google → le découpage adaptatif s'adapte tout seul.
- **Coûts** : le seul vrai risque financier est un field mask modifié par erreur → test automatisé + quotas + alerte budget.
