from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask import send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import selectinload
import datetime
import os
import time
import logging
from werkzeug.utils import secure_filename

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


def _normalize_database_url(raw_url: str) -> str:
    """Ensure SQLAlchemy can understand the database URL."""
    if not raw_url:
        return raw_url

    if raw_url.startswith("prisma+postgres://"):
        raw_url = raw_url.replace("prisma+postgres://", "postgresql+psycopg://", 1)
    elif raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return raw_url


def _get_database_uri() -> str:
    """Pick the first configured Postgres URL, fail fast if none is set."""
    candidate_keys = (
        "DATABASE_URL",
        "POSTGRES_URL",
        "POSTGRES_URL_NON_POOLING",
        "POSTGRES_URL_NO_SSL",
        "POSTGRES_PRISMA_URL",
        "PRISMA_DATABASE_URL",
    )
    for key in candidate_keys:
        value = os.environ.get(key)
        if value:
            normalized = _normalize_database_url(value)
            if normalized:
                return normalized

    raise RuntimeError(
        "No Postgres connection string found. Set one of: "
        f"{', '.join(candidate_keys)}."
    )


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = _get_database_uri()
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
}
app.config['SQLALCHEMY_SESSION_OPTIONS'] = {
    "expire_on_commit": False,  # keep objects warm across commits for serverless latency
}
db = SQLAlchemy(app)
app.logger.setLevel(logging.INFO)
app.jinja_env.globals['current_year'] = lambda: datetime.datetime.utcnow().year
try:
	app_requests = __import__("requests")
except Exception:
	app_requests = None

# Gallery configuration
ALLOWED_MEDIA_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'mov', 'avi'}
GALLERY_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images')

def allowed_media_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_MEDIA_EXTENSIONS
 
# Try to ensure gallery year folders exist (ignore errors on read-only FS)
for _year in ('2025', '2026'):
    try:
        os.makedirs(os.path.join(GALLERY_ROOT, _year), exist_ok=True)
    except OSError:
        pass

# ---------------
# Vercel Blob (read-only integration for gallery)
# Required env vars:
# - BLOB_READ_TOKEN: Vercel Blob read or read/write token
# - BLOB_PREFIX: prefix/path for images, e.g. "cfc-images/images"
# Notes: We list blobs by prefix "{BLOB_PREFIX}/{year}/" and use returned 'url' to render.
VERCEL_BLOB_READ_TOKEN = os.environ.get('BLOB_READ_TOKEN')
BLOB_PREFIX = os.environ.get('BLOB_PREFIX', 'images')

def _list_vercel_blobs_for_year(year: int):
    """Return list of dicts: {name, url, uploaded_at} for files under prefix."""
    if not VERCEL_BLOB_READ_TOKEN or app_requests is None:
        return []
    prefix = f"{BLOB_PREFIX}/{year}/"
    try:
        resp = app_requests.get(
            "https://api.vercel.com/v2/blob",
            params={"limit": 1000, "prefix": prefix},
            headers={"Authorization": f"Bearer {VERCEL_BLOB_READ_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("blobs", []) or data.get("items", []) or []
        results = []
        for it in items:
            # fields vary slightly by API version; handle both
            url = it.get("url")
            pathname = it.get("pathname") or it.get("key") or ""
            name = os.path.basename(pathname) if pathname else ""
            if not name or not allowed_media_file(name):
                continue
            uploaded_at = it.get("uploadedAt") or it.get("createdAt")
            results.append({"name": name, "url": url, "uploaded_at": uploaded_at})
        return results
    except Exception:
        return []

 

# Custom filter to format date as "DD MMM YYYY"
@app.template_filter('format_date')
def format_date(value, format='%d %b %Y'):
    try:
        # If value is already a date, use it; otherwise, parse from string.
        if isinstance(value, datetime.date):
            dt = value
        else:
            dt = datetime.datetime.strptime(value, '%Y-%m-%d').date()
        return dt.strftime(format)
    except Exception as e:
        return value
    
# Player details db
class PlayerInfo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    preferred_position = db.Column(db.String(50), nullable=True)
    shirt_number = db.Column(db.String(10), nullable=True)
    beer_duty_date = db.Column(db.Date, nullable=True)
    support_offered = db.Column(db.Text, nullable=True)  # New column for support notes

    def __repr__(self):
        return f'<PlayerInfo {self.name}>'


# Define the Event model. All events are matches.
class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(10), nullable=False)  # Expected "HH:MM" format
    field = db.Column(db.String(100), nullable=False)
    opponent = db.Column(db.String(100), nullable=True)
    type = db.Column(db.String(20), nullable=False, default='match')  # Always "match"
    lineup = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=lambda: {
        "Striker": "", "Left Wing": "", "Right Wing": "", "Attacking Mid": "",
        "Defensive Mid 1": "", "Defensive Mid 2": "", "Right Back": "", "Left Back": "",
        "Centre Back 1": "", "Centre Back 2": "", "Goalkeeper": "", "Away": "",
        "Sub 1": "", "Sub 2": "", "Sub 3": "", "Sub 4": "", "Beer Duty": ""
    })
    result = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=lambda: {
        "home_score": "",
        "away_score": "",
        "goal_scorers": [],
        "assists": [],          # ← added assists array
        "cards": {"yellow": [], "red": []}
    })

    def __repr__(self):
        return f'<Event {self.id} on {self.date}>'

    
