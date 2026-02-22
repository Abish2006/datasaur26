# -*- coding: utf-8 -*-
"""
routing.py — Business rules engine for ticket-to-manager assignment.

Cascade logic:
  1. Match city name or find nearest office by Haversine (client coords from Yandex API)
  2. Filter managers at that office by hard skills (VIP, position, language)
  3. Pick top-2 by lowest workload
  4. Assign via persistent round-robin counter
"""
import math
from typing import Optional, Tuple

from models import db, RoutingState

# Hardcoded GPS coordinates for all 15 Freedom Finance offices in Kazakhstan.
# Used as the ground truth for Haversine distance calculation against
# client coordinates obtained from Yandex Maps Geocoder API.
OFFICE_COORDS = {
    "Актау":             (43.6520, 51.2100),
    "Актобе":            (50.2797, 57.2074),
    "Алматы":            (43.2389, 76.8897),
    "Астана":            (51.1801, 71.4460),
    "Атырау":            (47.1066, 51.9146),
    "Караганда":         (49.8047, 73.1094),
    "Кокшетау":          (53.2836, 69.3962),
    "Костанай":          (53.2147, 63.6265),
    "Кызылорда":         (44.8490, 65.5074),
    "Павлодар":          (52.2867, 76.9677),
    "Петропавловск":     (54.8749, 69.1586),
    "Тараз":             (42.9002, 71.3784),
    "Уральск":           (51.2337, 51.3697),
    "Усть-Каменогорск":  (49.9488, 82.6271),
    "Шымкент":           (42.3170, 69.5963),
}

# Maps Kazakhstan regions (oblasts) to the nearest office city.
# Covers all current oblasts, legacy names, and common spelling variants.
REGION_TO_OFFICE = {
    # Standard oblasts
    "алматинская": "Алматы",
    "акмолинская": "Кокшетау",
    "актюбинская": "Актобе",
    "атырауская": "Атырау",
    "восточно-казахстанская": "Усть-Каменогорск",
    "жамбылская": "Тараз",
    "западно-казахстанская": "Уральск",
    "карагандинская": "Караганда",
    "костанайская": "Костанай",
    "кызылординская": "Кызылорда",
    "мангистауская": "Актау",
    "павлодарская": "Павлодар",
    "северо-казахстанская": "Петропавловск",
    "туркестанская": "Шымкент",
    "абайская": "Усть-Каменогорск",
    "жетісуская": "Алматы",
    "жетисуская": "Алматы",
    "ұлытауская": "Караганда",
    "улытауская": "Караганда",
    # Cities of republican significance
    "г. алматы": "Алматы",
    "г. астана": "Астана",
    "г. шымкент": "Шымкент",
    "алматы": "Алматы",
    "астана": "Астана",
    "шымкент": "Шымкент",
    # Legacy / alternate names found in ticket data
    "юко": "Шымкент",
    "южно-казахстанская": "Шымкент",
    "семипалатинская": "Усть-Каменогорск",
    "вко": "Усть-Каменогорск",
    # English / transliterated names from ticket data
    "mangystau": "Актау",
    "almaty": "Алматы",
    "astana": "Астана",
}


def _find_office_by_region(region: str, offices):
    """Match a region/oblast string to an office using REGION_TO_OFFICE mapping."""
    if not region:
        return None
    region_lower = region.lower().strip()
    # Try exact match first
    if region_lower in REGION_TO_OFFICE:
        target_city = REGION_TO_OFFICE[region_lower]
        for o in offices:
            if o.name == target_city:
                return o
    # Try substring match (e.g. "Северо-Казахстанская обл." contains "северо-казахстанская")
    for key, target_city in REGION_TO_OFFICE.items():
        if key in region_lower or region_lower in key:
            for o in offices:
                if o.name == target_city:
                    return o
    return None


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


def _office_coords(office):
    """Get (lat, lon) for an office — hardcoded coords first, DB fallback."""
    if office.name in OFFICE_COORDS:
        return OFFICE_COORDS[office.name]
    if office.latitude is not None and office.longitude is not None:
        return (office.latitude, office.longitude)
    return None


