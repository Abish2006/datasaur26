# -*- coding: utf-8 -*-
"""
routing.py — Business rules engine for ticket-to-manager assignment.

Cascade logic:
  1. Find nearest office by GPS distance (or split Astana/Almaty if unknown)
  2. Filter managers at that office by hard skills (VIP, position, language)
  3. Pick top-2 by lowest workload
  4. Assign via persistent round-robin counter
"""
import math
from typing import Optional, Tuple

from models import db, RoutingState


def _get_rr_counter() -> int:
    """Load round-robin counter from database."""
    state = RoutingState.query.get(1)
    if state is None:
        state = RoutingState(id=1, rr_counter=0)
        db.session.add(state)
        db.session.flush()
    return state.rr_counter


def _increment_rr_counter():
    """Increment and persist round-robin counter."""
    state = RoutingState.query.get(1)
    if state is None:
        state = RoutingState(id=1, rr_counter=1)
        db.session.add(state)
    else:
        state.rr_counter += 1
    db.session.flush()


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two GPS points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def find_nearest_office(lat: float, lon: float, offices):
    """Return the Office object closest to the given coordinates."""
    return min(
        offices,
        key=lambda o: haversine(lat, lon, o.latitude, o.longitude)
        if (o.latitude and o.longitude)
        else float("inf"),
    )


def assign_ticket(ticket, analysis: dict, offices, managers) -> Tuple[Optional[object], Optional[object], dict]:
    """
    Determine the best manager and office for a ticket.

    Returns (manager_or_None, office_or_None, assignment_reason).
    Updates manager.current_workload in memory (caller must db.session.commit()).
    """
    reason = {
        "nearest_office": None,
        "distance_km": None,
        "filters_applied": [],
        "candidates_initial": 0,
        "candidates_after_filters": 0,
        "top2_managers": [],
        "chosen_by": "round_robin",
        "chosen_reason": "",
    }

    rr_counter = _get_rr_counter()
    lat = analysis.get("latitude")
    lon = analysis.get("longitude")

    # ── STEP 1: Determine target office(s) ──────────────────────────
    if lat and lon:
        nearest = find_nearest_office(lat, lon, offices)
        target_ids = [nearest.id]
        dist = haversine(lat, lon, nearest.latitude, nearest.longitude)
        reason["nearest_office"] = nearest.name
        reason["distance_km"] = round(dist, 1)
        fallback_office = nearest
    else:
        # Unknown / foreign address → pool Astana + Almaty
        special = [o for o in offices if o.name in ("Астана", "Алматы")]
        if not special:
            special = offices[:2]
        target_ids = [o.id for o in special]
        fallback_office = special[rr_counter % len(special)]
        reason["nearest_office"] = "Астана/Алматы (fallback)"
        reason["distance_km"] = None
        reason["filters_applied"].append("unknown_address_fallback")

    # ── STEP 2: Candidates at target office(s) ──────────────────────
    candidates = [m for m in managers if m.office_id in target_ids]
    reason["candidates_initial"] = len(candidates)

    # ── STEP 3: Hard skill filters (all apply simultaneously) ────────

    # 3a. VIP/Priority segment → manager must have VIP skill
    if ticket.segment in ("VIP", "Priority"):
        candidates = [m for m in candidates if m.skills and "VIP" in m.skills]
        reason["filters_applied"].append("VIP_skill")

    # 3b. "Смена данных" ticket type → manager must be Главный специалист
    if analysis.get("ticket_type") == "Смена данных":
        candidates = [
            m for m in candidates if m.position == "Главный специалист"
        ]
        reason["filters_applied"].append("data_change_position")

    # 3c. Language requirement
    lang = analysis.get("language", "RU")
    if lang == "KZ":
        candidates = [m for m in candidates if m.skills and "KZ" in m.skills]
        reason["filters_applied"].append("KZ_language")
    elif lang == "ENG":
        candidates = [m for m in candidates if m.skills and "ENG" in m.skills]
        reason["filters_applied"].append("ENG_language")

    reason["candidates_after_filters"] = len(candidates)

    if not candidates:
        print(
            f"  [ROUTING] No qualified manager found for ticket {ticket.id} "
            f"(segment={ticket.segment}, type={analysis.get('ticket_type')}, lang={lang})"
        )
        reason["chosen_by"] = "unassigned"
        reason["chosen_reason"] = "No qualified manager found after applying filters"
        return None, fallback_office, reason

    # ── STEP 4: Sort by workload, pick top 2 ────────────────────────
    candidates.sort(key=lambda m: m.current_workload)
    top2 = candidates[:2]
    reason["top2_managers"] = [
        f"{m.full_name} (workload: {m.current_workload})" for m in top2
    ]

    # ── STEP 5: Round-robin between the 2 ───────────────────────────
    chosen = top2[rr_counter % len(top2)]
    _increment_rr_counter()

    reason["chosen_by"] = "round_robin"
    reason["chosen_reason"] = (
        f"Selected {chosen.full_name} (workload: {chosen.current_workload}) "
        f"via round-robin (counter={rr_counter})"
    )

    # Increment workload (persisted when caller commits)
    chosen.current_workload += 1

    target_office = fallback_office if not (lat and lon) else find_nearest_office(lat, lon, offices)
    return chosen, target_office, reason


def reset_counter():
    """Reset round-robin counter in database."""
    state = RoutingState.query.get(1)
    if state:
        state.rr_counter = 0
        db.session.flush()
