-- Ejecuta esto en el SQL Editor de Supabase (además de los .sql anteriores).
-- Es idempotente: se puede ejecutar varias veces sin romper nada.

-- 1) Métricas extra por actividad (zonas de FC, cadencia, potencia, natación, carga...)
alter table garmin_activities add column if not exists metrics jsonb;

-- 2) Métricas diarias de bienestar (vale tanto si ya creaste la tabla antes como si no)
create table if not exists garmin_daily_metrics (
  user_id uuid not null references auth.users(id) on delete cascade,
  date date not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, date)
);

alter table garmin_daily_metrics add column if not exists resting_hr integer;
alter table garmin_daily_metrics add column if not exists resting_hr_7d integer;
alter table garmin_daily_metrics add column if not exists steps integer;
alter table garmin_daily_metrics add column if not exists stress_avg integer;
alter table garmin_daily_metrics add column if not exists stress_max integer;
alter table garmin_daily_metrics add column if not exists body_battery_high integer;
alter table garmin_daily_metrics add column if not exists body_battery_low integer;
alter table garmin_daily_metrics add column if not exists body_battery_charged integer;
alter table garmin_daily_metrics add column if not exists body_battery_drained integer;
alter table garmin_daily_metrics add column if not exists intensity_moderate integer;
alter table garmin_daily_metrics add column if not exists intensity_vigorous integer;
alter table garmin_daily_metrics add column if not exists active_calories integer;
alter table garmin_daily_metrics add column if not exists respiration_avg numeric;
alter table garmin_daily_metrics add column if not exists sleep_seconds integer;
alter table garmin_daily_metrics add column if not exists deep_sleep_seconds integer;
alter table garmin_daily_metrics add column if not exists light_sleep_seconds integer;
alter table garmin_daily_metrics add column if not exists rem_sleep_seconds integer;
alter table garmin_daily_metrics add column if not exists awake_sleep_seconds integer;
alter table garmin_daily_metrics add column if not exists sleep_score integer;
alter table garmin_daily_metrics add column if not exists hrv_avg numeric;
alter table garmin_daily_metrics add column if not exists vo2max numeric;
alter table garmin_daily_metrics add column if not exists vo2max_cycling numeric;

alter table garmin_daily_metrics enable row level security;

drop policy if exists "select own garmin_daily_metrics" on garmin_daily_metrics;
create policy "select own garmin_daily_metrics"
  on garmin_daily_metrics for select
  using (auth.uid() = user_id);