# Create database tables if they don't exist
with app.app_context():
    db.create_all()


# Simple timing helper so we can inspect latency in Vercel logs.
def _log_duration(label: str, start_time: float) -> float:
    elapsed_ms = (time.perf_counter() - start_time) * 1000
    app.logger.info("%s took %.2f ms", label, elapsed_ms)
    return elapsed_ms


 
def _ensure_player_stat_rows(players):
    """Batch ensure PlayerStat rows exist without per-player queries."""
    existing_stats = PlayerStat.query.all()
    stats_by_name = {stat.player: stat for stat in existing_stats}

    missing_stats = [
        PlayerStat(player=player.name)
        for player in players
        if player.name not in stats_by_name
    ]
    created = bool(missing_stats)

    if created:
        # combined multiple per-player inserts into one batch write
        db.session.add_all(missing_stats)
        db.session.flush()
        for stat in missing_stats:
            stats_by_name[stat.player] = stat

    return stats_by_name, created


def _ensure_season_stat_rows(players, season_year: int):
    """Batch ensure SeasonStat rows exist per season."""
    if not players:
        return {}, False

    player_ids = [player.id for player in players]
    existing_stats = (
        SeasonStat.query.options(selectinload(SeasonStat.player))
        .filter(
            SeasonStat.season_year == season_year,
            SeasonStat.player_id.in_(player_ids),
        )
        .all()
    )
    stats_by_player_id = {stat.player_id: stat for stat in existing_stats}

    missing_stats = []
    for player in players:
        if player.id not in stats_by_player_id:
            stat = SeasonStat(player_id=player.id, season_year=season_year)
            stat.player = player
            stats_by_player_id[player.id] = stat
            missing_stats.append(stat)
    created = bool(missing_stats)

    if created:
        # avoid one insert per player by batching the new season rows
        db.session.add_all(missing_stats)
        db.session.flush()

    return stats_by_player_id, created


def _sort_player_stats(stats, field, descending):
    if field == 'player':
        key_fn = lambda stat: stat.player.lower()
    else:
        key_fn = lambda stat: getattr(stat, field, 0)
    return sorted(stats, key=key_fn, reverse=descending)


def _sort_season_stats(stats, field, descending):
    if field == 'player':
        key_fn = lambda stat: (stat.player.name.lower() if stat.player else '')
    else:
        key_fn = lambda stat: getattr(stat, field, 0)
    return sorted(stats, key=key_fn, reverse=descending)


# Helper function to get the next match event (today or later)
def get_next_match_event():
    today = datetime.date.today()
    return Event.query.filter(Event.type == 'match', Event.date >= today).order_by(Event.date.asc()).first()

# ----------------- Authentication Routes -----------------
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            flash("Invalid credentials. Please try again.")
    return render_template("login.html")


