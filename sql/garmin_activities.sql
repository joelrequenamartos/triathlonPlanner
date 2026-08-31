-- Ejecuta esto en el SQL Editor de tu proyecto Supabase (Project > SQL Editor > New query).

create table if not exists public.garmin_activities (
    user_id uuid not null references auth.users(id) on delete cascade,
    activity_id bigint not null,
    name text,
    type text,
    event_type text,
    start_time timestamptz not null,
    distance_meters double precision,
    duration_seconds double precision,
    moving_duration_seconds double precision,
    calories double precision,
    avg_hr_bpm double precision,
    max_hr_bpm double precision,
    elevation_gain_meters double precision,
    elevation_loss_meters double precision,
    steps integer,
    synced_at timestamptz not null default now(),
    primary key (user_id, activity_id)
);

create index if not exists garmin_activities_user_start_idx
    on public.garmin_activities (user_id, start_time desc);

alter table public.garmin_activities enable row level security;

-- El frontend (con el usuario logueado) solo puede LEER sus propias actividades.
-- El script de sync escribe con la service role key, que salta RLS por completo,
-- así que no hace falta (ni conviene) una policy de insert/update para el rol anon/authenticated.
create policy "Users can read own garmin activities"
    on public.garmin_activities
    for select
    using (auth.uid() = user_id);
