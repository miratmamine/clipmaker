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
                    │                      ▲
                    │ repository_dispatch  │ job réclamé / terminé
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

## 5. Render (l'interface)

1. Créer un compte sur <https://render.com> en choisissant **GitHub** comme
   moyen de connexion (gratuit, aucune carte demandée).
2. **New → Blueprint**, sélectionner le dépôt `clipmaker`, puis **Apply**.
   Render lit `render.yaml` : service web Python, plan gratuit, région
   Francfort, santé sur `/healthz`.
3. Ouvrir le service → **Environment** → **Add from .env**, ouvrir
   `render-env.txt` et coller tout son contenu, puis **Save changes**.
4. Attendre la fin du déploiement (statut **Live**), puis ouvrir
   `https://<nom>.onrender.com/healthz` : la réponse doit être
   `{"mode":"cloud","status":"ok"}`.

Adresse publique : `https://<nom>.onrender.com`. En cas de doute, l'onglet
**Logs** affiche les erreurs de démarrage.

> Le service gratuit s'endort après 15 minutes sans visite : la première page
> met alors ~1 minute à s'afficher. Le traitement, lui, continue sur GitHub.

## 6. Depuis le téléphone

1. Ouvrir l'adresse Render dans le navigateur du téléphone.
2. Se connecter avec `APP_PASSWORD`.
3. Coller une URL YouTube, choisir 1 à 10 clips, lancer.
4. La page se rafraîchit et affiche la progression, puis les clips (lus depuis
   Supabase via une URL signée d'une heure).
5. Pour l'APK : télécharger `ClipMaker.apk` depuis la page, l'installer, puis
   saisir l'adresse Render.

## 7. Fonctionnement interne

- `webapp.py` détecte `DATABASE_URL` : en mode cloud, un lancement insère un
  job `queued` dans PostgreSQL, puis appelle `repository_dispatch`.
- `worker.py --once` remet en file les jobs bloqués (`heartbeat_at` trop
  ancien), réclame un job avec `FOR UPDATE SKIP LOCKED`, exécute `main._run`
  avec la transcription Groq, envoie chaque MP4 dans Supabase Storage et
  enregistre le clip.
- La transcription complète est faite **une seule fois**, puis découpée par
  clip (`groq_transcriber.slice_segments`) : un seul appel Groq par vidéo.
- Les MP4 sont purgés après `RETENTION_DAYS` (14 jours) et au-delà de
  `MAX_STORAGE_MB` (900 Mo) ; les jobs terminés après `JOB_RETENTION_DAYS`
  (60 jours).
- L'interface ne télécharge rien et n'encode rien : elle ne dépend pas de
  ffmpeg (voir `requirements-web.txt`).

## 8. Quotas et limites

| Service | Limite | Conséquence |
| --- | --- | --- |
| Render | 750 h/mois, 512 Mo, 0,1 CPU | veille après 15 min ; ~1 min de réveil |
| Render | 5 Go de bande passante/mois | les téléchargements de clips comptent |
| GitHub Actions | 6 h max par job | une vidéo de plusieurs heures peut être coupée |
| Supabase | pause après 7 jours sans activité | `/healthz` et le healthcheck Render génèrent de l'activité |
| Supabase | 1 Go de stockage, bucket 50 Mo/fichier | un clip de 60 s pèse 6 à 10 Mo |
| Groq | 7 200 s/h, 28 800 s/jour | privilégier des vidéos de moins d'une heure |

Les runners GitHub utilisent des adresses IP de datacenter : YouTube peut
répondre « Sign in to confirm you're not a bot ». `downloader.py` réessaie
alors automatiquement avec d'autres clients (`tv_simply`, `android_vr`,
`web_embedded`). Si le blocage persiste, deux solutions :

- relancer depuis le téléphone plus tard (l'IP change) ;
- exécuter le job sur le PC : `python worker.py --once` traite un job de la
  file avec `.env.cloud` (et `python worker.py` en continu).

## 9. Dépannage

| Symptôme | Cause probable | Solution |
| --- | --- | --- |
| La page du job reste « en attente » | déclenchement refusé | vérifier `GITHUB_REPO` / `GITHUB_DISPATCH_TOKEN` dans Render, puis **Actions → Run workflow** |
| Le job reste « en cours » | runner interrompu | il est remis en file après `STALE_MINUTES` (15 min) par le passage planifié |
| `401` / `403` sur `/run` | mot de passe absent ou faux | se reconnecter, vérifier `APP_PASSWORD` |
| `Impossible de créer le traitement` | base injoignable ou en pause | réveiller le projet Supabase, vérifier `DATABASE_URL` |
| Erreur `404 model_not_found` | modèle Groq retiré | mettre à jour `OPENAI_MODEL` |
| Clip en `502` | URL signée expirée (1 h) | recharger la page du job |
| `Aucun secret` dans Render | `render-env.txt` non collé | Environment → Add from .env |

Vérifier l'installation sans rien déployer (PostgreSQL embarqué + Groq et
Storage simulés) :

```bash
python test_cloud.py
```

## 10. Variante « tout-en-un » (VPS, Docker, PC)

Pour un serveur qui reste allumé, `Dockerfile` + `start_cloud.sh` lancent
gunicorn **et** le worker en boucle dans le même conteneur (ffmpeg inclus).
`docker-compose.yml` fournit un PostgreSQL local pour tester sans Supabase.

```bash
docker compose up --build
```

Le mode local (`python main.py`, `python webapp.py`) reste inchangé et ne
nécessite aucun service cloud.
