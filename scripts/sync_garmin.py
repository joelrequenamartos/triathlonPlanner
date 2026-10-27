"""Sincroniza actividades y métricas diarias de Garmin Connect con Supabase.

Pensado para correr desde GitHub Actions (o cualquier cron), leyendo credenciales
y config exclusivamente de variables de entorno. No guarda ni imprime secretos.

Tablas que rellena:
    garmin_activities       una fila por actividad: datos básicos, traza GPS y una columna
                            `metrics` (jsonb) con métricas extra (zonas FC, cadencia,
                            potencia, natación, carga TRIMP...)
    garmin_daily_metrics    una fila por día: FC reposo, estrés, body battery, pasos,
                            minutos de intensidad, respiración, sueño, HRV y VO2max (este último
                            con historial de 180 días, para medir la progresión)

El sync es incremental para no saturar la API de Garmin:
    - solo pide el detalle (traza GPS, zonas de FC) de actividades nuevas o que aún no
      tengan zonas, con un tope de llamadas por ejecución;
    - las métricas diarias solo se piden desde el último día guardado (refrescando los
      2 últimos, que pueden estar incompletos), con un backfill inicial acotado.

Variables de entorno requeridas:
    GARMIN_EMAIL            email de la cuenta Garmin Connect
    GARMIN_PASSWORD         contraseña de la cuenta Garmin Connect
    SUPABASE_URL            https://<proyecto>.supabase.co
    SUPABASE_SERVICE_KEY    service role key de Supabase (NO la anon key)
    SUPABASE_USER_ID        UUID del usuario (auth.users.id) al que asociar las filas

Variables opcionales:
    GARMIN_SYNC_LIMIT           nº de actividades recientes a revisar (default 50)
    GARMIN_DETAIL_BUDGET        máx. de actividades con detalle extra por ejecución (default 20)
    GARMIN_WELLNESS_BACKFILL    días hacia atrás en el primer sync de bienestar (default 30)
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
    """Llama al cliente de Garmin sin que un fallo puntual tumbe el resto del sync."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"  aviso: fallo en {label or fn.__name__}: {exc}", file=sys.stderr)
        return None


def compact(d):
    """Quita claves con valor None para no guardar ruido en jsonb."""
    return {k: v for k, v in d.items() if v is not None}


def non_negative(value):
    """Garmin usa -1/-2 para "sin datos" en algunas métricas (p. ej. estrés)."""
    return value if value is not None and value >= 0 else None


# --- Supabase (REST) ---------------------------------------------------------------