@app.route('/guest')
def guest():
    session['guest'] = True
    return redirect(url_for('home'))


@app.route('/home')
def home():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    return render_template("home.html")


# ----------------- Game Day Route (Read-Only) -----------------

@app.route('/game-day')
def game_day():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    event = get_next_match_event()
    if not event:
        flash("No upcoming game found in the schedule.")
        return render_template("game_day.html", event=None)
    
    # Lookup player assigned for beer duty on the event date:
    beer_duty_player = PlayerInfo.query.filter_by(beer_duty_date=event.date).first()
    
    return render_template("game_day.html", event=event, beer_duty_player=beer_duty_player)



# ----------------- Player Details Routes -----------------

@app.route('/players', methods=['GET', 'POST'])
def players():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))

    if request.method == 'POST' and session.get('logged_in'):
        for player in PlayerInfo.query.all():
            # Store the old name before updating
            old_name = player.name
            new_name = request.form.get(f'name_{player.id}', player.name)
            if new_name != old_name:
                # Update the corresponding PlayerStat record if it exists
                player_stat = PlayerStat.query.filter_by(player=old_name).first()
                if player_stat:
                    player_stat.player = new_name
            player.name = new_name

            # Update other fields
            player.preferred_position = request.form.get(f'preferred_position_{player.id}', '')
            player.shirt_number = request.form.get(f'shirt_{player.id}', '')
            
            # Update beer duty date:
            date_str = request.form.get(f'beer_duty_date_{player.id}', '')
            if date_str:
                try:
                    player.beer_duty_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                except ValueError:
                    player.beer_duty_date = None
            else:
                player.beer_duty_date = None

            # Update support offered notes
            player.support_offered = request.form.get(f'support_offered_{player.id}', '')
            
        db.session.commit()
        flash("Player details updated.")
        return redirect(url_for('players'))


    players = PlayerInfo.query.order_by(PlayerInfo.name).all()
    return render_template('players.html', players=players)



@app.route('/players/add', methods=['POST'])
def add_player():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    name = request.form.get('new_player_name').strip()
    if name and not PlayerInfo.query.filter_by(name=name).first():
        db.session.add(PlayerInfo(name=name))
        db.session.commit()
        flash(f"Added {name}.")
    return redirect(url_for('players'))


