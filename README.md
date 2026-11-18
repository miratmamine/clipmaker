# ClipMaker

Pipeline Python qui télécharge une vidéo, la transcrit, sélectionne les meilleurs moments, les découpe et ajoute des sous-titres.

> État actuel : téléchargement, transcription, sélection par LLM, découpe et sous-titres implémentés.

## Structure

```
clipmaker/
├── main.py              # point d'entrée du pipeline
├── downloader.py        # téléchargement de la vidéo source (yt-dlp)
├── transcriber.py       # transcription audio locale (faster-whisper)
├── groq_transcriber.py  # transcription cloud (API Groq)
├── selector.py          # sélection des extraits pertinents (API LLM)
├── cutter.py            # découpe des extraits (ffmpeg)
├── subtitles.py         # génération et incrustation des sous-titres
├── webapp.py            # interface web (locale ou cloud)
├── store.py             # file d'attente et historique (PostgreSQL)
├── storage.py           # clips dans Supabase Storage
├── worker.py            # worker cloud (file PostgreSQL, boucle ou --once)
├── supabase/schema.sql  # tables jobs + clips, RLS
├── docs/cloud.md        # déploiement gratuit (Render + GitHub Actions)
├── .github/workflows/worker.yml # runner Actions (python worker.py --once)
├── render.yaml          # blueprint Render (interface seule)
├── requirements-web.txt # dépendances de l'interface (sans ffmpeg)
├── scripts/setup_supabase.py # tables + bucket + test du stockage
├── scripts/setup_github.py   # dépôt + secrets Actions + render-env.txt
├── scripts/setup_cookies.py  # cookies YouTube → secret Actions (optionnel)
├── Dockerfile           # image tout-en-un (ffmpeg + gunicorn + worker)
├── docker-compose.yml   # test cloud local
├── requirements.txt
├── .env.example         # modèle de configuration locale (clé API)
├── .env.cloud.example   # modèle de configuration cloud
└── README.md
```

## Prérequis

- Python 3.10 ou plus récent
- ffmpeg (requis par yt-dlp et par la découpe/sous-titrage)
- une clé API OpenAI (ou compatible) pour la sélection des extraits

## Installation

### 1. Environnement virtuel

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Dépendances Python

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Cela installe :

- `yt-dlp` : téléchargement des vidéos
- `faster-whisper` : transcription audio locale
- `openai` : appel à l'API LLM pour sélectionner les extraits
- `python-dotenv` : lecture du fichier `.env`
- `httpx` + `psycopg` : mode cloud (Groq, Supabase Storage, PostgreSQL)

### 3. ffmpeg

ffmpeg doit être installé au niveau du système et accessible dans le `PATH`.

**Windows (winget)**

```powershell
winget install --id Gyan.FFmpeg -e
```

**Windows (Chocolatey)**

```powershell
choco install ffmpeg
```

**macOS (Homebrew)**

```bash
brew install ffmpeg
```

**Debian / Ubuntu**

```bash
sudo apt update && sudo apt install -y ffmpeg
```

**Fedora**

```bash
sudo dnf install -y ffmpeg
```

Vérification de l'installation :

```bash
ffmpeg -version
yt-dlp --version
python -c "import faster_whisper; print('faster-whisper OK')"
```

### 4. Clé API

