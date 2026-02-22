# F.I.R.E. — Freedom Intelligent Routing Engine

## Quick Start (run in this exact order)

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Create the PostgreSQL database
Open **psql** or **pgAdmin** and run:
```sql
CREATE DATABASE fire_challenge;
```

### 3. Set environment variables
**Windows (PowerShell):**
```powershell
$env:OPENAI_API_KEY = "sk-YOUR_KEY_HERE"
$env:GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_MAPS_KEY_HERE"
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost/fire_challenge"
```

**Or create a `.env` file** (easier for hackathon):
```
OPENAI_API_KEY=sk-YOUR_KEY_HERE
GOOGLE_MAPS_API_KEY=YOUR_GOOGLE_MAPS_KEY_HERE
DATABASE_URL=postgresql://postgres:yourpassword@localhost/fire_challenge
```

### 4. Seed the database (load CSV data)
```bash
python seed.py
```
Expected output:
```
Tables created.
Inserted 15 offices.
Inserted 51 managers.
Inserted 47 tickets.
Seeding complete!
```

### 5. Run the Flask app
```bash
python app.py
```

### 6. Open the dashboard
Go to: http://localhost:5000

### 7. Process tickets
Click the green **"Run Processing"** button on the dashboard.
This calls OpenAI API for each ticket (~2-3 sec each, ~2 min total for 47 tickets).

---

## Architecture

```
tickets.csv + managers.csv + business_units.csv
        ↓ seed.py
   PostgreSQL DB
        ↓
   app.py (Flask)
     ├─ /process → ai_module.py (OpenAI API) → routing.py → Analysis table
     ├─ /         → Dashboard with charts
     ├─ /ticket/<id> → Detail view
     └─ /ask (POST) → Star task: AI → Chart.js
```

## File Overview

| File | Purpose |
|------|---------|
| `config.py` | DB URL + OpenAI API key + Google Maps key |
| `models.py` | SQLAlchemy ORM (Office, Manager, Ticket, Analysis) |
| `seed.py` | Load CSVs into DB |
| `ai_module.py` | OpenAI API: classify ticket, detect language, geocode |
| `routing.py` | Business rules: find nearest office, filter managers, round-robin |
| `app.py` | Flask web server |
| `templates/index.html` | Dashboard |
| `templates/ticket.html` | Ticket detail page |