@app.route('/players/delete/<int:player_id>')
def delete_player(player_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    player = PlayerInfo.query.get_or_404(player_id)
    player_name = player.name
    db.session.delete(player)
    
    # Delete the corresponding PlayerStat record
    player_stat = PlayerStat.query.filter_by(player=player_name).first()
    if player_stat:
        db.session.delete(player_stat)

    # Delete any season stats linked to the player
    SeasonStat.query.filter_by(player_id=player_id).delete()
    
    db.session.commit()

    preferred_year_arg = request.args.get('stats_year')
    try:
        preferred_year = int(preferred_year_arg) if preferred_year_arg else None
    except ValueError:
        preferred_year = None
    flash(f"Deleted {player_name}.")
    return redirect(url_for('players'))



# ----------------- Schedule Routes -----------------
@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    # Only allow adding events if admin is logged in.
    if request.method == "POST":
        if not session.get('logged_in'):
            flash("Only admin can add events.")
            return redirect(url_for('schedule'))
        try:
            new_date = datetime.datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DD.")
            return redirect(url_for('schedule'))
        new_event = Event(
            date=new_date,
            time=request.form.get('time'),
            field=request.form.get('field'),
            opponent=request.form.get('opponent', ''),
            type='match',
                lineup={pos: '' for pos in [
                    "Striker", "Left Wing", "Right Wing", "Attacking Mid",
                    "Defensive Mid 1", "Defensive Mid 2", "Right Back", "Left Back",
                    "Centre Back 1", "Centre Back 2", "Goalkeeper",
                    "Sub 1", "Sub 2", "Sub 3", "Sub 4", "Sub 5",
                    "Away"
                ]},
            result={"home_score": "", "away_score": "", "goal_scorers": [], "cards": {"yellow": [], "red": []}}
        )
        db.session.add(new_event)
        db.session.commit()
        flash("New event added successfully.")
        return redirect(url_for('schedule'))
    events = Event.query.order_by(Event.date.asc()).all()
    events_by_year = {}
    for event in events:
        event_date = event.date
        if not isinstance(event_date, datetime.date):
            try:
                event_date = datetime.datetime.strptime(str(event.date), '%Y-%m-%d').date()
            except ValueError:
                continue
        events_by_year.setdefault(event_date.year, []).append(event)
    now = datetime.date.today()
    return render_template(
        "schedule.html",
        schedule_data=events,
        events_by_year=events_by_year,
        now=now
    )


@app.route('/schedule/edit/<int:event_id>', methods=['GET', 'POST'])
def edit_schedule(event_id):
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    if not session.get('logged_in'):
        flash("Only admin can edit events.")
        return redirect(url_for('schedule'))

    event = Event.query.get_or_404(event_id)
    players = PlayerInfo.query.order_by(PlayerInfo.name).all()  # Get players from DB

    if request.method == "POST":
        try:
            event.date = datetime.datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DD.")
            return redirect(url_for('edit_schedule', event_id=event_id))

        event.time = request.form.get('time')
        event.field = request.form.get('field')
        event.opponent = request.form.get('opponent', '')

        selected_players = set()
        new_lineup = {}

        # Handle all positions except "Away"
        for pos in [
            "Striker", "Left Wing", "Right Wing", "Attacking Mid",
            "Defensive Mid 1", "Defensive Mid 2", "Right Back", "Left Back",
            "Centre Back 1", "Centre Back 2", "Goalkeeper",
            "Sub 1", "Sub 2", "Sub 3", "Sub 4", "Sub 5",
            "Beer Duty"
        ]:
            player = request.form.get(pos, '')
            new_lineup[pos] = player
            if player:
                selected_players.add(player)

        # Dynamically populate "Away" with unselected players
        all_players = [p.name for p in players]
        away_players = [p for p in all_players if p not in selected_players]
        new_lineup["Away"] = ",".join(away_players)

        event.lineup = new_lineup
        db.session.commit()
        flash("Event updated successfully.")
        return redirect(url_for('schedule'))

    return render_template("edit_schedule.html", event=event, players=players)



@app.route('/schedule/delete/<int:event_id>')
def delete_schedule(event_id):
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    # Only allow deletion if admin is logged in.
    if not session.get('logged_in'):
        flash("Only admin can delete events.")
        return redirect(url_for('schedule'))
    event = Event.query.get_or_404(event_id)
    db.session.delete(event)
    db.session.commit()
    flash("Event deleted successfully.")
    return redirect(url_for('schedule'))


# ----------------- Results Routes -----------------
@app.route('/results')
def results():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    match_events = Event.query.filter_by(type='match').order_by(Event.date.asc()).all()
    events_by_year = {}
    for event in match_events:
        event_date = event.date
        if not isinstance(event_date, datetime.date):
            try:
                event_date = datetime.datetime.strptime(str(event.date), '%Y-%m-%d').date()
            except ValueError:
                continue
        events_by_year.setdefault(event_date.year, []).append(event)
    now = datetime.date.today()
    return render_template(
        "results.html",
        match_events=match_events,
        events_by_year=events_by_year,
        now=now
    )

@app.route('/results/edit/<int:event_id>', methods=['GET', 'POST'])
def edit_result(event_id):
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))

    event = Event.query.get_or_404(event_id)
    if not event.result:
        event.result = {
            "home_score": "",
            "away_score": "",
            "goal_scorers": [],
            "assists": [],            # ← ensure assists exists
            "cards": {"yellow": [], "red": []}
        }

    if request.method == "POST":
        new_result = {}
        new_result['home_score'] = request.form.get('home_score', '')
        new_result['away_score'] = request.form.get('away_score', '')

        # Process goal scorers
        num_goal_scorers = int(request.form.get('num_goal_scorers', 0))
        goal_scorers = []
        for i in range(1, num_goal_scorers + 1):
            player = request.form.get(f'goal_scorer_{i}', '')
            goals  = request.form.get(f'goal_count_{i}', '')
            if player and goals:
                goal_scorers.append({'player': player, 'goals': int(goals)})
        new_result['goal_scorers'] = goal_scorers

        # Process assists
        num_assists = int(request.form.get('num_assists', 0))
        assist_list = []
        for i in range(1, num_assists + 1):
            player = request.form.get(f'assist_player_{i}', '')
            count  = request.form.get(f'assist_count_{i}', '')
            if player and count:
                assist_list.append({'player': player, 'assists': int(count)})
        new_result['assists'] = assist_list

        # Process yellow cards
        num_yellow = int(request.form.get('num_yellow_cards', 0))
        yellow_cards = []
        for i in range(1, num_yellow + 1):
            player = request.form.get(f'yellow_card_{i}', '')
            if player:
                yellow_cards.append(player)

        # Process red cards
        num_red = int(request.form.get('num_red_cards', 0))
        red_cards = []
        for i in range(1, num_red + 1):
            player = request.form.get(f'red_card_{i}', '')
            if player:
                red_cards.append(player)

        new_result['cards'] = {"yellow": yellow_cards, "red": red_cards}

        event.result = new_result
        db.session.commit()
        flash("Result updated successfully.")
        return redirect(url_for('results'))

    players = PlayerInfo.query.order_by(PlayerInfo.name).all()
    return render_template("edit_result.html", event=event, players=players)


