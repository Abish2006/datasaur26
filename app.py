# -*- coding: utf-8 -*-
"""
app.py — Flask web application entry point.

Routes:
  GET  /              Dashboard (ticket list + charts + metrics)
  GET  /ticket/<id>   Ticket detail page
  GET  /process       Trigger AI analysis + routing (concurrent)
  GET  /reset         Drop analyses + reset workloads
  POST /ask           Star task: natural language → chart data (JSON)
  GET  /export        Download .sql file
  GET  /export/csv    Download .csv file
"""
import os
import csv
import io
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, render_template, redirect, url_for,
    request, jsonify, Response, send_from_directory,
)

from config import DATABASE_URL, OPENAI_MODEL
from models import db, Ticket, Manager, Office, Analysis
from ai_module import analyze_ticket, get_client
from routing import assign_ticket, reset_counter

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


def compute_metrics(rows):
    """Compute routing quality metrics from processed data."""
    if not rows:
        return {}

    total = len(rows)
    assigned = sum(1 for a, t, m, o in rows if m)
    unassigned = total - assigned

    # VIP compliance: VIP/Priority tickets assigned to VIP-skilled managers
    vip_tickets = [(a, t, m, o) for a, t, m, o in rows if t.segment in ("VIP", "Priority")]
    vip_correct = sum(1 for a, t, m, o in vip_tickets if m and m.skills and "VIP" in m.skills)
    vip_compliance = round(100 * vip_correct / len(vip_tickets), 1) if vip_tickets else 100.0

    # Language compliance: KZ/ENG tickets matched to skilled managers
    lang_tickets = [(a, t, m, o) for a, t, m, o in rows if a.language in ("KZ", "ENG")]
    lang_correct = sum(
        1 for a, t, m, o in lang_tickets
        if m and m.skills and a.language in m.skills
    )
    lang_compliance = round(100 * lang_correct / len(lang_tickets), 1) if lang_tickets else 100.0

    # Load distribution (std deviation of workloads for assigned managers)
    workloads = [m.current_workload for a, t, m, o in rows if m]
    load_std = round(statistics.stdev(workloads), 2) if len(workloads) > 1 else 0.0

    # Manager workload data for chart
    manager_workloads = {}
    for a, t, m, o in rows:
        if m:
            manager_workloads[m.full_name] = m.current_workload

    return {
        "total": total,
        "assigned": assigned,
        "unassigned": unassigned,
        "vip_total": len(vip_tickets),
        "vip_correct": vip_correct,
        "vip_compliance": vip_compliance,
        "lang_total": len(lang_tickets),
        "lang_correct": lang_correct,
        "lang_compliance": lang_compliance,
        "load_std": load_std,
        "manager_workloads": manager_workloads,
    }


# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────

@app.route("/")
def index():
    rows = (
        db.session.query(Analysis, Ticket, Manager, Office)
        .join(Ticket, Analysis.ticket_id == Ticket.id)
        .outerjoin(Manager, Analysis.manager_id == Manager.id)
        .outerjoin(Office, Analysis.office_id == Office.id)
        .order_by(Analysis.priority_score.desc())
        .all()
    )

    total = Ticket.query.count()
    processed = Analysis.query.count()
    unassigned = Analysis.query.filter(Analysis.manager_id.is_(None)).count()

    # Chart data
    type_counts = Counter(a.ticket_type for a, t, m, o in rows if a.ticket_type)
    sentiment_counts = Counter(a.sentiment for a, t, m, o in rows if a.sentiment)
    lang_counts = Counter(a.language for a, t, m, o in rows if a.language)

    # Metrics
    metrics = compute_metrics(rows)

    # Manager workload chart data
    mgr_labels = json.dumps(list(metrics.get("manager_workloads", {}).keys()), ensure_ascii=False)
    mgr_values = json.dumps(list(metrics.get("manager_workloads", {}).values()))

    return render_template(
        "index.html",
        rows=rows,
        total=total,
        processed=processed,
        unassigned=unassigned,
        metrics=metrics,
        type_labels=json.dumps(list(type_counts.keys()), ensure_ascii=False),
        type_values=json.dumps(list(type_counts.values())),
        sentiment_labels=json.dumps(list(sentiment_counts.keys()), ensure_ascii=False),
        sentiment_values=json.dumps(list(sentiment_counts.values())),
        lang_labels=json.dumps(list(lang_counts.keys()), ensure_ascii=False),
        lang_values=json.dumps(list(lang_counts.values())),
        mgr_labels=mgr_labels,
        mgr_values=mgr_values,
    )


