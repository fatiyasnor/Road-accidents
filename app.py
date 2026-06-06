from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
from dateutil import parser as dateparser
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///crm.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── Models ──────────────────────────────────────────────────────────────────

STATUSES = ['New', 'Contacted', 'Qualified', 'Proposal Sent', 'Negotiating', 'Won', 'Lost']
SOURCES  = ['Website', 'Referral', 'LinkedIn', 'Cold Outreach', 'Event', 'Social Media', 'Other']


class Lead(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    company    = db.Column(db.String(120))
    email      = db.Column(db.String(120))
    phone      = db.Column(db.String(40))
    source     = db.Column(db.String(60), default='Other')
    status     = db.Column(db.String(40), default='New')
    value      = db.Column(db.Float, default=0.0)
    notes      = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    followups  = db.relationship('FollowUp', backref='lead', lazy=True,
                                 cascade='all, delete-orphan', order_by='FollowUp.due_date')

    @property
    def next_followup(self):
        pending = [f for f in self.followups if not f.completed]
        return pending[0] if pending else None

    @property
    def is_overdue(self):
        nf = self.next_followup
        return nf and nf.due_date < date.today()

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'company': self.company or '',
            'email': self.email or '', 'status': self.status,
            'value': self.value, 'source': self.source,
        }


class FollowUp(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    lead_id    = db.Column(db.Integer, db.ForeignKey('lead.id'), nullable=False)
    due_date   = db.Column(db.Date, nullable=False)
    note       = db.Column(db.String(255), default='')
    completed  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_overdue(self):
        return not self.completed and self.due_date < date.today()

    @property
    def is_today(self):
        return not self.completed and self.due_date == date.today()


# ─── Dashboard ───────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    today = date.today()
    leads = Lead.query.order_by(Lead.updated_at.desc()).all()

    status_counts = {s: 0 for s in STATUSES}
    pipeline_value = 0.0
    for lead in leads:
        status_counts[lead.status] = status_counts.get(lead.status, 0) + 1
        if lead.status not in ('Won', 'Lost'):
            pipeline_value += lead.value or 0

    overdue = (
        FollowUp.query
        .filter(FollowUp.completed == False, FollowUp.due_date < today)
        .order_by(FollowUp.due_date)
        .all()
    )
    upcoming = (
        FollowUp.query
        .filter(FollowUp.completed == False, FollowUp.due_date >= today)
        .order_by(FollowUp.due_date)
        .limit(10)
        .all()
    )
    recent_leads = leads[:5]

    return render_template('dashboard.html',
        status_counts=status_counts, statuses=STATUSES,
        pipeline_value=pipeline_value, total_leads=len(leads),
        overdue=overdue, upcoming=upcoming, recent_leads=recent_leads,
        today=today,
    )


# ─── Leads ───────────────────────────────────────────────────────────────────

@app.route('/leads')
def leads():
    q      = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    source = request.args.get('source', '')

    query = Lead.query
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(Lead.name.ilike(like), Lead.company.ilike(like), Lead.email.ilike(like))
        )
    if status:
        query = query.filter(Lead.status == status)
    if source:
        query = query.filter(Lead.source == source)

    leads_list = query.order_by(Lead.updated_at.desc()).all()
    return render_template('leads.html', leads=leads_list, statuses=STATUSES,
                           sources=SOURCES, q=q, filter_status=status, filter_source=source)


@app.route('/leads/new', methods=['GET', 'POST'])
def lead_new():
    if request.method == 'POST':
        lead = Lead(
            name    = request.form['name'].strip(),
            company = request.form.get('company', '').strip(),
            email   = request.form.get('email', '').strip(),
            phone   = request.form.get('phone', '').strip(),
            source  = request.form.get('source', 'Other'),
            status  = request.form.get('status', 'New'),
            value   = float(request.form.get('value') or 0),
            notes   = request.form.get('notes', '').strip(),
        )
        db.session.add(lead)
        db.session.commit()

        due_raw = request.form.get('followup_date', '').strip()
        if due_raw:
            db.session.add(FollowUp(
                lead_id  = lead.id,
                due_date = dateparser.parse(due_raw).date(),
                note     = request.form.get('followup_note', '').strip(),
            ))
            db.session.commit()

        flash(f'Lead "{lead.name}" created.', 'success')
        return redirect(url_for('lead_detail', lead_id=lead.id))

    return render_template('lead_form.html', lead=None, statuses=STATUSES, sources=SOURCES,
                           today=date.today().isoformat())


