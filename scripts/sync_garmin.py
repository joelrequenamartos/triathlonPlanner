"""Sincroniza las actividades de Garmin Connect con la tabla garmin_activities de Supabase.

Pensado para correr desde GitHub Actions (o cualquier cron), leyendo credenciales
y config exclusivamente de variables de entorno. No guarda ni imprime secretos.

Variables de entorno requeridas:
    GARMIN_EMAIL           email de la cuenta Garmin Connect
    GARMIN_PASSWORD        contraseña de la cuenta Garmin Connect
    SUPABASE_URL            https://<proyecto>.supabase.co
    SUPABASE_SERVICE_KEY    service role key de Supabase (NO la anon key)
    SUPABASE_USER_ID        UUID del usuario (auth.users.id) al que asociar las filas

Variables opcionales:
    GARMIN_SYNC_LIMIT       nº máx. de actividades a traer por ejecución (default 50)
"""

import os
import sys

import requests
from garminconnect import Garmin


def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        print(f"Falta la variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return value


def to_row(user_id, activity):
    return {
        "user_id": user_id,
        "activity_id": activity["activityId"],
        "name": activity.get("activityName"),
        "type": (activity.get("activityType") or {}).get("typeKey"),
        "event_type": (activity.get("eventType") or {}).get("typeKey"),
        "start_time": activity.get("startTimeGMT"),
        "distance_meters": activity.get("distance"),
        "duration_seconds": activity.get("duration"),
        "moving_duration_seconds": activity.get("movingDuration"),
        "calories": activity.get("calories"),
        "avg_hr_bpm": activity.get("averageHR"),
        "max_hr_bpm": activity.get("maxHR"),
        "elevation_gain_meters": activity.get("elevationGain"),
        "elevation_loss_meters": activity.get("elevationLoss"),
        "steps": activity.get("steps"),
    }


def upsert_rows(supabase_url, service_key, rows):
    if not rows:
        print("Sin actividades nuevas que sincronizar.")
        return

    resp = requests.post(
        f"{supabase_url}/rest/v1/garmin_activities",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        params={"on_conflict": "user_id,activity_id"},
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Sincronizadas {len(rows)} actividades.")


def main():
    garmin_email = env("GARMIN_EMAIL")
    garmin_password = env("GARMIN_PASSWORD")
    supabase_url = env("SUPABASE_URL").rstrip("/")
    service_key = env("SUPABASE_SERVICE_KEY")
    user_id = env("SUPABASE_USER_ID")
    limit = int(env("GARMIN_SYNC_LIMIT", required=False, default="50"))

    client = Garmin(garmin_email, garmin_password)
    client.login()

    activities = client.get_activities(0, limit)
    rows = [to_row(user_id, a) for a in activities]

    upsert_rows(supabase_url, service_key, rows)


if __name__ == "__main__":
    main()
