"""Pulse AI — VPS trigger script.

Reads daily_metrics.json, calls Groq, posts the AI message to the Vercel app.
The Vercel app stores it in KV and sends a push notification.

Usage (add to crontab on VPS):
  # Morning report (7:00 CEST = 5:00 UTC)
  0 5 * * *   /root/bioaipulse/.venv/bin/python /root/bioaipulse/vps/pulse_ai.py --mode morning

  # Evening summary (22:00 CEST = 20:00 UTC)
  0 20 * * *  /root/bioaipulse/.venv/bin/python /root/bioaipulse/vps/pulse_ai.py --mode evening

  # Workout detection (every 15 min)
  */15 * * * * /root/bioaipulse/.venv/bin/python /root/bioaipulse/vps/pulse_ai.py --mode workout

Required env (set in /root/bioaipulse/.env or export in crontab):
  GROQ_API_KEY       — Groq API key
  PULSE_AI_URL       — e.g. https://pulse-ai.vercel.app (no trailing slash)
  CRON_SECRET        — same value as in Vercel env
  METRICS_PATH       — default /root/bioaipulse/data/daily_metrics.json
  STATE_PATH         — default /root/bioaipulse/data/pulse_state.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from groq import Groq

load_dotenv("/root/bioaipulse/.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pulse_ai")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GROQ_KEY      = os.environ.get("GROQ_API_KEY",  "")
PULSE_AI_URL  = os.environ.get("PULSE_AI_URL",  "").rstrip("/")
CRON_SECRET   = os.environ.get("CRON_SECRET",   "")
METRICS_PATH  = Path(os.environ.get("METRICS_PATH", "/root/bioaipulse/data/daily_metrics.json"))
STATE_PATH    = Path(os.environ.get("STATE_PATH",   "/root/bioaipulse/data/pulse_state.json"))
MODEL         = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """Jesteś Pulse — osobistym analitykiem zdrowia i trenerem regeneracji Franka. Rozmawiasz po polsku, bezpośrednio i konkretnie. Nie jesteś chatbotem — jesteś ekspertem który analizuje twarde dane i wyciąga z nich praktyczne wnioski.

## Twoja rola
Analizujesz dane biometryczne z opaski Fitbit i generujesz trzy typy raportów:
1. Morning Readiness Report — poranny raport gotowości (wysyłany o 7:00)
2. Evening Summary — wieczorne podsumowanie dnia (wysyłane o 22:00)
3. Workout Analysis — analiza zakończonego treningu (wysyłana po treningu)

## Jak interpretujesz dane
### Tętno spoczynkowe (RHR)
- < 50 bpm — doskonałe (atletyczne)
- 50–60 bpm — bardzo dobre
- 60–70 bpm — dobre
- > 70 bpm — podwyższone, warto zwrócić uwagę

### HRV (RMSSD)
- Wyższe = lepsza regeneracja
- Spadek > 10ms względem normy = sygnał przemęczenia

### Sen
- 7–9h = optymalne; < 6h = niedobór; deep sleep powinien stanowić 15–20%

### Aktywność
- 8 000–10 000 kroków = aktywny tryb; < 5 000 = siedzący

## Formaty

### Morning Readiness Report
```
RAPORT GOTOWOŚCI — [DATA]
━━━━━━━━━━━━━━━━━━━━━━━━━
WYNIK REGENERACJI: [X/100]

SEN
• Czas: Xh Xmin  (score: X/100)
• Fazy: deep Xmin | REM Xmin | light Xmin | awake Xmin

TĘTNO
• Spoczynkowe: X bpm
• HRV: X ms

GOTOWOŚĆ DNIA
[2-3 zdania konkretnej oceny]

REKOMENDACJA
[1-2 praktyczne porady]
━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Evening Summary
```
PODSUMOWANIE — [DATA]
━━━━━━━━━━━━━━━━━━━━━━━━━
AKTYWNOŚĆ
• Kroki: X  (cel: 8 000)
• Kalorie: X kcal
• AZM: X min

