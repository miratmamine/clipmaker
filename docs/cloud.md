# Utiliser ClipMaker sans le PC (gratuit, sans carte bancaire)

Objectif : lancer un traitement depuis le téléphone alors que le PC est éteint,
sans rien payer et sans saisir de carte bancaire.

## 1. Principe

| Rôle | Service | Offre gratuite |
| --- | --- | --- |
| Interface web (pages, file d'attente) | **Render** | 750 h/mois, 512 Mo, sans carte |
| Calcul vidéo (yt-dlp, ffmpeg, sous-titres) | **GitHub Actions** | illimité sur dépôt public, 2000 min/mois sur dépôt privé |
| Transcription audio | **Groq** (`whisper-large-v3-turbo`) | 20 req/min, 7 200 s/h, 28 800 s/jour |
| Sélection des extraits (LLM) | **Groq** (`openai/gpt-oss-120b`) | inclus |
| File d'attente + stockage des MP4 | **Supabase** | 500 Mo de base, 1 Go de stockage |

```text
téléphone ──► Render (Flask) ──► PostgreSQL Supabase (table jobs)
    │               │                      ▲
    │   envoi vidéo │ repository_dispatch  │ job réclamé / terminé
    └──► Supabase Storage (sources)        │
                    ▼                      │
             GitHub Actions (worker) ──────┘
                    │
                    ├──► Groq (transcription + sélection)
                    └──► Supabase Storage (MP4 + URL signée)
```

Pourquoi deux services ? Le plan gratuit de Render ne fournit que ~0,1 CPU et
512 Mo : découper et sous-titrer une vidéo y serait très lent. GitHub Actions
offre des machines bien plus puissantes, gratuitement, mais elles ne démarrent
qu'à la demande — d'où le déclenchement depuis Render.

## 2. Supabase (base + stockage)

1. Créer un compte sur <https://supabase.com> (gratuit, sans carte) puis un
   nouveau projet. Choisir une région proche (Europe) et **noter le mot de
   passe de la base** (il ne sera plus affiché).
2. Récupérer les trois identifiants :
   - **Project Settings → Database → Connection string → URI** : copier l'URI
     du **pooler** (port `5432` ou `6543`). Elle ressemble à
     `postgresql://postgres.<ref>:<mot_de_passe>@aws-0-<region>.pooler.supabase.com:6543/postgres`.
     Ajouter `?sslmode=require` si absent. Si le mot de passe contient des
     caractères spéciaux (`@`, `#`, `/`, `:`), il faut les encoder en
     pourcentage (`@` → `%40`, etc.).
   - **Project Settings → API** : copier `Project URL` et la clé
     `service_role` (section « Project API keys »). La clé `anon` ne suffit
     pas.
3. Renseigner ces trois valeurs dans `.env.cloud` puis laisser le script faire
   le reste :

```bash
python scripts/setup_supabase.py
```

Il applique `supabase/schema.sql` (tables, index, RLS), crée le bucket privé
`clips` (50 Mo max/fichier), vérifie la RLS, puis fait un aller-retour réel
(upload → URL signée → relecture → suppression). Le script est idempotent :
`--check` se contente de vérifier l'existant. Plus besoin de toucher au SQL
Editor ni à Storage à la main.

> La clé `service_role` contourne les RLS : elle ne doit vivre que dans les
> secrets de Render et de GitHub, jamais dans l'APK ni dans un dépôt git.

## 3. Groq (transcription + sélection)

1. Compte gratuit sur <https://console.groq.com> (aucune carte).
2. **API Keys → Create API Key**, copier la clé `gsk_...`.
3. La même clé sert aux deux étapes :

```ini
GROQ_API_KEY=gsk_...
OPENAI_API_KEY=gsk_...                    # même clé
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_MODEL=openai/gpt-oss-120b
```

La transcription utilise `whisper-large-v3-turbo` avec timestamps par mot.
L'audio est découpé en morceaux de 10 minutes (≈ 4,8 Mo) pour rester sous la
limite de 25 Mo. Les appels sont espacés pour respecter ~20 requêtes/minute.

> Les modèles Groq changent régulièrement : un modèle retiré provoque une erreur
> `404 model_not_found` au moment de la sélection. Vérifie la liste à jour sur
> <https://console.groq.com/docs/models> (ou via
> `https://api.groq.com/openai/v1/models`).

## 4. GitHub (le runner de calcul)

Le dépôt contient le code et le workflow `.github/workflows/worker.yml`. Le
script `scripts/setup_github.py` s'occupe de la création, de l'envoi et des
secrets.

1. Créer un token **classique** avec la portée `repo` :
   <https://github.com/settings/tokens/new>.