Créez un fichier `.env` à partir du modèle fourni, puis renseignez votre clé :

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
```

**macOS / Linux**

```bash
cp .env.example .env
```

Contenu de `.env` :

```dotenv
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=https://api.openai.com/v1
```

Le fichier `.env` est ignoré par git. `OPENAI_MODEL` et `OPENAI_BASE_URL` sont optionnels et permettent d'utiliser un fournisseur compatible OpenAI.

Pour Google Gemini (clé AI Studio), utilisez son endpoint compatible OpenAI :

```dotenv
OPENAI_API_KEY=cle-google-ai-studio
OPENAI_MODEL=gemini-3.5-flash
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
```

## Utilisation

Lancer le pipeline complet sur une vidéo YouTube :

```bash
python main.py "https://www.youtube.com/watch?v=..." --clips 5
```

`--clips` est optionnel (défaut : 5). Le pipeline enchaîne :

1. téléchargement MP4 1080p dans `./work/` (réutilisé si le fichier existe déjà) ;
2. transcription complète dans `./work/<titre>.json` (réutilisée si elle existe déjà) ;
3. sélection des passages par le LLM ;
4. pour chaque passage : transcription locale avec timestamps relatifs au début du clip, génération du fichier ASS et découpe verticale 1080x1920 avec sous-titres incrustés ;
5. bilan final.

Les clips sont enregistrés dans `./output/` sous la forme `01_titre_nettoye.mp4`. Si un passage échoue, l'erreur est affichée et le pipeline passe au suivant ; le résumé final indique le nombre de clips réussis/échoués ainsi que les fichiers produits avec leur titre et leur score.

### Depuis un téléphone (interface web + APK)

Le traitement reste sur le PC ; le téléphone ne fait qu'afficher l'interface.

1. Sur le PC, lancer :

```bash
python webapp.py
```

ou double-cliquer sur `start_webapp.bat` (utilise `.venv` s'il existe).

Le terminal affiche l'adresse à utiliser, par exemple `http://192.168.1.10:5000`.

2. Depuis le navigateur du téléphone (même Wi-Fi), ouvrir cette adresse. Coller une URL YouTube, choisir le nombre de clips, lancer : la progression s'affiche et les clips se lisent directement dans la page.

3. Autoriser Python dans le pare-feu Windows lors de la première demande, ou ouvrir le port 5000 :

```powershell
New-NetFirewallRule -DisplayName "ClipMaker web" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow
```

#### APK Android (WebView)

Le dossier `android/` contient une petite application native qui affiche cette interface. Elle ne fait aucun traitement : elle charge simplement `http://<IP_DU_PC>:5000`.

Compilation (JDK 17 + Android SDK 34 + Gradle 8.9) :

```bash
cd android
gradle assembleDebug
```

L'APK est produit dans `android/app/build/outputs/apk/debug/app-debug.apk` (une copie prête à installer se trouve à la racine : `ClipMaker.apk`).

