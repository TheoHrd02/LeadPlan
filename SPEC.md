# LeadPlan — Spécification

Email quotidien listant les nouveaux leads beauté / bien-être (métiers Planity) dans le Var (83) : créations d'entreprises officielles (SIRENE), annonces légales de création et de reprise (BODACC) + nouvelles fiches Google. Pas d'interface. Coût cible : 0 €.

---

## 1. Fonctionnel

**Entrée** : rien (tâche planifiée).
**Sortie** : 1 email par jour, ~8h (heure du PC).

Contenu de l'email :
- Objet : `LeadPlan 22/10 — 2 créations, 3 fiches Google`
- Section **Créations d'entreprises** (SIRENE, §6) : établissements apparus depuis hier et créés il y a moins de 120 jours. Nom (lien fiche officielle annuaire-entreprises) · Activité · Adresse · Date de création · lien « chercher » sur Google Maps (pas de téléphone dans SIRENE).
- Section **Annonces légales BODACC** (§6 bis) : créations et reprises de fonds dont l'activité décrite est une prestation beauté / bien-être, hors doublons SIRENE. Nom (lien annonce) · Type · Activité · Commune · Début d'activité · « chercher ».
- Section **Nouvelles fiches Google** : fiches jamais vues, hors professions de santé, dont le premier avis a moins de 60 jours (ou aucun avis). Tableau Nom · Catégorie · Commune · Téléphone · Site · Nb avis · Date du 1er avis · Lien Google Maps.
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
2. Fiche retenue seulement : `GET places/{id}` (Enterprise + Atmosphere, 1 000 gratuits/mois) : `nationalPhoneNumber,websiteUri,userRatingCount,reviews`.

