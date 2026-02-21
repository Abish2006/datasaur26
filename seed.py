# -*- coding: utf-8 -*-
"""
seed.py — Load CSV data into PostgreSQL.
Run once: python seed.py
Append:   python seed.py --append [csv_path]
"""
import csv
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


# Office GPS coordinates (hardcoded — all 15 cities from business_units.csv)
OFFICE_COORDS = {
    "Актау":             (43.6520, 51.2100),
    "Актобе":            (50.2797, 57.2074),
    "Алматы":            (43.2389, 76.8897),
    "Астана":            (51.1801, 71.4460),
    "Атырау":            (47.1066, 51.9146),
    "Караганда":          (49.8047, 73.1094),
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

# Initial manager workloads from CSV (to restore on reset)
INITIAL_WORKLOADS = {}


def get_coords(city_name: str):
    """Return (lat, lon) for a city, trying multiple spellings."""
    coords = OFFICE_COORDS.get(city_name)
    if coords:
        return coords
    for key, val in OFFICE_COORDS.items():
        if key.lower() in city_name.lower() or city_name.lower() in key.lower():
            return val
    print(f"  WARNING: no coordinates found for office '{city_name}', defaulting to Astana")
    return (51.1801, 71.4460)


def create_views_and_indexes(db):
    """Create SQL VIEW and indexes for optimal query performance."""
    from sqlalchemy import text

    stmts = [
        # Indexes
        "CREATE INDEX IF NOT EXISTS idx_analysis_ticket ON analysis(ticket_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_manager ON analysis(manager_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_office ON analysis(office_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_priority ON analysis(priority_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_manager_office ON manager(office_id)",
        # Final view
        """
        CREATE OR REPLACE VIEW fire_final_view AS
        SELECT
            t.id AS ticket_id,
            t.guid,
            t.segment,
            t.gender,
            t.city,
            t.description,
            a.ticket_type,
            a.sentiment,
            a.priority_score,
            a.language,
            a.summary,
            a.recommendation,
            a.latitude AS customer_lat,
            a.longitude AS customer_lon,
            m.full_name AS assigned_manager,
            m.position AS manager_position,
            o.name AS assigned_office,
            o.address AS office_address,
            a.assignment_reason,
            a.assigned_at
        FROM analysis a
        JOIN ticket t ON a.ticket_id = t.id
        LEFT JOIN manager m ON a.manager_id = m.id
        LEFT JOIN office o ON a.office_id = o.id
        ORDER BY a.priority_score DESC
        """,
    ]
    for stmt in stmts:
        db.session.execute(text(stmt))
    db.session.commit()
    print("Views and indexes created.")


def run():
    from app import app
    from models import db, Office, Manager, Ticket, RoutingState

    with app.app_context():
        # Drop VIEW first (it depends on tables and blocks drop_all)
        from sqlalchemy import text
        try:
            db.session.execute(text("DROP VIEW IF EXISTS fire_final_view CASCADE"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        db.drop_all()
        db.create_all()
        print("Tables created.")

        # ── RoutingState init ───────────────────────────────────────
        db.session.add(RoutingState(id=1, rr_counter=0))
        db.session.flush()

        # ── Offices ──────────────────────────────────────────────────
        with open("data/business_units.csv", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("Офис", "").strip()
                address = row.get("Адрес", "").strip()
                lat, lon = get_coords(name)
                db.session.add(Office(
                    name=name,
                    address=address,
                    latitude=lat,
                    longitude=lon,
                ))
        db.session.commit()
        print(f"Inserted {Office.query.count()} offices.")

        # ── Managers ─────────────────────────────────────────────────
        offices_map = {o.name: o.id for o in Office.query.all()}

        with open("data/managers.csv", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                full_name = row.get("ФИО", "").strip()
                position = row.get("Должность ", "").strip()
                if not position:
                    position = row.get("Должность", "").strip()
                office_name = row.get("Офис", "").strip()
                skills_raw = row.get("Навыки", "").strip()
                workload_raw = row.get("Количество обращений в работе", "0").strip()

                skills = [s.strip() for s in skills_raw.split(",") if s.strip()] if skills_raw else []

                try:
                    workload = int(workload_raw)
                except ValueError:
                    workload = 0

                office_id = offices_map.get(office_name)
                if office_id is None:
                    for key, oid in offices_map.items():
                        if key.lower() in office_name.lower():
                            office_id = oid
                            break

                # Save initial workload for reset
                INITIAL_WORKLOADS[full_name] = workload

                db.session.add(Manager(
                    full_name=full_name,
                    position=position,
                    office_id=office_id,
                    skills=skills,
                    current_workload=workload,
                ))
        db.session.commit()
        print(f"Inserted {Manager.query.count()} managers.")

        # ── Tickets ───────────────────────────────────────────────────
        with open("data/tickets.csv", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row = {k.strip(): v for k, v in row.items()}

                db.session.add(Ticket(
                    guid=row.get("GUID клиента", "").strip(),
                    gender=row.get("Пол клиента", "").strip(),
                    birth_date=row.get("Дата рождения", "").strip(),
                    description=row.get("Описание", "").strip(),
                    attachments=row.get("Вложения", "").strip(),
                    segment=row.get("Сегмент клиента", "").strip(),
                    country=row.get("Страна", "").strip(),
                    region=row.get("Область", "").strip(),
                    city=row.get("Населённый пункт", "").strip(),
                    street=row.get("Улица", "").strip(),
                    building=row.get("Дом", "").strip(),
                ))
        db.session.commit()
        print(f"Inserted {Ticket.query.count()} tickets.")

        # ── Views & Indexes ──────────────────────────────────────────
        create_views_and_indexes(db)
        print("Seeding complete!")


def append_tickets(csv_path="data/tickets.csv"):
    """
    Load new tickets from a CSV file WITHOUT dropping existing data.
    Skips tickets whose GUID already exists in the database.
    """
    from app import app
    from models import db, Ticket

    with app.app_context():
        db.create_all()

        existing_guids = set(
            g[0] for g in db.session.query(Ticket.guid).all() if g[0]
        )
        print(f"Existing tickets in DB: {len(existing_guids)}")

        added = 0
        skipped = 0
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row = {k.strip(): v for k, v in row.items()}
                guid = row.get("GUID клиента", "").strip()

                if guid in existing_guids:
                    skipped += 1
                    continue

                db.session.add(Ticket(
                    guid=guid,
                    gender=row.get("Пол клиента", "").strip(),
                    birth_date=row.get("Дата рождения", "").strip(),
                    description=row.get("Описание", "").strip(),
                    attachments=row.get("Вложения", "").strip(),
                    segment=row.get("Сегмент клиента", "").strip(),
                    country=row.get("Страна", "").strip(),
                    region=row.get("Область", "").strip(),
                    city=row.get("Населённый пункт", "").strip(),
                    street=row.get("Улица", "").strip(),
                    building=row.get("Дом", "").strip(),
                ))
                existing_guids.add(guid)
                added += 1

        db.session.commit()
        print(f"Appended {added} new tickets, skipped {skipped} duplicates.")
        print(f"Total tickets now: {Ticket.query.count()}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if len(sys.argv) > 1 and sys.argv[1] == "--append":
        csv_path = sys.argv[2] if len(sys.argv) > 2 else "data/tickets.csv"
        append_tickets(csv_path)
    else:
        run()
