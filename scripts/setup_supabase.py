"""Configure automatiquement un projet Supabase pour ClipMaker.

Ce script remplace les manipulations manuelles du tableau de bord Supabase :

1. exécute ``supabase/schema.sql`` (tables ``jobs`` et ``clips``, index, RLS) ;
2. crée le bucket privé ``clips`` s'il n'existe pas ;
3. vérifie que les tables, la RLS et le bucket sont bien en place ;
4. fait un aller-retour réel : upload d'un petit fichier, signature d'URL,
   suppression — exactement le chemin utilisé par le worker.

Utilisation :
    python scripts/setup_supabase.py

Il lit ``.env.cloud`` (ou les variables d'environnement). Seules trois valeurs
sont nécessaires :

- ``DATABASE_URL``          (Supabase → Database → Connection string → URI pooler)
- ``SUPABASE_URL``          (Supabase → API → Project URL)
- ``SUPABASE_SERVICE_KEY``  (Supabase → API → clé service_role)

Le script est idempotent : on peut le relancer sans risque.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT / ".env.cloud", override=False)
load_dotenv(PROJECT / ".env", override=False)

import httpx  # noqa: E402
import psycopg  # noqa: E402

import storage  # noqa: E402
import store  # noqa: E402

OK = "[ OK ]"
WARN = "[ !! ]"
FAIL = "[FAIL]"


def step(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 46 - len(title)))


def _database_host() -> str:
    try:
        return urlsplit(store._database_url()).hostname or ""
    except Exception:  # noqa: BLE001 - diagnostic seulement
        return ""


def check_config() -> bool:
    step("Configuration")
    missing = []
    for name, value in (
        ("DATABASE_URL", store._database_url if store.configured() else ""),
        ("SUPABASE_URL", os.getenv("SUPABASE_URL", "").strip()),
        ("SUPABASE_SERVICE_KEY", "***" if storage.configured() else ""),
    ):
        if not value:
            missing.append(name)
            print(f"{FAIL} {name} manquant")
        else:
            print(f"{OK} {name} renseigné")

    host = _database_host()
    if host:
        print(f"       hôte base : {host}")
        if host.startswith("db.") and host.endswith(".supabase.co"):
            print(
                f"{WARN} connexion directe détectée : Supabase la limite à IPv6. "
                "Préfère l'URI du pooler (…pooler.supabase.com:6543)."
            )
        elif "pooler.supabase.com" in host:
            print(f"{OK} pooler Supabase détecté")

    return not missing


def apply_schema() -> bool:
    step("Schéma (tables jobs + clips)")
    schema_path = PROJECT / "supabase" / "schema.sql"
    if not schema_path.is_file():
        print(f"{FAIL} fichier introuvable : {schema_path}")
        return False

    sql = schema_path.read_text(encoding="utf-8")
    try:
        with psycopg.connect(
            store._database_url(),
            connect_timeout=20,
            autocommit=True,
            prepare_threshold=None,
            application_name="clipmaker-setup",
        ) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        print(f"{FAIL} exécution du schéma impossible : {exc}")
        return False

    print(f"{OK} schema.sql appliqué")
    return True


def verify_schema() -> bool:
    step("Vérification des tables et de la RLS")
    ok = True
    try:
        with psycopg.connect(
            store._database_url(),
            connect_timeout=20,
            prepare_threshold=None,
            application_name="clipmaker-setup",
        ) as conn:
            rows = conn.execute(
                """
                select c.relname, c.relrowsecurity
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'public' and c.relname in ('jobs', 'clips')
                order by c.relname
                """
            ).fetchall()
    except psycopg.Error as exc:
        print(f"{FAIL} lecture du catalogue impossible : {exc}")
        return False

    found = {row[0]: bool(row[1]) for row in rows}
    for table in ("clips", "jobs"):
        if table not in found:
            print(f"{FAIL} table public.{table} absente")
            ok = False
        elif not found[table]:
            print(f"{WARN} table public.{table} sans RLS")
            ok = False
        else:
            print(f"{OK} table public.{table} (RLS activée)")
    return ok


def _bucket_url() -> str:
    return f"{os.getenv('SUPABASE_URL', '').strip().rstrip('/')}/storage/v1/bucket"


def ensure_bucket() -> bool:
    step(f"Bucket privé « {storage.bucket()} »")
    name = storage.bucket()
    headers = {
        "apikey": os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        or os.getenv("SUPABASE_KEY", "").strip(),
        "Content-Type": "application/json",
    }
    headers["Authorization"] = f"Bearer {headers['apikey']}"

    try:
        listed = httpx.get(_bucket_url(), headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        print(f"{FAIL} Storage injoignable : {exc}")
        return False

    if listed.status_code != 200:
        print(f"{FAIL} liste des buckets refusée (HTTP {listed.status_code})")
        return False

    buckets = {item.get("name"): item for item in listed.json() or []}
    if name in buckets:
        public = bool(buckets[name].get("public"))
        if public:
            print(f"{WARN} le bucket « {name} » est PUBLIC")
            return False
        print(f"{OK} bucket « {name} » déjà présent (privé)")
        return True

    try:
        created = httpx.post(
            _bucket_url(),
            headers=headers,
            json={
                "id": name,
                "name": name,
                "public": False,
                "file_size_limit": 52428800,  # 50 Mo, limite du plan gratuit
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"{FAIL} création du bucket impossible : {exc}")
        return False

    if created.status_code in (200, 201):
        print(f"{OK} bucket « {name} » créé (privé, 50 Mo max/fichier)")
        return True

    print(f"{FAIL} création refusée (HTTP {created.status_code}) : {created.text[:200]}")
    return False


def smoke_test() -> bool:
    step("Test réel : upload, URL signée, suppression")
    key = "_clipmaker/setup-check.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "setup-check.mp4"
        sample.write_bytes(b"clipmaker-setup-check")

        try:
            storage.upload_file(sample, key)
        except storage.StorageError as exc:
            print(f"{FAIL} upload : {exc}")
            return False
        print(f"{OK} upload réussi ({key})")

        try:
            url = storage.signed_url(key, expires=120)
        except storage.StorageError as exc:
            print(f"{FAIL} signature : {exc}")
            storage.delete_file(key)
            return False
        print(f"{OK} URL signée générée")

        try:
            head = httpx.get(url, timeout=30.0)
        except httpx.HTTPError as exc:
            print(f"{WARN} téléchargement de contrôle impossible : {exc}")
        else:
            if head.status_code == 200 and head.content == b"clipmaker-setup-check":
                print(f"{OK} fichier relu via l'URL signée")
            else:
                print(f"{WARN} relecture inattendue (HTTP {head.status_code})")

        try:
            storage.delete_file(key)
            print(f"{OK} suppression effectuée")
        except storage.StorageError as exc:
            print(f"{WARN} suppression impossible : {exc}")

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure Supabase pour ClipMaker (schéma + bucket + test)."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="ne modifie rien, vérifie seulement l'existant",
    )
    args = parser.parse_args(argv)

    print("Configuration Supabase de ClipMaker")

    if not check_config():
        print("\nRenseigne les valeurs manquantes dans .env.cloud puis relance.")
        return 1

    if not store.ping():
        print(f"{FAIL} connexion PostgreSQL impossible (DATABASE_URL / mot de passe ?)")
        return 1
    print(f"{OK} connexion PostgreSQL établie")

    if args.check:
        ok = verify_schema() and ensure_bucket()
    else:
        ok = apply_schema() and verify_schema() and ensure_bucket()
        if ok:
            ok = smoke_test()

    print()
    if ok:
        print("Supabase est prêt pour ClipMaker.")
        return 0

    print("Des points restent à corriger (voir les lignes [ !! ] / [FAIL] ci-dessus).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