Filtres :
1. Code postal ne commence pas par `83` → `out_of_zone`, pas envoyé.
2. `businessStatus` ≠ `OPERATIONAL` → `closed`, pas envoyé.
3. `primaryType` hors `ALLOWED_TYPES` → `off_target`, pas envoyé.
4. Nom contenant une profession de santé (`HEALTH_NAME_RE` : ostéopathe, sage-femme, infirmier, psychothérapeute, aide à domicile…) → `off_target` (dès l'appel Pro).
5. **Âge** : plus ancien avis visible (champ `reviews`, 5 avis max) antérieur à `GOOGLE_MAX_AGE_DAYS` = **60 jours** → `old`, pas envoyé (seuil d'un an jusqu'au 29/10 : laissait passer des fiches avec un premier avis de fin 2025 ; on ne veut que des fiches nouvelles). Aucun avis = âge inconnu, gardé. Le nombre d'avis n'est pas un bon indicateur (un salon ouvert cette semaine peut avoir 18 avis) ; la date du premier avis l'est. Mesuré sur les 51 fiches envoyées du 23 au 26/10 : 23 avaient un premier avis de plus d'un an (16 de plus de 3 ans), 9 étaient des professions de santé ; le filtre en garde 26.

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

- Tâche `LeadPlan` : tous les jours 08:00 (heure du PC) puis **toutes les heures jusqu'à 23:00**, `C:\Python314\pythonw.exe leadplan.py --log leadplan.log`, dossier du projet. Le script sort aussitôt si l'email du jour est déjà parti (`data/last_sent.txt`) ; s'il est parti sans SIRENE (API trop lente, `last_sent.txt` = « date sirene »), la relance suivante refait SIRENE seul et envoie un email « complément » avec les créations → un lancement coupé (redémarrage Windows, veille) est rattrapé à l'heure suivante. Limite d'exécution 45 min.
- `pythonw` → aucune fenêtre ; sortie et erreurs ajoutées à `leadplan.log` (ignoré par git).
- PC éteint ou en veille à 8h → lancée dès qu'il redémarre (« StartWhenAvailable »). Échec → 3 relances à 30 min d'intervalle.
- Incidents des 27-28/10/2026 : redémarrage Windows 1 min après le lancement (non rattrapé à l'époque) ; boucle de pagination infinie de Google sur une requête texte seul (corrigée : `MAX_PAGES`, arrêt sur page sans nouvelle fiche).
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

- **Source principale (depuis le 29/10/2026)** : API Sirene de l'INSEE (`api.insee.fr/api-sirene/3.11/siret`), clé gratuite `INSEE_API_KEY` (portail-api.insee.fr, application « Simple », plan Public), en-tête `X-INSEE-Api-Key-Integration`. **Une seule requête par jour** : `codePostalEtablissement:83* AND dateCreationEtablissement:[J-120 TO J+365] AND periode((NAF 2008 ou NAF 2025 ciblés) AND etatAdministratifEtablissement:A)` → ~190 établissements en ~5 s ; la période en cours doit être active et dans une activité ciblée. Codes NAF 2025 déjà inclus : la bascule de janvier 2027 ne casse rien.
- **Secours sans clé** (ancienne source) : API [Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr) (État, celle d'annuaire-entreprises.data.gouv.fr). Gratuite, **sans clé**, 7 req/s. (L'API Sirene de l'INSEE exige une clé : écartée.)
- **Requête** : `activite_principale` ∈ {96.02A Coiffure, 96.02B Soins de beauté, 96.04Z Entretien corporel} × `departement=83` × `etat_administratif=A`, paginée (25/page, ~260 pages, ~6 min, en parallèle du balayage Google).
- **Filtre établissement** : actif, code postal 83, activité de l'*établissement* ciblée (une société de coiffure peut avoir une holding ou un local commercial dans le Var : écartés).
- **Diff** sur SIRET (`data/sirene_seen.json`) : SIRET jamais vu **et** créé il y a moins de 120 jours → lead ; jamais vu mais ancien → `old` (changement d'activité, déménagement), pas envoyé. Premier passage = baseline silencieuse.
- **Volume mesuré** (22/10/2026) : 5 876 établissements actifs ; 4 créés en 7 jours, 35 en 30 jours (12 coiffure, 17 beauté, 6 entretien corporel), 183 en 120 jours → ~1 création/jour.
- **NAF 2025 (bascule janvier 2027)** : correspondances relevées dans les données : 96.02A → 96.21G / 96.21H, 96.02B → 96.22Y, 96.04Z → 96.23Y. Le filtre établissement accepte déjà les deux. L'API refuse aujourd'hui les codes NAF25 en paramètre ; à la bascule, l'erreur apparaîtra en rouge dans l'email → remplacer `NAF_CODES` par les codes NAF25.
- **Limites** : pas de téléphone (lien « chercher » sur Google Maps à la place) ; bien-être hors NAF beauté (sophrologue, naturopathe… souvent en 86.90F) non couvert par SIRENE, seulement par Google ; délai de quelques jours à quelques semaines entre création et apparition dans SIRENE.
- SIRENE en panne → l'email part quand même avec les leads Google et un avertissement.
- **Fiabilité (constatée les 27-29/10/2026)** : l'API répond en ~1,5 s par page, mais par moments en 20-35 s ou en 429 (surcharge côté État, aucun en-tête de limite ni Retry-After ; nous sommes très en dessous de 7 req/s). Avec ~260 pages par passage, ces périodes bloquaient le script 25-40 min. Désormais : 15 s max par page, 1 seul nouvel essai, 8 min max au total (`SIRENE_MAX_SECONDS`), progression dans le journal ; au-delà, abandon net, email sans SIRENE, et la relance horaire retente SIRENE seul (email « complément »). Solution de fond : API Sirene de l'INSEE (clé gratuite) avec filtre sur la date de création → 1 requête par jour au lieu de 260.
- V3 possible : rapprocher une création SIRENE d'une fiche Google (nom + commune) pour récupérer le téléphone.

## 6 bis. BODACC (annonces légales) — en place

Complément de SIRENE, pas un remplaçant. Mesuré sur 30 jours dans le Var (22/10/2026) : 35 créations SIRENE beauté, 47 créations BODACC repérées par mots-clés, **5 en commun seulement**. SIRENE seul : surtout des artisans (coiffeuses, esthéticiennes) non inscrits au registre du commerce. BODACC seul : beaucoup de bruit (vente de cosmétiques en ligne, etc.) mais de vrais leads que SIRENE rate : indépendants multi-activités dont le code NAF principal n'est pas la beauté (cils + coursier, prothésiste ongulaire + véhicules), bien-être (naturopathie, hypnose, drainage lymphatique), et sociétés publiées au BODACC avant d'apparaître dans SIRENE.

- **Source** : API BODACC (opendatasoft, DILA), gratuite, sans clé. Familles `creation`, `immatriculation`, `vente` (reprises de fonds), département 83, parutions des 14 derniers jours relues chaque jour, diff sur l'id d'annonce (`data/bodacc_seen.json`). Pas de baseline : le premier passage envoie les annonces pertinentes des 14 derniers jours.
- **Filtre d'activité** (texte libre, pas de code NAF) : l'activité est découpée en morceaux (« - », « ; », « . ») ; un morceau compte si une prestation ciblée (`SERVICE_RE` : coiffure, esthéticienne, soins esthétiques, onglerie, cils, épilation, massage, drainage lymphatique, naturopathie, hypnose, tatouage, perçage…) y est citée **avant** toute notion de vente ou de travaux (`RETAIL_RE`). Ex. « coiffure - vente de produits » → oui ; « vente en ligne de produits de beauté, maquillage » → non. Vérifié sur les 55 annonces réelles du mois : 20 gardées, toutes pertinentes, 24 écartées.
- **Dédoublonnage** : une création dont le SIREN est déjà connu de SIRENE est écartée ; une société déjà envoyée via BODACC n'est pas renvoyée ensuite par SIRENE. Les reprises sont gardées même si l'acheteur est connu (nouveau propriétaire = nouveau lead).
- **Volume attendu** : ~5 leads BODACC par semaine en plus de SIRENE.

---

## 7. Risques & conformité

- **CGU Google Maps Platform** : stockage des `place_id` autorisé sans limite ; stockage/export du reste du contenu (nom, téléphone…) interdit hors usage Maps. Un email de leads est une zone grise → on n'archive rien d'autre que les IDs. Risque réaliste : suspension de la clé, pas plus.
- **RGPD / prospection** : une esthéticienne en micro-entreprise = personne physique → données personnelles. Prospection B2B tolérée si le message concerne son activité pro et propose une désinscription. Ne pas revendre la liste.
- **Faux positifs** semaines 1–2 (trous de couverture du baseline) → le seuil d'avis et le rodage les absorbent.
- **Évolution API** : la limite de 60 résultats est « susceptible de changer » selon Google → le découpage adaptatif s'adapte tout seul.
- **Coûts** : le seul vrai risque financier est un field mask modifié par erreur → test automatisé + quotas + alerte budget.