class Supabase:
    def __init__(self, url, service_key):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def get(self, table, params):
        resp = requests.get(f"{self.url}/rest/v1/{table}", headers=self.headers, params=params, timeout=30)
        return resp

    def upsert(self, table, on_conflict, rows):
        if not rows:
            print(f"Sin filas para {table}.")
            return
        resp = requests.post(
            f"{self.url}/rest/v1/{table}",
            headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
            params={"on_conflict": on_conflict},
            json=rows,
            timeout=60,
        )
        if not resp.ok:
            print(f"Error al guardar en {table}: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()
        print(f"Guardadas {len(rows)} filas en {table}.")


# --- Actividades -------------------------------------------------------------------


MAX_POLYLINE_POINTS = 150

# Campos opcionales del resumen de cada actividad que Garmin ya incluye en el listado
# (no cuestan llamadas extra). Solo se guardan los que el reloj realmente rellena.
SUMMARY_METRIC_KEYS = {
    "avg_speed_mps": "averageSpeed",
    "max_speed_mps": "maxSpeed",
    "min_hr_bpm": "minHR",
    "max_elevation_meters": "maxElevation",
    "min_elevation_meters": "minElevation",
    "lap_count": "lapCount",
    "intensity_moderate_min": "moderateIntensityMinutes",
    "intensity_vigorous_min": "vigorousIntensityMinutes",
    "run_cadence_avg": "averageRunningCadenceInStepsPerMinute",
    "run_cadence_max": "maxRunningCadenceInStepsPerMinute",
    "bike_cadence_avg": "averageBikingCadenceInRevPerMinute",
    "bike_cadence_max": "maxBikingCadenceInRevPerMinute",
    "power_avg": "avgPower",
    "power_max": "maxPower",
    "power_normalized": "normPower",
    "swim_swolf_avg": "averageSwolf",
    "swim_stroke_rate_avg": "averageSwimCadenceInStrokesPerMinute",
    "swim_strokes_total": "strokes",
    "swim_strokes_per_length": "avgStrokes",
    "swim_pool_length_m": "poolLength",
    "swim_active_lengths": "activeLengths",
    "vo2max": "vO2MaxValue",
    "aerobic_te": "aerobicTrainingEffect",
    "anaerobic_te": "anaerobicTrainingEffect",
    "training_load": "activityTrainingLoad",
    "respiration_avg": "avgRespirationRate",
    "temperature_min": "minTemperature",
    "temperature_max": "maxTemperature",
    "body_battery_diff": "differenceBodyBattery",
}


def extract_summary_metrics(activity):
    metrics = compact({key: activity.get(src) for key, src in SUMMARY_METRIC_KEYS.items()})

    zones = [activity.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    if any(z is not None for z in zones):
        metrics["hr_zones"] = [round(z or 0) for z in zones]
    return metrics


def trimp(zone_seconds):
    """Carga de entreno tipo TRIMP de Edwards: minutos en cada zona × nº de zona."""
    return round(sum((secs / 60) * (i + 1) for i, secs in enumerate(zone_seconds)))


def fetch_hr_zones(client, activity_id):
    raw = safe_call(client.get_activity_hr_in_timezones, activity_id, label=f"zonas FC {activity_id}")
    if not isinstance(raw, list) or not raw:
        return None, None
    by_zone = sorted(raw, key=lambda z: z.get("zoneNumber", 0))
    seconds = [round(z.get("secsInZone") or 0) for z in by_zone][:5]
    bounds = [z.get("zoneLowBoundary") for z in by_zone][:5]
    if not any(seconds):
        return None, None
    return seconds, bounds


def fetch_polyline(client, activity_id):
    """Traza GPS reducida a ~150 puntos [lat, lon], o None si no hay GPS."""
    details = safe_call(
        client.get_activity_details, activity_id, maxpoly=MAX_POLYLINE_POINTS,
        label=f"traza {activity_id}",
    )
    if not details:
        return None

    raw_points = (details.get("geoPolylineDTO") or {}).get("polyline") or []
    points = [[p["lat"], p["lon"]] for p in raw_points if p.get("lat") is not None and p.get("lon") is not None]
    if len(points) < 2:
        return None
    if len(points) > MAX_POLYLINE_POINTS:
        stride = len(points) / MAX_POLYLINE_POINTS
        points = [points[int(i * stride)] for i in range(MAX_POLYLINE_POINTS)]
    return points


def base_activity_row(user_id, activity):
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


def load_existing_activities(sb, user_id, activity_ids):
    """Devuelve ({activity_id: metrics}, hay_columna_metrics) de las actividades ya guardadas."""
    if not activity_ids:
        return {}, True
    ids = ",".join(str(i) for i in activity_ids)
    resp = sb.get("garmin_activities", {
        "select": "activity_id,metrics",
        "user_id": f"eq.{user_id}",
        "activity_id": f"in.({ids})",
    })
    if resp.ok:
        return {row["activity_id"]: (row.get("metrics") or {}) for row in resp.json()}, True

    # Si aún no se ha ejecutado el SQL que añade la columna `metrics`, seguimos sin ella.
    print("  aviso: la columna garmin_activities.metrics no existe todavía; "
          "ejecuta el SQL de la v2 en Supabase para guardar métricas extra.", file=sys.stderr)
    resp = sb.get("garmin_activities", {
        "select": "activity_id",
        "user_id": f"eq.{user_id}",
        "activity_id": f"in.({ids})",
    })
    resp.raise_for_status()
    return {row["activity_id"]: {} for row in resp.json()}, False


def sync_activities(client, sb, user_id, limit, detail_budget):
    activities = client.get_activities(0, limit) or []
    existing, has_metrics_column = load_existing_activities(
        sb, user_id, [a["activityId"] for a in activities]
    )

    new_rows, existing_rows = [], []
    for activity in activities:
        activity_id = activity["activityId"]
        is_new = activity_id not in existing
        row = base_activity_row(user_id, activity)

        metrics = {**existing.get(activity_id, {}), **extract_summary_metrics(activity)}
        needs_zones = (
            "hr_zones" not in metrics
            and not metrics.get("hr_zones_unavailable")
            and activity.get("averageHR")
        )
        if needs_zones and detail_budget > 0:
            detail_budget -= 1
            seconds, bounds = fetch_hr_zones(client, activity_id)
            if seconds:
                metrics["hr_zones"] = seconds
                metrics["hr_zone_bounds"] = bounds
            else:
                # Para no volver a gastar una llamada en ella en cada ejecución.
                metrics["hr_zones_unavailable"] = True
        if metrics.get("hr_zones"):
            metrics["trimp"] = trimp(metrics["hr_zones"])

        if has_metrics_column:
            row["metrics"] = metrics or None

        if is_new:
            # La traza solo se pide una vez; en las ya guardadas no se reenvía y se conserva.
            row["polyline"] = fetch_polyline(client, activity_id)
            new_rows.append(row)
        else:
            existing_rows.append(row)

    # Dos tandas porque PostgREST exige las mismas columnas en todas las filas de un lote.
    sb.upsert("garmin_activities", "user_id,activity_id", new_rows)
    sb.upsert("garmin_activities", "user_id,activity_id", existing_rows)


# --- Bienestar diario --------------------------------------------------------------


def daily_metrics_row(client, user_id, date_str):
    stats = safe_call(client.get_stats, date_str, label=f"resumen diario {date_str}") or {}
    sleep = safe_call(client.get_sleep_data, date_str, label=f"sueño {date_str}") or {}
    hrv = safe_call(client.get_hrv_data, date_str, label=f"HRV {date_str}") or {}

    daily_sleep = sleep.get("dailySleepDTO") or {}
    sleep_score = (((daily_sleep.get("sleepScores") or {}).get("overall")) or {}).get("value")

    return {
        "user_id": user_id,
        "date": date_str,
        "resting_hr": stats.get("restingHeartRate"),
        "resting_hr_7d": stats.get("lastSevenDaysAvgRestingHeartRate"),
        "steps": stats.get("totalSteps"),
        "stress_avg": non_negative(stats.get("averageStressLevel")),
        "stress_max": non_negative(stats.get("maxStressLevel")),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_charged": stats.get("bodyBatteryChargedValue"),
        "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
        "intensity_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_vigorous": stats.get("vigorousIntensityMinutes"),
        "active_calories": round(stats["activeKilocalories"]) if stats.get("activeKilocalories") is not None else None,
        "respiration_avg": stats.get("avgWakingRespirationValue"),
        "sleep_seconds": daily_sleep.get("sleepTimeSeconds"),
        "deep_sleep_seconds": daily_sleep.get("deepSleepSeconds"),
        "light_sleep_seconds": daily_sleep.get("lightSleepSeconds"),
        "rem_sleep_seconds": daily_sleep.get("remSleepSeconds"),
        "awake_sleep_seconds": daily_sleep.get("awakeSleepSeconds"),
        "sleep_score": sleep_score,
        "hrv_avg": (hrv.get("hrvSummary") or {}).get("lastNightAvg"),
    }


VO2MAX_HISTORY_DAYS = 180
VO2MAX_CHUNK_DAYS = 90  # Garmin no garantiza rangos más largos en una sola petición


def sync_vo2max_history(client, sb, user_id):
    """Historial de VO2max (carrera y ciclismo) en pocas llamadas, para la progresión.

    Garmin solo guarda valor los días en que lo recalcula; se guardan esos días tal
    cual (la app arrastra el último valor conocido). Va en lotes separados del resto
    del bienestar para no pisar otras columnas.
    """
    today = dt.date.today()
    generic_rows, cycling_rows = {}, {}

    end = today
    remaining = VO2MAX_HISTORY_DAYS
    while remaining > 0:
        span = min(VO2MAX_CHUNK_DAYS, remaining)
        start = end - dt.timedelta(days=span - 1)
        items = safe_call(
            client.get_max_metrics_range, start.isoformat(), end.isoformat(),
            label=f"VO2max {start} → {end}",
        )
        for item in items if isinstance(items, list) else []:
            for sport, target, column in (
                ("generic", generic_rows, "vo2max"),
                ("cycling", cycling_rows, "vo2max_cycling"),
            ):
                entry = item.get(sport) or {}
                value = entry.get("vo2MaxPreciseValue") or entry.get("vo2MaxValue")
                day = entry.get("calendarDate")
                if value and day:
                    target[day] = {"user_id": user_id, "date": day, column: value}
        end = start - dt.timedelta(days=1)
        remaining -= span

    sb.upsert("garmin_daily_metrics", "user_id,date", list(generic_rows.values()))
    sb.upsert("garmin_daily_metrics", "user_id,date", list(cycling_rows.values()))


def last_synced_day(sb, user_id):
    resp = sb.get("garmin_daily_metrics", {
        "select": "date",
        "user_id": f"eq.{user_id}",
        "order": "date.desc",
        "limit": "1",
    })
    if not resp.ok:
        raise RuntimeError(
            f"No se pudo leer garmin_daily_metrics ({resp.status_code}). "
            "¿Has ejecutado el SQL de la v2 en Supabase?"
        )
    rows = resp.json()
    return dt.date.fromisoformat(rows[0]["date"]) if rows else None


def sync_daily_metrics(client, sb, user_id, backfill_days):
    today = dt.date.today()
    last = last_synced_day(sb, user_id)
    if last is None:
        start = today - dt.timedelta(days=backfill_days - 1)
    else:
        # Se refrescan los 2 últimos días guardados: el de hoy siempre está a medias.
        start = max(last - dt.timedelta(days=1), today - dt.timedelta(days=backfill_days - 1))

    rows = []
    day = start
    while day <= today:
        rows.append(daily_metrics_row(client, user_id, day.isoformat()))
        day += dt.timedelta(days=1)
    print(f"Bienestar: {len(rows)} días ({start} → {today}).")
    sb.upsert("garmin_daily_metrics", "user_id,date", rows)


# --- Main --------------------------------------------------------------------------


def main():
    garmin_email = env("GARMIN_EMAIL")
    garmin_password = env("GARMIN_PASSWORD")
    sb = Supabase(env("SUPABASE_URL"), env("SUPABASE_SERVICE_KEY"))
    user_id = env("SUPABASE_USER_ID")
    limit = int(env("GARMIN_SYNC_LIMIT", required=False, default="50"))
    detail_budget = int(env("GARMIN_DETAIL_BUDGET", required=False, default="20"))
    backfill_days = int(env("GARMIN_WELLNESS_BACKFILL", required=False, default="30"))

    client = Garmin(garmin_email, garmin_password)
    client.login()

    sync_activities(client, sb, user_id, limit, detail_budget)

    # El bienestar va aparte: si falla, las actividades ya están guardadas.
    try:
        sync_daily_metrics(client, sb, user_id, backfill_days)
        sync_vo2max_history(client, sb, user_id)
    except Exception as exc:  # noqa: BLE001
        print(f"Error sincronizando bienestar: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