@app.route('/results/delete/<int:event_id>', methods=['POST'])
def delete_result(event_id):
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))

    if not session.get('logged_in'):
        flash("Only admin can delete match results.")
        return redirect(url_for('results'))

    event = Event.query.get_or_404(event_id)
    event.result = {
        "home_score": "",
        "away_score": "",
        "goal_scorers": [],
        "assists": [],
        "cards": {"yellow": [], "red": []}
    }
    db.session.commit()
    flash("Match result deleted.")
    return redirect(url_for('results'))


# Add this model definition below your Event model
class PlayerStat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player = db.Column(db.String(100), unique=True, nullable=False)
    goals = db.Column(db.Integer, default=0)
    assists = db.Column(db.Integer, default=0)
    player_of_match = db.Column(db.Integer, default=0)
    clean_sheets = db.Column(db.Integer, default=0)
    yellow_cards = db.Column(db.Integer, default=0)
    red_cards = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<PlayerStat {self.player}>'


class SeasonStat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player_info.id'), nullable=False)
    season_year = db.Column(db.Integer, nullable=False)
    goals = db.Column(db.Integer, default=0)
    assists = db.Column(db.Integer, default=0)
    player_of_match = db.Column(db.Integer, default=0)
    yellow_cards = db.Column(db.Integer, default=0)
    red_cards = db.Column(db.Integer, default=0)

    player = db.relationship('PlayerInfo', backref='season_stats', lazy=True)

    __table_args__ = (db.UniqueConstraint('player_id', 'season_year', name='uq_player_season'),)

    def __repr__(self):
        return f'<SeasonStat {self.season_year} {self.player_id}>'


# Make sure to create tables for new models as well
with app.app_context():
    db.create_all()