@app.route('/leads/<int:lead_id>')
def lead_detail(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    return render_template('lead_detail.html', lead=lead, today=date.today(),
                           statuses=STATUSES)


@app.route('/leads/<int:lead_id>/edit', methods=['GET', 'POST'])
def lead_edit(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    if request.method == 'POST':
        lead.name    = request.form['name'].strip()
        lead.company = request.form.get('company', '').strip()
        lead.email   = request.form.get('email', '').strip()
        lead.phone   = request.form.get('phone', '').strip()
        lead.source  = request.form.get('source', lead.source)
        lead.status  = request.form.get('status', lead.status)
        lead.value   = float(request.form.get('value') or 0)
        lead.notes   = request.form.get('notes', '').strip()
        lead.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Lead "{lead.name}" updated.', 'success')
        return redirect(url_for('lead_detail', lead_id=lead.id))

    return render_template('lead_form.html', lead=lead, statuses=STATUSES, sources=SOURCES,
                           today=date.today().isoformat())


@app.route('/leads/<int:lead_id>/delete', methods=['POST'])
def lead_delete(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    name = lead.name
    db.session.delete(lead)
    db.session.commit()
    flash(f'Lead "{name}" deleted.', 'info')
    return redirect(url_for('leads'))


@app.route('/leads/<int:lead_id>/status', methods=['POST'])
def lead_status(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    new_status = request.form.get('status')
    if new_status in STATUSES:
        lead.status = new_status
        lead.updated_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('lead_detail', lead_id=lead_id))


# ─── Follow-ups ──────────────────────────────────────────────────────────────

@app.route('/leads/<int:lead_id>/followups', methods=['POST'])
def followup_add(lead_id):
    Lead.query.get_or_404(lead_id)
    due_raw = request.form.get('due_date', '').strip()
    if not due_raw:
        flash('Please provide a due date.', 'warning')
        return redirect(url_for('lead_detail', lead_id=lead_id))
    fu = FollowUp(
        lead_id  = lead_id,
        due_date = dateparser.parse(due_raw).date(),
        note     = request.form.get('note', '').strip(),
    )
    db.session.add(fu)
    db.session.commit()
    flash('Follow-up scheduled.', 'success')
    return redirect(url_for('lead_detail', lead_id=lead_id))


@app.route('/followups/<int:fu_id>/complete', methods=['POST'])
def followup_complete(fu_id):
    fu = FollowUp.query.get_or_404(fu_id)
    fu.completed = True
    db.session.commit()
    return redirect(url_for('lead_detail', lead_id=fu.lead_id))


@app.route('/followups/<int:fu_id>/delete', methods=['POST'])
def followup_delete(fu_id):
    fu = FollowUp.query.get_or_404(fu_id)
    lead_id = fu.lead_id
    db.session.delete(fu)
    db.session.commit()
    return redirect(url_for('lead_detail', lead_id=lead_id))


# ─── JSON search (autocomplete) ──────────────────────────────────────────────

@app.route('/api/leads')
def api_leads():
    q = request.args.get('q', '').strip()
    query = Lead.query
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(Lead.name.ilike(like), Lead.company.ilike(like))
        )
    return jsonify([l.to_dict() for l in query.limit(20).all()])


# ─── Seed sample data ────────────────────────────────────────────────────────

@app.cli.command('seed')
def seed():
    """Load sample leads into the database."""
    samples = [
        dict(name='Alice Johnson', company='Bloom Design', email='alice@bloomdesign.io',
             phone='555-0101', source='Referral', status='Qualified', value=4500,
             notes='Needs a brand refresh. Follow up after her vacation.'),
        dict(name='Bob Martinez', company='SquarePeg SaaS', email='bob@squarepeg.com',
             phone='555-0102', source='LinkedIn', status='Proposal Sent', value=12000,
             notes='Sent proposal 2024-05-10. Awaiting sign-off from their CFO.'),
        dict(name='Carol Smith', company='', email='carol@gmail.com',
             phone='555-0103', source='Website', status='New', value=800,
             notes='Interested in monthly retainer.'),
        dict(name='David Lee', company='Pivot Analytics', email='david@pivotanalytics.co',
             phone='555-0104', source='Event', status='Won', value=9000,
             notes='Contract signed. Project starts next month.'),
        dict(name='Eva Patel', company='GreenLeaf Co', email='eva@greenleaf.com',
             phone='555-0105', source='Cold Outreach', status='Contacted', value=3200,
             notes='Left voicemail. Try email next.'),
    ]
    today = date.today()
    for s in samples:
        if not Lead.query.filter_by(email=s['email']).first():
            lead = Lead(**s)
            db.session.add(lead)
            db.session.flush()
            if lead.status not in ('Won', 'Lost'):
                db.session.add(FollowUp(lead_id=lead.id,
                                        due_date=today + timedelta(days=2),
                                        note='Initial check-in'))
    db.session.commit()
    print('Sample data loaded.')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
