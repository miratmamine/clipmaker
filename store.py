"""File d'attente et historique des traitements (PostgreSQL / Supabase).

Le mode local n'utilise pas ce module : l'interface web garde les jobs en
mémoire. En mode cloud, ``webapp.py`` écrit les jobs dans la table ``jobs`` et
``worker.py`` les réclame avec ``FOR UPDATE SKIP LOCKED``.

La connexion passe par le pooler Supabase (port 5432 ou 6543). Les requêtes
préparées sont désactivées (``prepare_threshold=None``) pour rester compatible
avec le mode transaction du pooler.
"""

from __future__ import annotations

import os
import re
import time

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOG_LIMIT = 60_000
CONNECT_ATTEMPTS = 3


class StoreError(Exception):
    """Erreur de communication avec la base de données."""


def configured() -> bool:
    """Indique si une base de données est configurée."""
    return bool(DATABASE_URL or os.getenv("DATABASE_URL", "").strip())


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip() or DATABASE_URL
    if not url:
        raise StoreError(
            "DATABASE_URL est vide : impossible de joindre la base de données."
        )
    # Erreur de copier-coller fréquente : « :6543//postgres » (double slash).
    url = re.sub(r"(:\d+)/{2,}", r"\1/", url)
    # Le pooler Supabase exige TLS ; on l'ajoute si l'utilisateur l'a oublié.
    if "supabase." in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _connect():
    """Ouvre une connexion court terme avec quelques tentatives."""
    last_error: Exception | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return psycopg.connect(
                _database_url(),
                row_factory=dict_row,
                connect_timeout=15,
                prepare_threshold=None,
                application_name="clipmaker",
            )
        except psycopg.Error as exc:
            last_error = exc
            if attempt + 1 < CONNECT_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
    raise StoreError(
        "Connexion à la base de données impossible. Vérifiez DATABASE_URL "
        f"et que le projet Supabase n'est pas en pause. ({last_error})"
    )


def _query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with _connect() as conn:
            cursor = conn.execute(sql, params)
            if cursor.description is None:
                return []
            return cursor.fetchall()
    except psycopg.Error as exc:
        raise StoreError(f"Requête base de données échouée : {exc}") from exc


def ping() -> bool:
    """Teste la connexion (utilisé par ``/healthz``)."""
    try:
        rows = _query("select 1 as ok")
        return bool(rows and rows[0].get("ok") == 1)
    except StoreError:
        return False


def create_job(url: str, clips: int) -> dict:
    rows = _query(
        """
        insert into jobs (url, clips)
        values (%s, %s)
        returning id::text as id, url, clips, status, created_at
        """,
        (url, clips),
    )
    return rows[0]


def get_job(job_id: str) -> dict | None:
    rows = _query(
        """
        select id::text as id, url, clips, status, error, exit_code, log,
               created_at, started_at, finished_at
        from jobs
        where id::text = %s
        """,
        (str(job_id),),
    )
    return rows[0] if rows else None


def list_jobs(limit: int = 10) -> list[dict]:
    return _query(
        """
        select j.id::text as id, j.url, j.clips, j.status, j.error,
               j.created_at, j.started_at, j.finished_at,
               (select count(*) from clips c where c.job_id = j.id) as clip_count
        from jobs j
        order by j.created_at desc
        limit %s
        """,
        (max(1, min(int(limit), 50)),),
    )


def active_count() -> int:
    rows = _query(
        "select count(*) as total from jobs where status in ('queued', 'running')"
    )
    return int(rows[0]["total"]) if rows else 0


def active_source_keys() -> list[str]:
    """Clés Storage des vidéos encore utiles (jobs en attente ou en cours)."""
    rows = _query(
        "select url from jobs where status in ('queued', 'running') "
        "and url like %s",
        ("upload://%",),
    )
    return [
        str(row.get("url") or "")[len("upload://") :].strip()
        for row in rows
        if str(row.get("url") or "").strip()
    ]


def claim_job() -> dict | None:
    """Réclame le plus ancien job en attente, atomiquement."""
    rows = _query(
        """
        update jobs
        set status = 'running',
            started_at = now(),
            finished_at = null,
            heartbeat_at = now()
        where id = (
            select id from jobs
            where status = 'queued'
            order by created_at
            limit 1
            for update skip locked
        )
        returning id::text as id, url, clips, created_at
        """
    )
    return rows[0] if rows else None


def touch(job_id: str) -> None:
    _query(
        "update jobs set heartbeat_at = now() where id::text = %s",
        (str(job_id),),
    )


