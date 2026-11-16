-- Schéma ClipMaker pour Supabase (ou tout PostgreSQL 14+).
-- À exécuter une fois dans Supabase : SQL Editor → New query → Run.

create table if not exists public.jobs (
    id uuid primary key default gen_random_uuid(),
    url text not null,
    clips integer not null default 5 check (clips between 1 and 10),
    status text not null default 'queued'
        check (status in ('queued', 'running', 'done', 'error')),
    error text not null default '',
    exit_code integer,
    log text not null default '',
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    heartbeat_at timestamptz
);

create index if not exists jobs_status_created_idx
    on public.jobs (status, created_at);

create index if not exists jobs_created_idx
    on public.jobs (created_at desc);

create table if not exists public.clips (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    idx integer not null,
    title text not null default '',
    score double precision,
    filename text not null default '',
    storage_key text not null default '',
    size_bytes bigint not null default 0,
    created_at timestamptz not null default now(),
    unique (job_id, idx)
);

create index if not exists clips_job_idx
    on public.clips (job_id, idx);

create index if not exists clips_created_idx
    on public.clips (created_at);

-- L'application passe par la clé service_role (côté serveur uniquement) ou par
-- la connexion PostgreSQL directe : aucune policy publique n'est nécessaire.
alter table public.jobs enable row level security;
alter table public.clips enable row level security;

-- Le bucket Storage « clips » doit être créé manuellement (privé) :
--   Supabase → Storage → New bucket → name: clips → Public: NON.
-- La purge (âge + quota) est faite par worker.py.
