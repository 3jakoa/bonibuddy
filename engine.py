from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any

# ===== In-memory matching engine (MVP) =====
WINDOW_MIN = 15  # match okno ±15 minut


@dataclass
class Request:
    user_id: int
    chat_id: int
    location: str
    when: datetime
    username: Optional[str] = None
    name: Optional[str] = None


# Čakalnica v pomnilniku
waiting: List[Request] = []


def _close_in_time(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= WINDOW_MIN * 60


def _find_match(location: str, when: datetime) -> Optional[Request]:
    for req in waiting:
        if req.location == location and _close_in_time(req.when, when):
            return req
    return None


def add_request(
    *,
    user_id: int,
    chat_id: int,
    location: str,
    when: datetime,
    username: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Doda request v čakalnico.

    Če najde match, vrne:
    {
        'location': str,
        'when': datetime,
        'a': {...},  # čakajoči
        'b': {...},  # novi
    }

    Če ne najde matcha, vrne None.
    """

    # Ne dovoli, da isti user čaka večkrat
    for req in waiting:
        if req.user_id == user_id:
            return None

    match = _find_match(location, when)
    new_req = Request(
        user_id=user_id,
        chat_id=chat_id,
        location=location,
        when=when,
        username=username,
        name=name,
    )

    if match:
        try:
            waiting.remove(match)
        except ValueError:
            pass

        return {
            "location": location,
            "when": when,
            "a": asdict(match),
            "b": asdict(new_req),
        }

    waiting.append(new_req)
    return None


def cancel_wait(user_id: int) -> bool:
    """Odstrani userja iz čakalnice. Vrne True, če je bil odstranjen."""
    for req in list(waiting):
        if req.user_id == user_id:
            waiting.remove(req)
            return True
    return False


def waiting_count() -> int:
    return len(waiting)