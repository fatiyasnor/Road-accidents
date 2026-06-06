from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from dateutil import parser as dateparser
import os

try:
    import stripe
    STRIPE_ENABLED = True
except ImportError:
    stripe = None
    STRIPE_ENABLED = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me-in-production')

_db_url = os.environ.get('DATABASE_URL', '').strip()
if not _db_url:
    _db_url = 'sqlite:///crm.db'
elif _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to continue.'
login_manager.login_message_category = 'info'

stripe.api_key          = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLIC_KEY       = os.environ.get('STRIPE_PUBLIC_KEY', '')
STRIPE_PRICE_ID         = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET   = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
FREE_LEAD_LIMIT         = 10

STATUSES = ['New', 'Contacted', 'Qualified', 'Proposal Sent', 'Negotiating', 'Won', 'Lost']
SOURCES  = ['Website', 'Referral', 'LinkedIn', 'Cold Outreach', 'Event', 'Social Media', 'Other']


# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    id                     = db.Column(db.Integer, primary_key=True)
    name                   = db.Column(db.String(120), nullable=False)
    email                  = db.Column(db.String(120), unique=True, nullable=False)
    password_hash          = db.Column(db.String(256), nullable=False)
    plan                   = db.Column(db.String(20), default='free')
    stripe_customer_id     = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    created_at             = db.Column(db.DateTime, default=datetime.utcnow)
    leads = db.relationship('Lead', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_pro(self):
        return self.plan == 'pro'

    @property
    def lead_count(self):
        return Lead.query.filter_by(user_id=self.id).count()

    @property
    def can_add_lead(self):
        return self.is_pro or self.lead_count < FREE_LEAD_LIMIT


class Lead(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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
        return {'id': self.id, 'name': self.name, 'company': self.company or '',
                'email': self.email or '', 'status': self.status,
                'value': self.value, 'source': self.source}


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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name     = request.form['name'].strip()
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'warning')
            return redirect(url_for('signup'))
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'warning')
            return redirect(url_for('signup'))
        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash(f'Welcome, {name}! Your account is ready.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('auth/signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        remember = request.form.get('remember') == 'on'
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=remember)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('auth/login.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── Pricing & Subscriptions ───────────────────────────────────────────────────

@app.route('/pricing')
def pricing():
    return render_template('pricing.html',
                           stripe_public_key=STRIPE_PUBLIC_KEY,
                           stripe_price_id=STRIPE_PRICE_ID)


@app.route('/subscribe/checkout', methods=['POST'])
@login_required
def subscribe_checkout():
    if not STRIPE_ENABLED or not stripe.api_key or not STRIPE_PRICE_ID:
        flash('Payments are not configured yet.', 'warning')
        return redirect(url_for('pricing'))
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',
            customer_email=current_user.email,
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            success_url=url_for('subscribe_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('pricing', _external=True),
            metadata={'user_id': current_user.id},
        )
        return redirect(session.url)
    except Exception as e:
        flash(f'Payment error: {e}', 'danger')
        return redirect(url_for('pricing'))


@app.route('/subscribe/success')
@login_required
def subscribe_success():
    session_id = request.args.get('session_id')
    if session_id and STRIPE_ENABLED and stripe.api_key:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            current_user.plan = 'pro'
            current_user.stripe_customer_id = sess.customer
            current_user.stripe_subscription_id = sess.subscription
            db.session.commit()
        except Exception:
            pass
    flash('You are now on the Pro plan — enjoy unlimited leads!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload    = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    if not STRIPE_ENABLED:
        abort(400)
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)
    if event['type'] == 'customer.subscription.deleted':
        sub  = event['data']['object']
        user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
        if user:
            user.plan = 'free'
            db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/account')
@login_required
def account():
    return render_template('account.html')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    today  = date.today()
    leads  = Lead.query.filter_by(user_id=current_user.id).order_by(Lead.updated_at.desc()).all()
    lead_ids = [l.id for l in leads]

    status_counts  = {s: 0 for s in STATUSES}
    pipeline_value = 0.0
    for lead in leads:
        status_counts[lead.status] = status_counts.get(lead.status, 0) + 1
        if lead.status not in ('Won', 'Lost'):
            pipeline_value += lead.value or 0

    base_fu = FollowUp.query.filter(FollowUp.lead_id.in_(lead_ids)) if lead_ids else FollowUp.query.filter(False)
    overdue  = base_fu.filter(FollowUp.completed == False, FollowUp.due_date < today).order_by(FollowUp.due_date).all()
    upcoming = base_fu.filter(FollowUp.completed == False, FollowUp.due_date >= today).order_by(FollowUp.due_date).limit(10).all()

    return render_template('dashboard.html',
        status_counts=status_counts, statuses=STATUSES,
        pipeline_value=pipeline_value, total_leads=len(leads),
        overdue=overdue, upcoming=upcoming, recent_leads=leads[:5], today=today,
    )


# ── Leads ─────────────────────────────────────────────────────────────────────

@app.route('/leads')
@login_required
def leads():
    q      = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    source = request.args.get('source', '')
    query  = Lead.query.filter_by(user_id=current_user.id)
    if q:
        like  = f'%{q}%'
        query = query.filter(db.or_(Lead.name.ilike(like), Lead.company.ilike(like), Lead.email.ilike(like)))
    if status:
        query = query.filter(Lead.status == status)
    if source:
        query = query.filter(Lead.source == source)
    leads_list = query.order_by(Lead.updated_at.desc()).all()
    return render_template('leads.html', leads=leads_list, statuses=STATUSES,
                           sources=SOURCES, q=q, filter_status=status, filter_source=source)


@app.route('/leads/new', methods=['GET', 'POST'])
@login_required
def lead_new():
    if request.method == 'POST':
        if not current_user.can_add_lead:
            flash(f'Free plan is limited to {FREE_LEAD_LIMIT} leads. Upgrade to Pro for unlimited.', 'warning')
            return redirect(url_for('pricing'))
        lead = Lead(
            user_id = current_user.id,
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
            db.session.add(FollowUp(lead_id=lead.id,
                                    due_date=dateparser.parse(due_raw).date(),
                                    note=request.form.get('followup_note', '').strip()))
            db.session.commit()
        flash(f'Lead "{lead.name}" created.', 'success')
        return redirect(url_for('lead_detail', lead_id=lead.id))
    return render_template('lead_form.html', lead=None, statuses=STATUSES, sources=SOURCES,
                           today=date.today().isoformat())


@app.route('/leads/<int:lead_id>')
@login_required
def lead_detail(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    return render_template('lead_detail.html', lead=lead, today=date.today(), statuses=STATUSES)


@app.route('/leads/<int:lead_id>/edit', methods=['GET', 'POST'])
@login_required
def lead_edit(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    if request.method == 'POST':
        lead.name       = request.form['name'].strip()
        lead.company    = request.form.get('company', '').strip()
        lead.email      = request.form.get('email', '').strip()
        lead.phone      = request.form.get('phone', '').strip()
        lead.source     = request.form.get('source', lead.source)
        lead.status     = request.form.get('status', lead.status)
        lead.value      = float(request.form.get('value') or 0)
        lead.notes      = request.form.get('notes', '').strip()
        lead.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Lead "{lead.name}" updated.', 'success')
        return redirect(url_for('lead_detail', lead_id=lead.id))
    return render_template('lead_form.html', lead=lead, statuses=STATUSES, sources=SOURCES,
                           today=date.today().isoformat())


@app.route('/leads/<int:lead_id>/delete', methods=['POST'])
@login_required
def lead_delete(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    name = lead.name
    db.session.delete(lead)
    db.session.commit()
    flash(f'Lead "{name}" deleted.', 'info')
    return redirect(url_for('leads'))


@app.route('/leads/<int:lead_id>/status', methods=['POST'])
@login_required
def lead_status(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    new_status = request.form.get('status')
    if new_status in STATUSES:
        lead.status     = new_status
        lead.updated_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('lead_detail', lead_id=lead_id))


# ── Follow-ups ────────────────────────────────────────────────────────────────

@app.route('/leads/<int:lead_id>/followups', methods=['POST'])
@login_required
def followup_add(lead_id):
    Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    due_raw = request.form.get('due_date', '').strip()
    if not due_raw:
        flash('Please provide a due date.', 'warning')
        return redirect(url_for('lead_detail', lead_id=lead_id))
    db.session.add(FollowUp(lead_id=lead_id,
                            due_date=dateparser.parse(due_raw).date(),
                            note=request.form.get('note', '').strip()))
    db.session.commit()
    flash('Follow-up scheduled.', 'success')
    return redirect(url_for('lead_detail', lead_id=lead_id))


@app.route('/followups/<int:fu_id>/complete', methods=['POST'])
@login_required
def followup_complete(fu_id):
    fu = FollowUp.query.get_or_404(fu_id)
    Lead.query.filter_by(id=fu.lead_id, user_id=current_user.id).first_or_404()
    fu.completed = True
    db.session.commit()
    return redirect(url_for('lead_detail', lead_id=fu.lead_id))


@app.route('/followups/<int:fu_id>/delete', methods=['POST'])
@login_required
def followup_delete(fu_id):
    fu = FollowUp.query.get_or_404(fu_id)
    Lead.query.filter_by(id=fu.lead_id, user_id=current_user.id).first_or_404()
    lead_id = fu.lead_id
    db.session.delete(fu)
    db.session.commit()
    return redirect(url_for('lead_detail', lead_id=lead_id))


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/leads')
@login_required
def api_leads():
    q     = request.args.get('q', '').strip()
    query = Lead.query.filter_by(user_id=current_user.id)
    if q:
        like  = f'%{q}%'
        query = query.filter(db.or_(Lead.name.ilike(like), Lead.company.ilike(like)))
    return jsonify([l.to_dict() for l in query.limit(20).all()])


# ── CLI seed ──────────────────────────────────────────────────────────────────

@app.cli.command('seed')
def seed():
    """Create a demo account with sample leads."""
    user = User.query.filter_by(email='demo@solocrm.com').first()
    if not user:
        user = User(name='Demo User', email='demo@solocrm.com')
        user.set_password('demo1234')
        db.session.add(user)
        db.session.commit()

    samples = [
        dict(name='Alice Johnson', company='Bloom Design', email='alice@bloomdesign.io',
             phone='555-0101', source='Referral', status='Qualified', value=4500,
             notes='Needs a brand refresh.'),
        dict(name='Bob Martinez', company='SquarePeg SaaS', email='bob@squarepeg.com',
             phone='555-0102', source='LinkedIn', status='Proposal Sent', value=12000,
             notes='Awaiting CFO sign-off.'),
        dict(name='Carol Smith', company='', email='carol@gmail.com',
             phone='555-0103', source='Website', status='New', value=800,
             notes='Interested in monthly retainer.'),
    ]
    today = date.today()
    for s in samples:
        if not Lead.query.filter_by(user_id=user.id, email=s['email']).first():
            lead = Lead(user_id=user.id, **s)
            db.session.add(lead)
            db.session.flush()
            db.session.add(FollowUp(lead_id=lead.id,
                                    due_date=today + timedelta(days=2),
                                    note='Initial check-in'))
    db.session.commit()
    print('Demo: demo@solocrm.com / demo1234')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