# Stats route
@app.route('/stats', methods=['GET', 'POST'])
def stats():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))

    route_start = time.perf_counter()
    db_section_start = time.perf_counter()

    # Load all players once; hooks cache ensures we don't re-query inside loops.
    all_players = PlayerInfo.query.order_by(PlayerInfo.name).all()
    player_stats_by_name, created_player_rows = _ensure_player_stat_rows(all_players)
    season_stats_by_player_id, created_season_rows = _ensure_season_stat_rows(all_players, 2026)
    db_changed = created_player_rows or created_season_rows

    player_stats = list(player_stats_by_name.values())
    season_stats_2026 = list(season_stats_by_player_id.values())

    # If 2026 season rows were just created, initialize them by copying 2025 totals
    if created_season_rows:
        for season_stat in season_stats_2026:
            base_2025 = player_stats_by_name.get(season_stat.player.name)
            if base_2025:
                season_stat.goals = base_2025.goals
                season_stat.assists = base_2025.assists
                season_stat.player_of_match = base_2025.player_of_match
                season_stat.yellow_cards = base_2025.yellow_cards
                season_stat.red_cards = base_2025.red_cards
        db_changed = True

    _log_duration("stats.db_setup", db_section_start)

    preferred_year_arg = request.args.get('stats_year')
    try:
        preferred_year = int(preferred_year_arg) if preferred_year_arg else None
    except (TypeError, ValueError):
        preferred_year = None

    # Sorting logic (2025 table)
    allowed_fields = ['player', 'goals', 'assists', 'player_of_match', 'yellow_cards', 'red_cards']
    sort_field = request.args.get('sort', 'player')
    if sort_field not in allowed_fields:
        sort_field = 'player'
    current_order = request.args.get('order', 'asc')
    next_order = 'desc' if current_order == 'asc' else 'asc'

    player_stats = _sort_player_stats(player_stats, sort_field, current_order == 'desc')

    # Sorting logic (2026 table)
    season_allowed_fields = ['player', 'goals', 'assists', 'player_of_match', 'yellow_cards', 'red_cards']
    season_sort_field = request.args.get('season_sort', 'player')
    if season_sort_field not in season_allowed_fields:
        season_sort_field = 'player'
    season_current_order = request.args.get('season_order', 'asc')
    season_next_order = 'desc' if season_current_order == 'asc' else 'asc'

    season_stats_2026 = _sort_season_stats(season_stats_2026, season_sort_field, season_current_order == 'desc')

    # Update form (admin only)
    if request.method == 'POST' and session.get('logged_in'):
        season_year = request.form.get('season_year', '2025')
        if season_year == '2026':
            for player in all_players:
                stat = season_stats_by_player_id.get(player.id)
                try:
                    stat.goals = int(request.form.get(f"goals_{player.id}", 0))
                    stat.assists = int(request.form.get(f"assists_{player.id}", 0))
                    stat.player_of_match = int(request.form.get(f"player_of_match_{player.id}", 0))
                    stat.yellow_cards = int(request.form.get(f"yellow_cards_{player.id}", 0))
                    stat.red_cards = int(request.form.get(f"red_cards_{player.id}", 0))
                except ValueError:
                    pass
            db.session.commit()
            _log_duration("stats.db_update.2026", db_section_start)
            flash("2026 stats updated successfully.")
            return redirect(url_for('stats', stats_year=2026))

        for stat in player_stats:
            try:
                stat.goals = int(request.form.get(f"goals_{stat.id}", 0))
                stat.assists = int(request.form.get(f"assists_{stat.id}", 0))
                stat.player_of_match = int(request.form.get(f"player_of_match_{stat.id}", 0))
                stat.yellow_cards = int(request.form.get(f"yellow_cards_{stat.id}", 0))
                stat.red_cards = int(request.form.get(f"red_cards_{stat.id}", 0))
            except ValueError:
                pass  # Ignore bad input silently
        db.session.commit()
        _log_duration("stats.db_update.2025", db_section_start)
        flash("Stats updated successfully.")
        return redirect(url_for('stats', stats_year=season_year))

    now = datetime.date.today()
    context = dict(
        player_stats=player_stats,
        sort_field=sort_field,
        next_order=next_order,
        current_order=current_order,
        now=now,
        all_players=all_players,
        season_stats_2026=season_stats_2026,
        season_sort_field=season_sort_field,
        season_current_order=season_current_order,
        season_next_order=season_next_order,
        preferred_year=preferred_year,
    )

    if db_changed:
        db.session.commit()

    _log_duration("stats.total", route_start)
    return render_template("stats.html", **context)


# ----------------- logout Routes -----------------


@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.")
    return redirect(url_for('login'))

# ----------------- Links Routes -----------------


@app.route('/links')
def links():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    return render_template("links.html")

# ----------------- Store (Merch) Routes -----------------
# Removed per request: merch store features are no longer supported.

# ----------------- Gallery Routes -----------------