def append_log(job_id: str, text: str) -> None:
    """Ajoute du texte au journal du job (limité à 60 000 caractères)."""
    if not text:
        return
    payload = text.rstrip("\r\n") + "\n"
    _query(
        """
        update jobs
        set log = right(coalesce(log, '') || %s, %s),
            heartbeat_at = now()
        where id::text = %s
        """,
        (payload, LOG_LIMIT, str(job_id)),
    )


def finish_job(
    job_id: str, status: str, error: str = "", exit_code: int | None = None
) -> None:
    _query(
        """
        update jobs
        set status = %s, error = %s, exit_code = %s,
            finished_at = now(), heartbeat_at = now()
        where id::text = %s
        """,
        (status, (error or "")[:2000], exit_code, str(job_id)),
    )


def requeue_job(job_id: str, note: str = "") -> None:
    """Remet un job en file (arrêt du service, job bloqué…)."""
    _query(
        """
        update jobs
        set status = 'queued',
            error = '',
            exit_code = null,
            started_at = null,
            finished_at = null,
            heartbeat_at = null,
            log = right(
                coalesce(log, '') || %s,
                %s
            )
        where id::text = %s
        """,
        (
            (note.rstrip() + "\n") if note else "",
            LOG_LIMIT,
            str(job_id),
        ),
    )


def requeue_stale(minutes: int = 15) -> int:
    """Remet en file les jobs ``running`` dont le worker a disparu."""
    rows = _query(
        """
        update jobs
        set status = 'queued',
            started_at = null,
            finished_at = null,
            heartbeat_at = null,
            log = right(
                coalesce(log, '') ||
                'Job interrompu (worker arrêté) : remis en file automatiquement.' || chr(10),
                %s
            )
        where status = 'running'
          and coalesce(heartbeat_at, started_at, now())
              < now() - make_interval(mins => %s)
        returning id::text as id
        """,
        (LOG_LIMIT, max(1, int(minutes))),
    )
    return len(rows)


def add_clip(
    job_id: str,
    index: int,
    title: str,
    score: object,
    filename: str,
    storage_key: str,
    size_bytes: int,
) -> dict:
    """Enregistre (ou met à jour) un clip produit pour un job."""
    try:
        numeric_score = float(score) if score not in (None, "?") else None
    except (TypeError, ValueError):
        numeric_score = None

    rows = _query(
        """
        insert into clips
            (job_id, idx, title, score, filename, storage_key, size_bytes)
        values (%s::uuid, %s, %s, %s, %s, %s, %s)
        on conflict (job_id, idx) do update
        set title = excluded.title,
            score = excluded.score,
            filename = excluded.filename,
            storage_key = excluded.storage_key,
            size_bytes = excluded.size_bytes
        returning id::text as id, job_id::text as job_id, idx, title, score,
                  filename, storage_key, size_bytes, created_at
        """,
        (
            str(job_id),
            int(index),
            title[:200],
            numeric_score,
            filename[:200],
            storage_key,
            max(0, int(size_bytes)),
        ),
    )
    return rows[0]


def list_clips(job_id: str) -> list[dict]:
    return _query(
        """
        select id::text as id, job_id::text as job_id, idx, title, score,
               filename, storage_key, size_bytes, created_at
        from clips
        where job_id::text = %s
        order by idx
        """,
        (str(job_id),),
    )


def get_clip(clip_id: str) -> dict | None:
    rows = _query(
        """
        select id::text as id, job_id::text as job_id, idx, title, score,
               filename, storage_key, size_bytes, created_at
        from clips
        where id::text = %s
        """,
        (str(clip_id),),
    )
    return rows[0] if rows else None


def list_stored_clips() -> list[dict]:
    """Clips stockés, du plus ancien au plus récent (pour la purge)."""
    return _query(
        """
        select id::text as id, job_id::text as job_id, filename, storage_key,
               size_bytes, created_at
        from clips
        where storage_key is not null and storage_key <> ''
        order by created_at asc
        """
    )


def delete_clip(clip_id: str) -> None:
    _query("delete from clips where id::text = %s", (str(clip_id),))


def purge_jobs(days: int = 60) -> int:
    """Supprime les vieux jobs terminés (les clips doivent être purgés avant)."""
    rows = _query(
        """
        delete from jobs
        where status in ('done', 'error')
          and created_at < now() - make_interval(days => %s)
        returning id
        """,
        (max(1, int(days)),),
    )
    return len(rows)