# ──────────────────────────────────────────────
# Ticket detail
# ──────────────────────────────────────────────

@app.route("/ticket/<int:ticket_id>")
def ticket_detail(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    analysis = Analysis.query.filter_by(ticket_id=ticket_id).first()
    manager = Manager.query.get(analysis.manager_id) if analysis and analysis.manager_id else None
    office = Office.query.get(analysis.office_id) if analysis and analysis.office_id else None
    return render_template(
        "ticket.html",
        ticket=ticket,
        analysis=analysis,
        manager=manager,
        office=office,
    )


# ──────────────────────────────────────────────
# Serve image attachments
# ──────────────────────────────────────────────

@app.route("/attachment/<filename>")
def serve_attachment(filename):
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    return send_from_directory(data_dir, filename)


# ──────────────────────────────────────────────
# Processing (concurrent with ThreadPoolExecutor)
# ──────────────────────────────────────────────

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "process.log")


def log(msg):
    """Write to both stdout and log file."""
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


@app.route("/process")
def process_tickets():
    """AI-analyze and route all tickets that have no Analysis record yet."""
    # Clear log file for this run
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    processed_ids = db.session.query(Analysis.ticket_id).subquery()
    tickets = Ticket.query.filter(~Ticket.id.in_(processed_ids)).all()

    if not tickets:
        log("[PROCESS] All tickets already processed.")
        return redirect(url_for("index"))

    offices = Office.query.all()
    managers = Manager.query.all()

    # Phase 1: Concurrent AI analysis (2 workers to respect rate limits)
    log(f"[PROCESS] Analyzing {len(tickets)} tickets with AI (2 workers)...")
    ai_results = {}

    def analyze_one(ticket):
        return ticket.id, analyze_ticket(ticket)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(analyze_one, t): t for t in tickets}
        for future in as_completed(futures):
            try:
                tid, result = future.result()
                ai_results[tid] = result
                log(
                    f"  [AI] Ticket {tid}: {result.get('ticket_type')} | "
                    f"{result.get('sentiment')} | P{result.get('priority_score')}"
                )
            except Exception as e:
                t = futures[future]
                log(f"  [AI] Error analyzing ticket {t.id}: {e}")
                ai_results[t.id] = {
                    "ticket_type": "Консультация",
                    "sentiment": "Neutral",
                    "priority_score": 5,
                    "language": "RU",
                    "summary": "Ошибка анализа — требуется ручная проверка.",
                    "recommendation": "Обратитесь к клиенту для уточнения деталей.",
                    "latitude": None,
                    "longitude": None,
                }

    # Phase 2: Sequential routing (needs DB state consistency)
    log(f"[PROCESS] Routing {len(ai_results)} tickets to managers...")
    success_count = 0
    for ticket in tickets:
        analysis_data = ai_results.get(ticket.id)
        if not analysis_data:
            log(f"  [SKIP] Ticket {ticket.id}: no AI result")
            continue

        # Skip if already processed (safety against double-clicks)
        if Analysis.query.filter_by(ticket_id=ticket.id).first():
            log(f"  [SKIP] Ticket {ticket.id} already has analysis, skipping.")
            continue

        try:
            manager, office, reason = assign_ticket(ticket, analysis_data, offices, managers)

            record = Analysis(
                ticket_id=ticket.id,
                manager_id=manager.id if manager else None,
                office_id=office.id if office else None,
                ticket_type=analysis_data.get("ticket_type"),
                sentiment=analysis_data.get("sentiment"),
                priority_score=analysis_data.get("priority_score"),
                language=analysis_data.get("language"),
                summary=analysis_data.get("summary"),
                recommendation=analysis_data.get("recommendation"),
                latitude=analysis_data.get("latitude"),
                longitude=analysis_data.get("longitude"),
                assignment_reason=reason,
            )
            db.session.add(record)
            db.session.commit()
            success_count += 1
            log(
                f"  -> Ticket {ticket.id}: {analysis_data.get('ticket_type')} | "
                f"Manager: {manager.full_name if manager else 'UNASSIGNED'}"
            )
        except Exception as e:
            db.session.rollback()
            log(f"  [ERROR] Ticket {ticket.id} routing failed: {e}")

    log(f"[PROCESS] Done! {success_count}/{len(tickets)} tickets processed successfully.")
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload_tickets():
    """Upload a CSV file with new tickets, append to DB (skip duplicates by GUID)."""
    file = request.files.get("file")
    if not file or not file.filename.endswith(".csv"):
        return redirect(url_for("index"))

    import io as _io
    stream = _io.StringIO(file.stream.read().decode("utf-8-sig"))
    reader = csv.DictReader(stream)

    existing_guids = set(
        g[0] for g in db.session.query(Ticket.guid).all() if g[0]
    )

    added = 0
    skipped = 0
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
    log(f"[UPLOAD] Добавлено {added} новых обращений, пропущено {skipped} дубликатов.")
    return redirect(url_for("index"))