@app.route('/gallery')
def gallery():
    if not (session.get('logged_in') or session.get('guest')):
        return redirect(url_for('login'))
    # Default to 2025 tab
    try:
        year = int(request.args.get('year', 2025))
    except (TypeError, ValueError):
        year = 2025
    year = 2025 if year not in (2025, 2026) else year

    # Prefer Vercel Blob listing when configured; otherwise fall back to bundled files
    media_files = []
    blob_items = _list_vercel_blobs_for_year(year)
    if blob_items:
        for it in blob_items:
            version = int(time.time())
            try:
                # uploaded_at may be ISO string; we only need a cache-busting int
                if isinstance(it.get("uploaded_at"), (int, float)):
                    version = int(it["uploaded_at"])
            except Exception:
                pass
        # Build list suitable for templates; include 'url' directly
        media_files = [{"name": it["name"], "v": version, "url": it.get("url")} for it in blob_items]
    else:
        year_dir = os.path.join(GALLERY_ROOT, str(year))
        if os.path.isdir(year_dir):
            for name in sorted(os.listdir(year_dir)):
                if allowed_media_file(name):
                    try:
                        stat = os.stat(os.path.join(year_dir, name))
                        version = int(stat.st_mtime)
                    except OSError:
                        version = int(time.time())
                    media_files.append({"name": name, "v": version})

    return render_template(
        "gallery.html",
        active_year=year,
        media_files=media_files
    )


@app.route('/gallery/media/<int:year>/<path:filename>')
def gallery_media(year, filename):
    if year not in (2025, 2026):
        abort(404)
    # If available on Vercel Blob, redirect to its public URL
    blob_items = _list_vercel_blobs_for_year(year)
    if blob_items:
        for it in blob_items:
            if it.get("name") == filename and it.get("url"):
                return redirect(it["url"])
    # Fallback to bundled files
    directory = os.path.join(GALLERY_ROOT, str(year))
    response = send_from_directory(directory, filename, max_age=0)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/gallery/upload', methods=['POST'])
def gallery_upload():
    if not session.get('logged_in'):
        flash("Only admin can upload media.")
        return redirect(url_for('gallery'))
    try:
        year = int(request.form.get('year', 2025))
    except (TypeError, ValueError):
        year = 2025
    year = 2025 if year not in (2025, 2026) else year

    if 'file' not in request.files:
        flash("No file part in the request.")
        return redirect(url_for('gallery', year=year))
    file = request.files['file']
    if file.filename == '':
        flash("No file selected.")
        return redirect(url_for('gallery', year=year))
    if file and allowed_media_file(file.filename):
        safe_name = secure_filename(file.filename)
        save_dir = os.path.join(GALLERY_ROOT, str(year))
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError:
            flash("Uploads are disabled on this deployment (read-only filesystem).")
            return redirect(url_for('gallery', year=year))
        try:
            save_path = os.path.join(save_dir, safe_name)
            file.save(save_path)
        except Exception:
            flash("Failed to save file (filesystem not writable).")
            return redirect(url_for('gallery', year=year))
        else:
            flash("Upload successful.")
            return redirect(url_for('gallery', year=year))
    else:
        flash("Unsupported file type.")
        return redirect(url_for('gallery', year=year))

@app.route('/gallery/delete', methods=['POST'])
def gallery_delete():
    if not session.get('logged_in'):
        flash("Only admin can delete media.")
        return redirect(url_for('gallery'))
    try:
        year = int(request.form.get('year', 2025))
    except (TypeError, ValueError):
        year = 2025
    year = 2025 if year not in (2025, 2026) else year

    filename = request.form.get('name', '')
    if not filename or not allowed_media_file(filename):
        flash("Invalid file.")
        return redirect(url_for('gallery', year=year))
    # Only delete within the intended directory
    target_dir = os.path.join(GALLERY_ROOT, str(year))
    target_path = os.path.join(target_dir, filename)
    if not os.path.abspath(target_path).startswith(os.path.abspath(target_dir)):
        flash("Invalid path.")
        return redirect(url_for('gallery', year=year))
    try:
        if os.path.exists(target_path):
            os.remove(target_path)
            flash("Media deleted.")
        else:
            flash("File not found.")
    except OSError:
        flash("Could not delete file.")
    return redirect(url_for('gallery', year=year))


if __name__ == '__main__':
    app.run(debug=True)
