-- Ejecuta esto en el SQL Editor de Supabase (además del garmin_activities.sql original).
-- Añade la columna donde guardamos la traza GPS simplificada de cada actividad.

alter table public.garmin_activities
    add column if not exists polyline jsonb;