Installation sur le téléphone : le plus simple est d'ouvrir l'interface dans le navigateur du téléphone puis de cliquer sur **Télécharger ClipMaker.apk** (la page web sert l'APK ; sinon copie USB, Drive, WhatsApp…). Ouvrir le fichier et autoriser l'installation depuis cette source. Au premier lancement, saisir l'adresse affichée par `webapp.py` dans le champ en haut, puis appuyer sur **Aller**. L'adresse est mémorisée.

Dépannage :

- page blanche / erreur de chargement : vérifier que le PC et le téléphone sont sur le même Wi-Fi, que `webapp.py` tourne et que le pare-feu autorise le port 5000 ;
- l'APK est une version debug : il faut autoriser l'installation hors Play Store ;
- repli sans APK : l'interface est accessible depuis n'importe quel navigateur du téléphone.

Les modules peuvent aussi être utilisés séparément.

## Utilisation sans le PC (cloud)

Le pipeline peut tourner gratuitement, sans carte bancaire, pour être utilisé
depuis le téléphone même quand le PC est éteint :

- **Render** (plan gratuit) héberge l'interface web, qui reste très légère ;
- **GitHub Actions** exécute le calcul vidéo (2-4 CPU, 7-16 Go) quand un job
  arrive, puis s'arrête — aucun serveur à laisser tourner ;
- **Groq** transcrit l'audio (`whisper-large-v3-turbo`, gratuit) ;
- **Supabase** fournit la file d'attente PostgreSQL et un bucket Storage privé
  pour les MP4 (limite gratuite : 500 Mo de base + 1 Go de stockage).

Le disque du runner est éphémère : chaque clip est envoyé dans Supabase Storage
puis purgé automatiquement (14 jours et 900 Mo par défaut).

YouTube bloque les adresses IP de datacenter (« Sign in to confirm you're not a
bot ») : depuis le téléphone, l'interface propose aussi **« Ou envoyer une
vidéo »** (MP4 téléchargé sur le téléphone, ≤ 500 Mo, envoyé par morceaux de
40 Mo pour respecter la limite Supabase de 50 Mo/fichier). Voir
[docs/cloud.md](docs/cloud.md#5-récupérer-la-vidéo-quand-youtube-bloque).

Déploiement pas à pas : **[docs/cloud.md](docs/cloud.md)**. Résumé :

```powershell
# 1. Supabase : tables + bucket privé + test réel
python scripts/setup_supabase.py

# 2. GitHub : dépôt + secrets Actions + render-env.txt
python scripts/setup_github.py

# 3. Render : New → Blueprint → ce dépôt → coller render-env.txt
#    (render.yaml décrit le service : plan gratuit, aucune carte)
```

L'interface devient `https://clipmaker-xxxx.onrender.com` ; c'est cette adresse
qu'il faut saisir dans l'APK. Le mode local (`python main.py`, `python webapp.py`)
reste inchangé et n'a besoin d'aucun service cloud.

Un runner de secours existe aussi sur le PC : `python worker.py --once` traite
un job de la file avec `.env.cloud`, utile si YouTube bloque l'IP de GitHub.

Tester le mode cloud sans rien déployer (PostgreSQL embarqué + serveurs Groq et
Storage simulés) :

```bash
pip install -r requirements-dev.txt
python test_cloud.py
```

Transcrire puis sélectionner les extraits :

```python
from transcriber import transcribe
from selector import select_clips

segments = transcribe("work/ma-video.mp4")  # écrit work/ma-video.json
clips = select_clips(segments, n=5)
# [{"start": 12.5, "end": 47.2, "title": "...", "score": 9}, ...]
```

La sélection envoie la transcription à l'API LLM, qui doit répondre uniquement avec un tableau JSON `[{start, end, title, score}]`. Les réponses mal formées, les extraits incomplets et les erreurs d'API lèvent une `SelectionError`.

Découper un extrait recadré en vertical 1080x1920 :

```python
from cutter import cut_clip

cut_clip("work/ma-video.mp4", 12.5, 47.2, "work/clip.mp4")
```

Le recadrage est centré et sans déformation. Le fichier de sortie est encodé en H.264/AAC.

Pour générer et incruster les sous-titres pendant la découpe, passez les segments de transcription :

```python
from cutter import cut_clip

cut_clip(
    "work/ma-video.mp4",
    12.5,
    47.2,
    "work/clip.mp4",
    words=segments,  # sortie de transcriber.transcribe()
)
```

Un fichier ASS temporaire est généré avec des temps relatifs au début du clip, puis brûlé dans la vidéo : texte blanc, contour noir, bas du centre, 2 à 4 mots par écran. `burn_subtitles=False` permet de désactiver l'incrustation même si `words` est fourni.

Par défaut, les timestamps de `words` sont absolus et le début du clip en est soustrait. Si la transcription a été faite directement sur le clip (timestamps déjà relatifs), passez `words_relative=True` :

```python
cut_clip(
    "work/ma-video.mp4",
    12.5,
    47.2,
    "work/clip.mp4",
    words=segments_du_clip,  # timestamps 0 → durée du clip
    words_relative=True,
)
```

Tester la découpe sur la vidéo présente dans `./work/` :

```bash
python test_cutter.py
python test_cutter.py --json work/selection.json --index 1
python test_cutter.py --start 10 --end 45
```

Le script utilise le premier extrait du JSON de sélection trouvé dans `./work/`, sinon le passage 60 s → 100 s, puis affiche la durée et la résolution du fichier produit via ffprobe.

## Notes

- `faster-whisper` télécharge le modèle Whisper au premier lancement (prévoir de l'espace disque et une connexion réseau).
- Sur une machine sans GPU, la transcription se fait sur CPU (plus lent).
- `requirements.txt` ne contient pas ffmpeg : c'est un binaire système, pas un paquet pip.
