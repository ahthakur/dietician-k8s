"""
Personal Dietician — Scheduler
Simplified: only protein shake reminders.
"""

from __future__ import annotations
from typing import Optional, List

import logging
from datetime import datetime, date
from config import WORKOUTS, WAKE_UP, SLEEP_TIME
from db import was_reminder_sent, log_reminder_sent, check_food_log_for_keyword, ack_reminder
from notifications import send_protein_reminder

logger = logging.getLogger("dietician.scheduler")


def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _minutes_to_time(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def generate_daily_reminders(day_date: Optional[date] = None) -> List[dict]:
    if day_date is None:
        day_date = date.today()
    dow = day_date.weekday()
    workout = WORKOUTS.get(dow)

    reminders = []

    # Protein shake: post workout or 3 PM on rest days
    if workout:
        workout_start = _time_to_minutes(workout["time"])
        workout_end = workout_start + workout["duration_min"]
        shake_time = workout_end + 20
        reminders.append({
            "time": _minutes_to_time(shake_time),
            "type": "protein",
            "label": "Protein Shake (post workout)",
            "minute": shake_time,
        })
    else:
        reminders.append({
            "time": "15:00",
            "type": "protein",
            "label": "Protein Shake",
            "minute": 15 * 60,
        })

    return reminders


async def check_and_send_reminders():
    now = datetime.now()
    today = now.date()
    current_time = f"{now.hour:02d}:{now.minute:02d}"
    reminders = generate_daily_reminders(today)

    for reminder in reminders:
        if reminder["time"] != current_time:
            continue
        if was_reminder_sent(str(today), reminder["type"], reminder["time"]):
            continue

        # Skip protein reminder if already logged
        if reminder["type"] == "protein":
            if check_food_log_for_keyword(str(today), "protein shake") or check_food_log_for_keyword(str(today), "whey") or check_food_log_for_keyword(str(today), "protein bar"):
                logger.info("Skipping protein reminder: already logged")
                log_reminder_sent(str(today), reminder["type"], reminder["time"])
                ack_reminder(str(today), reminder["type"], reminder["time"])
                continue

        logger.info(f"Firing reminder: {reminder['label']} at {reminder['time']}")
        log_reminder_sent(str(today), reminder["type"], reminder["time"])

        await send_protein_reminder()