TRENING
[jeśli był — krótka ocena; jeśli nie było — odnotuj]

REGENERACJA NA JUTRO
[Ocena i rekomendacja]
━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Workout Analysis
```
ANALIZA TRENINGU — [TYP]  [DATA]
━━━━━━━━━━━━━━━━━━━━━━━━━
OBCIĄŻENIE
• Czas: Xmin | Dystans: X km
• Tętno avg: X bpm | max: X bpm | Kalorie: X kcal

OCENA SESJI
[2-3 zdania oceny treningu na podstawie danych HR]

REGENERACJA
[Ile czasu na regenerację, co zalecasz jutro]
━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Zasady
- Mów do Franka bezpośrednio, po imieniu
- Każda ocena oparta na konkretnej liczbie z danych
- Jeśli danych brakuje — napisz wprost, nie zgaduj
- Nie diagnozujesz, ale mówisz co dane sugerują
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_metrics(days: int = 3) -> list[dict]:
    if not METRICS_PATH.exists():
        raise FileNotFoundError(f"Brak pliku {METRICS_PATH}")
    data = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return data[-days:]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def call_groq(user_msg: str) -> str:
    client = Groq(api_key=GROQ_KEY)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=1024,
    )
    return resp.choices[0].message.content


def post_to_vercel(type_: str, title: str, content: str) -> dict:
    resp = requests.post(
        f"{PULSE_AI_URL}/api/receive",
        json={"type": type_, "title": title, "content": content, "ts": int(time.time())},
        headers={"X-Cron-Secret": CRON_SECRET, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data formatters
# ---------------------------------------------------------------------------

def _date(entry: dict) -> str:
    return (entry.get("fetched_at") or entry.get("period_end") or "")[:10]


def format_morning(metrics: list[dict]) -> str:
    lines = []
    for e in metrics:
        lines.append(f"## {_date(e)}")
        sleep = e.get("sleep") or {}
        total = sleep.get("total_minutes")
        if total:
            h, m = divmod(total, 60)
            st = sleep.get("stages_minutes") or {}
            lines.append(f"- Sen: {h}h {m}min (score: {sleep.get('sleep_score','brak')})")
            lines.append(
                f"  deep={st.get('deep',0)}min rem={st.get('rem',0)}min "
                f"light={st.get('light',0)}min awake={st.get('awake',0)}min"
            )
            lines.append(f"  zaśnięcie: {(sleep.get('sleep_start',''))[:16].replace('T',' ')}")
        else:
            lines.append("- Sen: brak danych")
        lines.append(f"- RHR: {e.get('resting_hr_bpm','brak')} bpm")
        lines.append(f"- HRV: {e.get('hrv_rmssd','brak')} ms")
        lines.append(f"- Readiness: {(e.get('daily_readiness') or {}).get('score','brak')}/100")
        lines.append("")
    return "\n".join(lines)


def format_evening(entry: dict) -> str:
    lines = [
        f"## {_date(entry)}",
        f"- Kroki: {entry.get('steps','brak')}",
        f"- Kalorie: {entry.get('calories_kcal','brak')} kcal",
        f"- Dystans: {entry.get('distance_km','brak')} km",
        f"- Active Zone Minutes: {entry.get('active_zone_minutes','brak')}",
    ]
    hr = entry.get("hr_zones_minutes") or {}
    lines.append(
        f"- Strefy HR: fat_burn={hr.get('fat_burn',0)}min "
        f"cardio={hr.get('cardio',0)}min peak={hr.get('peak',0)}min"
    )
    lines.append(f"- SpO2: {entry.get('spo2_pct','brak')}%")
    lines.append(f"- Readiness: {(entry.get('daily_readiness') or {}).get('score','brak')}/100")
    workouts = entry.get("workouts") or []
    if workouts:
        for w in workouts:
            lines.append(
                f"- Trening: {w.get('type')} {w.get('duration_min')}min "
                f"avg_hr={w.get('avg_hr')} kcal={w.get('calories')}"
            )
    else:
        lines.append("- Brak treningu")
    return "\n".join(lines)


def format_workout(workout: dict, entry: dict) -> str:
    return (
        f"Typ: {workout.get('type','nieznany')}\n"
        f"Czas startu: {(workout.get('start',''))[:16].replace('T',' ')}\n"
        f"Czas trwania: {workout.get('duration_min','?')} min\n"
        f"Dystans: {workout.get('distance_km','?')} km\n"
        f"Tętno avg: {workout.get('avg_hr','?')} bpm | max: {workout.get('max_hr','?')} bpm\n"
        f"Kalorie: {workout.get('calories','?')} kcal\n"
        f"RHR tego dnia: {entry.get('resting_hr_bpm','brak')} bpm\n"
        f"HRV tego dnia: {entry.get('hrv_rmssd','brak')} ms\n"
        f"Readiness: {(entry.get('daily_readiness') or {}).get('score','brak')}/100\n"
    )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_morning() -> None:
    metrics = load_metrics(days=3)
    data_text = format_morning(metrics)
    today = datetime.now().strftime("%d.%m.%Y")
    msg = (
        f"Dzisiaj jest {today}, godzina 7:00. "
        f"Wygeneruj Morning Readiness Report dla Franka.\n\n{data_text}"
    )
    log.info("Calling Groq for morning report…")
    content = call_groq(msg)
    result = post_to_vercel("sleep", "Raport poranny", content)
    log.info("Morning report sent: %s", result)


def mode_evening() -> None:
    metrics = load_metrics(days=1)
    entry = metrics[-1]
    data_text = format_evening(entry)
    today = datetime.now().strftime("%d.%m.%Y")
    msg = (
        f"Dzisiaj jest {today}, godzina 22:00. "
        f"Wygeneruj Evening Summary dla Franka.\n\n{data_text}"
    )
    log.info("Calling Groq for evening summary…")
    content = call_groq(msg)
    result = post_to_vercel("evening", "Wieczorne podsumowanie", content)
    log.info("Evening summary sent: %s", result)


def mode_workout() -> None:
    state = load_state()
    last_notified = state.get("last_workout_ts", "")

    raw = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = [raw]

    # Collect all workouts newer than last notified, sorted oldest-first
    candidates: list[tuple[str, dict, dict]] = []
    for entry in raw:
        for w in (entry.get("workouts") or []):
            ts = w.get("start", "")
            if ts and ts > last_notified:
                candidates.append((ts, w, entry))

    if not candidates:
        log.info("No new workouts.")
        return

    candidates.sort(key=lambda x: x[0])          # oldest first
    ts, workout, entry = candidates[0]            # handle one per run

    data_text = format_workout(workout, entry)
    wtype = workout.get("type", "Trening")
    msg = (
        f"Franek właśnie ukończył trening: {wtype}. "
        f"Przeanalizuj tę sesję i daj mu konkretny feedback.\n\n{data_text}"
    )

    log.info("Calling Groq for workout analysis (%s at %s)…", wtype, ts)
    content = call_groq(msg)

    result = post_to_vercel("workout", f"Analiza treningu — {wtype}", content)
    log.info("Workout analysis sent: %s", result)

    state["last_workout_ts"] = ts
    save_state(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Pulse AI trigger script")
    p.add_argument("--mode", choices=["morning", "evening", "workout"], required=True)
    args = p.parse_args()

    for name, val, label in [
        (GROQ_KEY,     GROQ_KEY,     "GROQ_API_KEY"),
        (PULSE_AI_URL, PULSE_AI_URL, "PULSE_AI_URL"),
        (CRON_SECRET,  CRON_SECRET,  "CRON_SECRET"),
    ]:
        if not val:
            log.error("Missing env var: %s", label)
            sys.exit(1)

    {"morning": mode_morning, "evening": mode_evening, "workout": mode_workout}[args.mode]()


if __name__ == "__main__":
    main()
