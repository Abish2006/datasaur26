from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ARRAY, String

db = SQLAlchemy()


class Office(db.Model):
    __tablename__ = "office"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.Text)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)


class Manager(db.Model):
    __tablename__ = "manager"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    # Специалист | Ведущий специалист | Главный специалист
    position = db.Column(db.String(100))
    office_id = db.Column(db.Integer, db.ForeignKey("office.id"))
    skills = db.Column(ARRAY(String))      # e.g. ['VIP', 'ENG']
    current_workload = db.Column(db.Integer, default=0)

    office = db.relationship("Office", backref="managers")


class Ticket(db.Model):
    __tablename__ = "ticket"
    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(100))
    gender = db.Column(db.String(20))
    birth_date = db.Column(db.String(50))
    description = db.Column(db.Text)
    attachments = db.Column(db.Text)
    segment = db.Column(db.String(50))   # Mass | VIP | Priority
    country = db.Column(db.String(100))
    region = db.Column(db.String(100))
    city = db.Column(db.String(100))
    street = db.Column(db.String(200))
    building = db.Column(db.String(50))


class Analysis(db.Model):
    __tablename__ = "analysis"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), unique=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("manager.id"), nullable=True)
    office_id = db.Column(db.Integer, db.ForeignKey("office.id"), nullable=True)

    # AI-generated fields
    ticket_type = db.Column(db.String(100))
    sentiment = db.Column(db.String(20))
    priority_score = db.Column(db.Integer)
    language = db.Column(db.String(10))
    summary = db.Column(db.Text)
    recommendation = db.Column(db.Text)

    # Geocoded coordinates of the customer
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    # Routing explainability
    assignment_reason = db.Column(db.JSON, nullable=True)

    assigned_at = db.Column(db.DateTime, server_default=db.func.now())

    ticket = db.relationship("Ticket", backref="analysis")
    manager = db.relationship("Manager", backref="assignments")
    office = db.relationship("Office", backref="analyses")


class RoutingState(db.Model):
    """Persistent round-robin counter — survives app restarts."""
    __tablename__ = "routing_state"
    id = db.Column(db.Integer, primary_key=True, default=1)
    rr_counter = db.Column(db.Integer, default=0)