@app.route("/reset")
def reset():
    """Delete all analyses, restore initial manager workloads, reset routing counter."""
    Analysis.query.delete()

    # Restore initial workloads from CSV values
    import seed
    for m in Manager.query.all():
        m.current_workload = seed.INITIAL_WORKLOADS.get(m.full_name, 0)

    reset_counter()
    db.session.commit()
    print("[RESET] All analyses deleted, workloads restored, counter reset.")
    return redirect(url_for("index"))


# ──────────────────────────────────────────────
# SQL Export (VIEW-based)
# ──────────────────────────────────────────────

@app.route("/export")
def export_sql():
    """Generate SQL dump based on fire_final_view as a downloadable .sql file."""
    rows = (
        db.session.query(Analysis, Ticket, Manager, Office)
        .join(Ticket, Analysis.ticket_id == Ticket.id)
        .outerjoin(Manager, Analysis.manager_id == Manager.id)
        .outerjoin(Office, Analysis.office_id == Office.id)
        .order_by(Analysis.priority_score.desc())
        .all()
    )

    def esc(val):
        if val is None:
            return "NULL"
        return "'" + str(val).replace("'", "''") + "'"

    lines = [
        "-- F.I.R.E. Challenge — Exported Analysis Results",
        "-- Freedom Intelligent Routing Engine",
        "-- Generated from fire_final_view\n",
        """CREATE TABLE IF NOT EXISTS fire_results (
    ticket_id INTEGER PRIMARY KEY,
    guid VARCHAR(100),
    segment VARCHAR(50),
    city VARCHAR(100),
    description TEXT,
    ticket_type VARCHAR(100),
    sentiment VARCHAR(20),
    priority_score INTEGER,
    language VARCHAR(10),
    summary TEXT,
    recommendation TEXT,
    customer_latitude FLOAT,
    customer_longitude FLOAT,
    assigned_manager VARCHAR(200),
    manager_position VARCHAR(100),
    assigned_office VARCHAR(100),
    office_address TEXT,
    assignment_reason JSONB,
    assigned_at TIMESTAMP
);\n""",
    ]

    for a, t, m, o in rows:
        reason_json = json.dumps(a.assignment_reason, ensure_ascii=False) if a.assignment_reason else "NULL"
        reason_val = f"'{reason_json}'" if a.assignment_reason else "NULL"
        vals = ", ".join([
            str(t.id),
            esc(t.guid),
            esc(t.segment),
            esc(t.city),
            esc(t.description),
            esc(a.ticket_type),
            esc(a.sentiment),
            str(a.priority_score) if a.priority_score else "NULL",
            esc(a.language),
            esc(a.summary),
            esc(a.recommendation),
            str(a.latitude) if a.latitude else "NULL",
            str(a.longitude) if a.longitude else "NULL",
            esc(m.full_name if m else None),
            esc(m.position if m else None),
            esc(o.name if o else None),
            esc(o.address if o else None),
            reason_val,
            esc(str(a.assigned_at) if a.assigned_at else None),
        ])
        lines.append(f"INSERT INTO fire_results VALUES ({vals});")

    sql_content = "\n".join(lines)

    return Response(
        sql_content,
        mimetype="application/sql",
        headers={"Content-Disposition": "attachment; filename=fire_results.sql"},
    )