2. L'enregistrer localement (saisie masquée, jamais dans le chat) :

```powershell
cd "C:\Users\User\Documents\Default Project\clipmaker"
$t = Read-Host "Token GitHub"
Set-Content -Path ".gh_token" -Value $t -NoNewline
Test-Path ".gh_token"
```

3. Publier :

```powershell
.\.venv\Scripts\python.exe scripts\setup_github.py
```

Le script :

- vérifie le token et affiche le compte ;
- crée le dépôt `clipmaker` s'il n'existe pas ;
- commite et pousse le code sur la branche `main` ;
- enregistre les secrets du workflow (`DATABASE_URL`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
  `OPENAI_MODEL`) ;
- écrit `render-env.txt` (à coller dans Render à l'étape suivante).

Les cookies YouTube sont facultatifs et s'ajoutent à part (voir la section 5) :
`python scripts/setup_cookies.py cookies.txt` enregistre le secret
`YOUTUBE_COOKIES`.

> **Dépôt public ou privé ?** Public = minutes Actions illimitées (le code ne
> contient aucun secret). Privé = 2000 min/mois, largement suffisant pour un
> usage personnel, mais il faut passer `--private`.
>
> `--secrets` met seulement à jour les secrets, `--dry-run` simule.

### Déclencheurs du workflow

| Déclencheur | Usage |
| --- | --- |
| `repository_dispatch` (`clipmaker-job`) | envoyé par Render à chaque job : démarrage immédiat |
| `workflow_dispatch` | bouton **Run workflow** pour tester |
| `schedule` (`*/15 * * * *`) | filet de sécurité si le déclenchement a échoué |

Chaque exécution lance `python worker.py --once` : elle réclame **un** job, le
traite, puis s'arrête. Si la file est vide, le job GitHub dure quelques
secondes.

## 5. Récupérer la vidéo quand YouTube bloque

Testé en vrai : depuis une adresse IP de datacenter (GitHub Actions), YouTube
répond « Sign in to confirm you're not a bot » **quel que soit le réglage**
(8 clients yt-dlp, empreintes TLS `curl_cffi`, jetons PO `bgutil`, instances
Invidious/Piped). Le blocage vient de l'adresse IP, pas de l'outil. Deux
méthodes fonctionnent, et elles se complètent.

### A. Envoyer la vidéo depuis le téléphone (toujours fiable)

1. Télécharger la vidéo sur le téléphone (site ou application au choix), en MP4
   de préférence.
2. Sur la page d'accueil de ClipMaker, utiliser le formulaire **« Ou envoyer une
   vidéo »**, choisir le fichier et lancer.

Le fichier passe par Render puis est déposé dans Supabase Storage
(`sources/<jeton>/<fichier>`), le job est créé avec `upload://…`, et le runner
le récupère depuis Storage. La vidéo source est supprimée dès la fin du job ;
en cas de plantage du runner, elle est purgée après `SOURCE_RETENTION_DAYS`
(3 jours par défaut).

> - Le plan gratuit Supabase limite chaque fichier à **50 Mo**. Le navigateur
>   découpe donc la vidéo en morceaux de `MAX_CHUNK_MB=40` Mo et le worker les
>   réassemble : la limite totale est `MAX_UPLOAD_MB=500` Mo.
> - La vidéo traverse trois fois la bande passante (vers Render, vers Supabase,
>   puis vers le runner) : 5 Go/mois ≈ 3 vidéos de 500 Mo, ou ~8 de 200 Mo.
>   Au-delà, attendre le mois suivant ou passer à un plan payant.
> - Aucune dépendance à YouTube : c'est la méthode de secours permanente.

### B. Cookies YouTube (automatique)

1. Se connecter à YouTube dans un navigateur (un **compte jetable** est
   conseillé).
2. Exporter les cookies de `youtube.com` au format Netscape (extension « Get
   cookies.txt LOCALLY », ou `yt-dlp --cookies-from-browser firefox`).
3. Enregistrer le secret :

```powershell
.\.venv\Scripts\python.exe scripts\setup_cookies.py cookies.txt
```

Le script vérifie le format, encode le fichier et l'enregistre dans le secret
`YOUTUBE_COOKIES` (limite GitHub : **48 Ko**). Le workflow le décode dans un
fichier temporaire et `downloader.py` le passe à yt-dlp
(`YTDLP_COOKIES_FILE`).

> Les cookies expirent souvent en 1 à 3 mois (relancer la commande avec un
> fichier frais). Même avec des cookies, Google peut refuser une IP de
> datacenter : garder la méthode A sous la main. Sans cookies, l'erreur
> affichée dans le journal propose justement l'envoi depuis le téléphone.

### C. Dernier recours : le PC

`python worker.py --once` traite un job de la file depuis le PC (avec
`.env.cloud`) : l'IP résidentielle passe généralement là où GitHub échoue.

### D. Éviter le blocage « pour de bon » : proxy résidentiel

YouTube ne bloque pas ClipMaker : il bloque les **adresses IP de datacenter**
(GitHub Actions, Render, VPS). Aucune option gratuite ne lève ce blocage de
façon permanente — la seule parade technique durable est de faire sortir le
téléchargement par une connexion résidentielle ou mobile.

`downloader.py` lit la variable `YTDLP_PROXY` et la transmet à yt-dlp
(`http://user:motdepasse@hote:port`). Côté GitHub Actions, elle vient du secret
`YTDLP_PROXY` :

```powershell
# une seule fois : enregistrer le proxy dans les secrets Actions
$env:GH_TOKEN = (Get-Content .gh_token -Raw).Trim()
gh secret set YTDLP_PROXY --repo miratmamine/clipmaker
```

Hiérarchie des solutions, de la plus fiable à la plus fragile :

| Solution | Coût | Fiabilité | Remarque |
| --- | --- | --- | --- |
| A. Envoi depuis le téléphone | 0 | 100 % | toujours disponible, ~2 min de plus |
| D. Proxy résidentiel | ~3–10 $/mois | élevée | seul vrai correctif « automatique » |
| B. Cookies YouTube | 0 | variable | améliore les chances, expire en 1–3 mois |
| C. PC allumé / vieux téléphone en proxy | 0 | élevée si allumé | dépend d'une machine à la maison |
| Clients yt-dlp, PO token, Piped/Invidious | 0 | nulle depuis un datacenter | déjà testés, abandonnés |

> Astuce 100 % gratuite si tu as un vieux téléphone Android : le laisser
> branché en Wi-Fi, y installer une app de proxy HTTP (ou Termux + `tinyproxy`)
> et mettre son adresse dans `YTDLP_PROXY`. L'IP devient celle de l'opérateur
> mobile, que YouTube bloque beaucoup moins. C'est plus fragile qu'un proxy
> payant, mais ça ne coûte rien.

## 6. Render (l'interface)

1. Créer un compte sur <https://render.com> en choisissant **GitHub** comme
   moyen de connexion (gratuit, aucune carte demandée).
2. **New → Blueprint**, sélectionner le dépôt `clipmaker`, puis **Apply**.
   Render lit `render.yaml` : service web Python, plan gratuit, région
   Francfort, santé sur `/healthz`.
3. Ouvrir le service → **Environment** → **Add from .env**, ouvrir
   `render-env.txt` et coller tout son contenu, puis **Save changes**.

   Si Render a créé un **Env Group** (page *Env Groups*) au lieu de coller
   directement dans le service, il faut aussi le lier : service →
   **Environment** → section **Environment Groups** → **Link Environment
   Group** → choisir le groupe → **Save changes**. Un groupe non lié n'a
   aucun effet et `/healthz` reste sur `{"status":"config"}`.

4. Attendre la fin du déploiement (statut **Live**), puis ouvrir
   `https://<nom>.onrender.com/healthz` : la réponse doit être
   `{"mode":"cloud","status":"ok"}`.

> Tant que les variables manquent, le service démarre quand même : la page
> affiche « Configuration incomplète » et `/healthz` répond
> `{"mode":"cloud","status":"config"}`. C'est volontaire : gunicorn ne
> plante plus sur l'import de `yt-dlp`.

Adresse publique : `https://<nom>.onrender.com`. En cas de doute, l'onglet
**Logs** affiche les erreurs de démarrage.

> Le service gratuit s'endort après 15 minutes sans visite : la première page
> met alors ~1 minute à s'afficher. Le traitement, lui, continue sur GitHub.

## 7. Depuis le téléphone

1. Ouvrir l'adresse Render dans le navigateur du téléphone.
2. Se connecter avec `APP_PASSWORD`.
3. **Méthode YouTube** : coller une URL, choisir 1 à 10 clips, lancer (utile si
   les cookies fonctionnent). **Méthode envoi** : formulaire « Ou envoyer une
   vidéo », fichier + nombre de clips.
4. La page se rafraîchit et affiche la progression, puis les clips (lus depuis
   Supabase via une URL signée d'une heure). Un job issu d'un envoi affiche
   « Vidéo envoyée depuis le téléphone » à la place de l'URL.
5. Pour l'APK : télécharger `ClipMaker.apk` depuis la page, l'installer, puis
   saisir l'adresse Render.

## 8. Fonctionnement interne

- `webapp.py` détecte `DATABASE_URL` : en mode cloud, un lancement insère un
  job `queued` dans PostgreSQL, puis appelle `repository_dispatch`. La route
  `/upload` enregistre d'abord la vidéo dans Supabase Storage et crée un job
  `upload://sources/<jeton>/<fichier>`.
- `worker.py --once` remet en file les jobs bloqués (`heartbeat_at` trop
  ancien), réclame un job avec `FOR UPDATE SKIP LOCKED`, récupère la vidéo
  (téléchargement yt-dlp, ou Storage pour un envoi), exécute `main._run` avec la
  transcription Groq, envoie chaque MP4 dans Supabase Storage et enregistre le
  clip. La vidéo source est supprimée à la fin.
- La transcription complète est faite **une seule fois**, puis découpée par
  clip (`groq_transcriber.slice_segments`) : un seul appel Groq par vidéo.
- Les MP4 sont purgés après `RETENTION_DAYS` (14 jours) et au-delà de
  `MAX_STORAGE_MB` (900 Mo) ; les jobs terminés après `JOB_RETENTION_DAYS`
  (60 jours) ; les vidéos envoyées orphelines après `SOURCE_RETENTION_DAYS`
  (3 jours).
- L'interface ne télécharge rien et n'encode rien : elle ne dépend pas de
  ffmpeg (voir `requirements-web.txt`).

## 9. Quotas et limites

| Service | Limite | Conséquence |
| --- | --- | --- |
| Render | 750 h/mois, 512 Mo, 0,1 CPU | veille après 15 min ; ~1 min de réveil |
| Render | 5 Go de bande passante/mois | chaque envoi de vidéo consomme ~2× sa taille |
| GitHub Actions | 6 h max par job | une vidéo de plusieurs heures peut être coupée |
| Supabase | pause après 7 jours sans activité | `/healthz` et le healthcheck Render génèrent de l'activité |
| Supabase | 1 Go de stockage, 50 Mo/fichier | envoi par morceaux de 40 Mo, vidéo totale `MAX_UPLOAD_MB=500` |
| Groq | 7 200 s/h, 28 800 s/jour | privilégier des vidéos de moins d'une heure |

Les runners GitHub utilisent des adresses IP de datacenter : YouTube bloque
presque toujours le téléchargement (voir la section 5). Utiliser l'envoi depuis
le téléphone, les cookies, ou le worker du PC.

## 10. Dépannage

| Symptôme | Cause probable | Solution |
| --- | --- | --- |
| La page du job reste « en attente » | déclenchement refusé | vérifier `GITHUB_REPO` / `GITHUB_DISPATCH_TOKEN` dans Render, puis **Actions → Run workflow** |
| Le job reste « en cours » | runner interrompu | il est remis en file après `STALE_MINUTES` (15 min) par le passage planifié |
| `401` / `403` sur `/run` | mot de passe absent ou faux | se reconnecter, vérifier `APP_PASSWORD` |
| `Impossible de créer le traitement` | base injoignable ou en pause | réveiller le projet Supabase, vérifier `DATABASE_URL` |
| Erreur `404 model_not_found` | modèle Groq retiré | mettre à jour `OPENAI_MODEL` |
| « YouTube demande une vérification anti-robot » | IP de datacenter bloquée | envoyer la vidéo depuis le téléphone, ou `scripts/setup_cookies.py` |
| « Vidéo trop lourde » | au-delà de `MAX_UPLOAD_MB` | réduire la vidéo (≤ 500 Mo) ; le quota bande passante Render peut aussi être épuisé |
| Envoi bloqué vers Render | fichier trop gros / réseau coupé | réessayer en Wi-Fi ; vérifier l'espace Supabase |
| Clip en `502` | URL signée expirée (1 h) | recharger la page du job |
| « Configuration incomplète » sur la page d'accueil | `DATABASE_URL` absent dans Render | Environment → Add from .env (ou lier l'Env Group au service) |
| `/healthz` répond `config` | variables non enregistrées | coller `render-env.txt`, **Save changes**, attendre le redémarrage |

Vérifier l'installation sans rien déployer (PostgreSQL embarqué + Groq et
Storage simulés) :

```bash
python test_cloud.py
```

## 11. Variante « tout-en-un » (VPS, Docker, PC)

Pour un serveur qui reste allumé, `Dockerfile` + `start_cloud.sh` lancent
gunicorn **et** le worker en boucle dans le même conteneur (ffmpeg inclus).
`docker-compose.yml` fournit un PostgreSQL local pour tester sans Supabase.

```bash
docker compose up --build
```

Le mode local (`python main.py`, `python webapp.py`) reste inchangé et ne
nécessite aucun service cloud.
