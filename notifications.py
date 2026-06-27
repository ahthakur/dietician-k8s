"""
Personal Dietician — Notifications via ntfy.sh
Simplified: protein shake reminders + system notifications.
"""

from __future__ import annotations
from typing import Optional, List

import httpx
import logging
from config import NTFY_SERVER, NTFY_TOPICS, NTFY_TOPIC

logger = logging.getLogger("dietician.notifications")


async def _send_to_topic(topic: str, title: str, message: str, priority: str = "default", tags: str = "bell"):
    url = f"{NTFY_SERVER}/{topic}"
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=message, headers=headers)
            if resp.status_code == 200:
                logger.info(f"Notification sent to {topic}: {title}")
            else:
                logger.warning(f"ntfy returned {resp.status_code} for {topic}")
    except Exception as e:
        logger.error(f"Failed to send notification to {topic}: {e}")


async def send_notification(title: str, message: str, target: str = "both", priority: str = "default", tags: str = "bell"):
    if target == "both":
        for user_id, topic in NTFY_TOPICS.items():
            await _send_to_topic(topic, title, message, priority, tags)
    elif target in NTFY_TOPICS:
        await _send_to_topic(NTFY_TOPICS[target], title, message, priority, tags)


async def send_protein_reminder():
    await send_notification(
        title="Protein Shake",
        message="You keep skipping this. Drink your protein shake now.",
        priority="high",
        tags="cup_with_straw",
    )


async def send_system_notification(title: str, message: str):
    await send_notification(title, message, tags="bell")