@app.route("/export/csv")
def export_csv():
    """Export analysis results as CSV."""
    rows = (
        db.session.query(Analysis, Ticket, Manager, Office)
        .join(Ticket, Analysis.ticket_id == Ticket.id)
        .outerjoin(Manager, Analysis.manager_id == Manager.id)
        .outerjoin(Office, Analysis.office_id == Office.id)
        .order_by(Analysis.priority_score.desc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ticket_id", "guid", "segment", "city", "ticket_type", "sentiment",
        "priority_score", "language", "summary", "recommendation",
        "customer_lat", "customer_lon", "assigned_manager",
        "manager_position", "assigned_office", "office_address", "assigned_at",
    ])

    for a, t, m, o in rows:
        writer.writerow([
            t.id, t.guid, t.segment, t.city,
            a.ticket_type, a.sentiment, a.priority_score, a.language,
            a.summary, a.recommendation,
            a.latitude, a.longitude,
            m.full_name if m else "UNASSIGNED",
            m.position if m else "",
            o.name if o else "",
            o.address if o else "",
            a.assigned_at,
        ])

    csv_content = output.getvalue()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=fire_results.csv"},
    )


# ──────────────────────────────────────────────
# Star task: AI-powered natural language query → chart
# ──────────────────────────────────────────────

@app.route("/ask", methods=["POST"])
def ask_ai():
    query = request.json.get("query", "")
    if not query:
        return jsonify({"error": "No query provided"}), 400

    rows = (
        db.session.query(Analysis, Ticket, Manager, Office)
        .join(Ticket, Analysis.ticket_id == Ticket.id)
        .outerjoin(Manager, Analysis.manager_id == Manager.id)
        .outerjoin(Office, Analysis.office_id == Office.id)
        .all()
    )
    summary_lines = []
    for a, t, m, o in rows:
        summary_lines.append(
            f"type={a.ticket_type}, sentiment={a.sentiment}, "
            f"priority={a.priority_score}, lang={a.language}, "
            f"segment={t.segment}, office={o.name if o else 'N/A'}, "
            f"manager={m.full_name if m else 'UNASSIGNED'}, "
            f"city={t.city or 'N/A'}"
        )
    data_summary = "\n".join(summary_lines[:200])

    prompt = f"""You are a data analyst. Given this dataset of customer support tickets,
answer the user's question by returning a Chart.js-compatible JSON object.

Dataset (one ticket per line):
{data_summary}

User question: {query}

Return ONLY a JSON object like this:
{{
  "chart_type": "<bar | pie | doughnut | line>",
  "title": "<chart title>",
  "labels": ["label1", "label2", ...],
  "values": [number1, number2, ...]
}}"""

    try:
        response = get_client().chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=512,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        chart_data = json.loads(raw)
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    app.run(debug=True, use_reloader=False, threaded=True, port=5000)
