"""Sincroniza actividades y métricas diarias de Garmin Connect con Supabase.

Pensado para correr desde GitHub Actions (o cualquier cron), leyendo credenciales
y config exclusivamente de variables de entorno. No guarda ni imprime secretos.

Tablas que rellena:
    garmin_activities       una fila por actividad (distancia, duración, FC, traza GPS...)
    garmin_daily_metrics    una fila por día (sueño, FC reposo, body battery, VO2max...)

Variables de entorno requeridas:
    GARMIN_EMAIL            email de la cuenta Garmin Connect
    GARMIN_PASSWORD         contraseña de la cuenta Garmin Connect
    SUPABASE_URL            https://<proyecto>.supabase.co
    SUPABASE_SERVICE_KEY    service role key de Supabase (NO la anon key)
    SUPABASE_USER_ID        UUID del usuario (auth.users.id) al que asociar las filas

Variables opcionales:
    GARMIN_SYNC_LIMIT       nº máx. de actividades a traer por ejecución (default 50)
    GARMIN_WELLNESS_DAYS    nº de días hacia atrás para los que traer bienestar (default 3)
"""

import datetime as dt
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


def safe_call(fn, *args, label=None, **kwargs):
    """Llama a un método del cliente de Garmin sin romper el resto del sync si falla."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - un fallo puntual no debe tumbar el resto
        print(f"  aviso: fallo en {label or fn.__name__}{args}: {exc}", file=sys.stderr)
        return None


MAX_POLYLINE_POINTS = 150


def fetch_polyline(client, activity_id):
    """Trae la traza GPS de una actividad y la reduce a un puñado de puntos [lat, lon].

    Devuelve None si la actividad no tiene GPS (indoor, piscina, etc.) o si Garmin
    no puede darnos el detalle por cualquier motivo — nunca rompe el resto del sync.
    """
    details = safe_call(
        client.get_activity_details, activity_id, maxpoly=MAX_POLYLINE_POINTS,
        label=f"get_activity_details({activity_id})"
    )
    if details is None:
        return None

    raw_points = (details.get("geoPolylineDTO") or {}).get("polyline") or []
    points = [
        [p["lat"], p["lon"]]
        for p in raw_points
        if p.get("lat") is not None and p.get("lon") is not None
    ]
    if len(points) < 2:
        return None

    if len(points) > MAX_POLYLINE_POINTS:
        stride = len(points) / MAX_POLYLINE_POINTS
        points = [points[int(i * stride)] for i in range(MAX_POLYLINE_POINTS)]

    return points


def to_activity_row(user_id, activity, polyline):
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
        "polyline": polyline,
    }


def fetch_daily_metrics_row(client, user_id, date_str):
    """Recopila sueño, FC reposo, body battery, HRV, VO2max y estado de entreno de un día.

    Cada métrica se pide por separado y nunca rompe las demás si falla o no existe
    para ese día (algunas, como HRV, dependen del modelo de reloj).
    """
    sleep = safe_call(client.get_sleep_data, date_str, label="get_sleep_data")
    rhr = safe_call(client.get_rhr_day, date_str, label="get_rhr_day")
    battery = safe_call(client.get_body_battery, date_str, date_str, label="get_body_battery")
    hrv = safe_call(client.get_hrv_data, date_str, label="get_hrv_data")
    max_metrics = safe_call(client.get_max_metrics, date_str, label="get_max_metrics")
    training_status = safe_call(client.get_training_status, date_str, label="get_training_status")

    daily_sleep = (sleep or {}).get("dailySleepDTO") or {}

    resting_hr = None
    try:
        rhr_points = (((rhr or {}).get("allMetrics") or {}).get("metricsMap") or {}).get(
            "WELLNESS_RESTING_HEART_RATE"
        )
        if rhr_points:
            resting_hr = round(rhr_points[0]["value"])
    except Exception:  # noqa: BLE001
        resting_hr = None

    battery_entry = (battery or [{}])[0] if battery else {}

    hrv_avg = None
    try:
        hrv_avg = ((hrv or {}).get("hrvSummary") or {}).get("lastNightAvg")
    except Exception:  # noqa: BLE001
        hrv_avg = None

    vo2max = None
    try:
        generic = (max_metrics or {}).get("generic") or {}
        vo2max = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
    except Exception:  # noqa: BLE001
        vo2max = None

    return {
        "user_id": user_id,
        "date": date_str,
        "resting_hr": resting_hr,
        "sleep_seconds": daily_sleep.get("sleepTimeSeconds"),
        "deep_sleep_seconds": daily_sleep.get("deepSleepSeconds"),
        "light_sleep_seconds": daily_sleep.get("lightSleepSeconds"),
        "rem_sleep_seconds": daily_sleep.get("remSleepSeconds"),
        "awake_sleep_seconds": daily_sleep.get("awakeSleepSeconds"),
        "body_battery_charged": battery_entry.get("charged"),
        "body_battery_drained": battery_entry.get("drained"),
        "hrv_avg": hrv_avg,
        "vo2max": vo2max,
        "training_status": training_status,
    }


def upsert_rows(supabase_url, service_key, table, on_conflict, rows):
    if not rows:
        print(f"Sin filas nuevas para {table}.")
        return

    resp = requests.post(
        f"{supabase_url}/rest/v1/{table}",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Sincronizadas {len(rows)} filas en {table}.")


def main():
    garmin_email = env("GARMIN_EMAIL")
    garmin_password = env("GARMIN_PASSWORD")
    supabase_url = env("SUPABASE_URL").rstrip("/")
    service_key = env("SUPABASE_SERVICE_KEY")
    user_id = env("SUPABASE_USER_ID")
    limit = int(env("GARMIN_SYNC_LIMIT", required=False, default="50"))
    wellness_days = int(env("GARMIN_WELLNESS_DAYS", required=False, default="3"))

    client = Garmin(garmin_email, garmin_password)
    client.login()

    activities = client.get_activities(0, limit)
    activity_rows = [
        to_activity_row(user_id, a, fetch_polyline(client, a["activityId"]))
        for a in activities
    ]
    upsert_rows(supabase_url, service_key, "garmin_activities", "user_id,activity_id", activity_rows)

    today = dt.date.today()
    daily_rows = []
    for i in range(wellness_days):
        date_str = (today - dt.timedelta(days=i)).isoformat()
        print(f"Bienestar del {date_str}...")
        daily_rows.append(fetch_daily_metrics_row(client, user_id, date_str))
    upsert_rows(supabase_url, service_key, "garmin_daily_metrics", "user_id,date", daily_rows)


if __name__ == "__main__":
    main()