def find_nearest_office(lat: float, lon: float, offices):
    """Return (Office, distance_km) closest to the given client coordinates."""
    best_office = None
    best_dist = float("inf")
    for o in offices:
        coords = _office_coords(o)
        if coords is None:
            continue
        dist = haversine(lat, lon, coords[0], coords[1])
        if dist < best_dist:
            best_dist = dist
            best_office = o
    return best_office, round(best_dist, 1)


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

    # ── Spam → never assign ──────────────────────────────────────
    if analysis.get("ticket_type") == "Спам":
        reason["chosen_by"] = "spam_filter"
        reason["chosen_reason"] = "Spam ticket — not assigned to any manager"
        return None, None, reason

    # ── VIP/Priority segment → force priority 10 ─────────────────
    if ticket.segment in ("VIP", "Priority"):
        analysis["priority_score"] = 10

    rr_counter = _get_rr_counter()
    lat = analysis.get("latitude")
    lon = analysis.get("longitude")

    # ── STEP 1: Determine target office ──────────────────────────
    # 1a. Try exact city name match first
    city = getattr(ticket, "city", "") or ""
    city_match = None
    if city:
        city_lower = city.lower()
        for o in offices:
            if o.name and (o.name.lower() == city_lower or o.name.lower() in city_lower or city_lower in o.name.lower()):
                city_match = o
                break

    if city_match:
        target_ids = [city_match.id]
        fallback_office = city_match
        reason["nearest_office"] = city_match.name
        reason["distance_km"] = 0.0
        reason["filters_applied"].append("city_name_match")
    elif lat is not None and lon is not None:
        # 1b. Use Haversine to find nearest office by client GPS (from Yandex API)
        nearest, dist = find_nearest_office(lat, lon, offices)
        target_ids = [nearest.id]
        reason["nearest_office"] = nearest.name
        reason["distance_km"] = dist
        reason["client_coords"] = [lat, lon]
        office_c = _office_coords(nearest)
        reason["office_coords"] = list(office_c) if office_c else None
        fallback_office = nearest
    else:
        # 1c. Try region/oblast mapping as last resort
        region = getattr(ticket, "region", "") or ""
        region_match = _find_office_by_region(region, offices)
        if region_match:
            target_ids = [region_match.id]
            fallback_office = region_match
            reason["nearest_office"] = region_match.name
            reason["distance_km"] = None
            reason["filters_applied"].append("region_match")
        else:
            # No location info at all — assign to Astana as default
            default = next((o for o in offices if o.name == "Астана"), offices[0])
            target_ids = [default.id]
            fallback_office = default
            reason["nearest_office"] = default.name
            reason["distance_km"] = None
            reason["filters_applied"].append("no_location_default")

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
        # No eligible manager at local office — search other offices by distance
        # Sort all offices by distance from the fallback office coords
        fb_coords = _office_coords(fallback_office)
        other_offices = [o for o in offices if o.id not in target_ids]
        if fb_coords:
            other_offices.sort(
                key=lambda o: haversine(fb_coords[0], fb_coords[1], *_office_coords(o))
                if _office_coords(o) else float("inf")
            )

        for other in other_offices:
            pool = [m for m in managers if m.office_id == other.id]
            # Apply the same skill filters
            if ticket.segment in ("VIP", "Priority"):
                pool = [m for m in pool if m.skills and "VIP" in m.skills]
            if analysis.get("ticket_type") == "Смена данных":
                pool = [m for m in pool if m.position == "Главный специалист"]
            if lang == "KZ":
                pool = [m for m in pool if m.skills and "KZ" in m.skills]
            elif lang == "ENG":
                pool = [m for m in pool if m.skills and "ENG" in m.skills]
            if pool:
                pool.sort(key=lambda m: m.current_workload)
                chosen = pool[0]
                other_coords = _office_coords(other)
                dist_to_other = (
                    round(haversine(fb_coords[0], fb_coords[1], other_coords[0], other_coords[1]), 1)
                    if fb_coords and other_coords else None
                )
                reason["filters_applied"].append("nearest_office_fallback")
                reason["chosen_by"] = "nearest_qualified"
                reason["chosen_reason"] = (
                    f"No eligible manager at {fallback_office.name}. "
                    f"Assigned to nearest qualified: {chosen.full_name} "
                    f"at {other.name} ({dist_to_other} km away, workload: {chosen.current_workload})"
                )
                chosen.current_workload += 1
                return chosen, other, reason

        print(
            f"  [ROUTING] No qualified manager found for ticket {ticket.id} "
            f"(segment={ticket.segment}, type={analysis.get('ticket_type')}, lang={lang})"
        )
        reason["chosen_by"] = "unassigned"
        reason["chosen_reason"] = "No qualified manager found at any office"
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

    target_office = fallback_office
    return chosen, target_office, reason


def reset_counter():
    """Reset round-robin counter in database."""
    state = RoutingState.query.get(1)
    if state:
        state.rr_counter = 0
        db.session.flush()
