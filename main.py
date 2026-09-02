import hashlib
import hmac
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("M17_DB_PATH", str(BASE_DIR / "m17_clients.db")))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
COACH_IDS = {x.strip() for x in os.getenv("COACH_TELEGRAM_IDS", "").split(",") if x.strip()}
CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
APP_VERSION = "0.5.4"

app = FastAPI(title="M17 Training System", version=APP_VERSION)


class Goal(str, Enum):
    ironman = "ironman"
    half_ironman = "half_ironman"
    olympic = "olympic"
    sprint = "sprint"
    running = "running"
    crossfit = "crossfit"
    fat_loss = "fat_loss"
    muscle_gain = "muscle_gain"
    strength = "strength"


class Level(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    competitive = "competitive"


class Sex(str, Enum):
    male = "male"
    female = "female"
    other = "other"
    not_specified = "not_specified"


class ClientStatus(str, Enum):
    new = "NEW"
    waiting_approval = "WAITING_APPROVAL"
    assessment = "ASSESSMENT"
    active = "ACTIVE"
    payment_due = "PAYMENT_DUE"
    suspended = "SUSPENDED"
    paused = "PAUSED"
    rehab = "REHAB"
    completed = "COMPLETED"
    archive = "ARCHIVE"


class ClientType(str, Enum):
    new = "NEW_CLIENT"
    experienced = "EXPERIENCED"
    returning = "RETURNING"


class GoalItem(BaseModel):
    goal: Goal
    priority: int = Field(default=2, ge=1, le=3)
    event_date: Optional[str] = None
    target: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=400)


class ClientProfile(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    telegram_username: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    sex: Sex = Sex.not_specified
    age: int = Field(ge=14, le=90)
    height_cm: Optional[float] = Field(default=None, ge=120, le=230)
    weight_kg: Optional[float] = Field(default=None, ge=35, le=300)
    experience_years: float = Field(default=0, ge=0, le=60)
    client_type: ClientType = ClientType.new
    primary_goal: Goal
    goals: list[GoalItem] = Field(default_factory=list)
    body_focus: list[str] = Field(default_factory=list)
    goal_details: dict[str, Any] = Field(default_factory=dict)
    level: Level = Level.beginner
    training_days: list[int] = Field(default_factory=lambda: [0, 2, 4])
    day_time_min: dict[str, int] = Field(default_factory=dict)
    days_per_week: int = Field(default=3, ge=1, le=7)
    hours_per_week: float = Field(default=6, ge=1, le=30)
    weeks_to_event: Optional[int] = Field(default=None, ge=1, le=104)
    equipment: list[str] = Field(default_factory=list)
    limitations: Optional[str] = Field(default=None, max_length=1000)
    notes: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_goals(self):
        if not self.goals:
            self.goals = [GoalItem(goal=self.primary_goal, priority=1)]
        if self.primary_goal not in [g.goal for g in self.goals]:
            self.goals.insert(0, GoalItem(goal=self.primary_goal, priority=1))
        days = sorted({int(x) for x in self.training_days if 0 <= int(x) <= 6})
        if not days:
            days = [0, 2, 4]
        self.training_days = days
        self.days_per_week = len(days)
        return self


class SavedClientProfile(ClientProfile):
    status: ClientStatus = ClientStatus.waiting_approval
    coach_notes: Optional[str] = None
    created_at: str
    updated_at: str


class PlanRequest(BaseModel):
    goal: Goal
    level: Level = Level.beginner
    training_days: list[int] = Field(default_factory=lambda: [0, 2, 4])
    day_time_min: dict[str, int] = Field(default_factory=dict)
    days_per_week: int = Field(default=3, ge=1, le=7)
    hours_per_week: float = Field(default=6, ge=1, le=30)
    weeks_to_event: Optional[int] = Field(default=None, ge=1, le=104)
    age: Optional[int] = Field(default=None, ge=14, le=90)
    experience_years: Optional[float] = Field(default=None, ge=0, le=60)
    limitations: Optional[str] = Field(default=None, max_length=1000)


class TrainingBlock(BaseModel):
    name: str
    sessions: int
    priority: str
    note: str


class PlanPreview(BaseModel):
    goal: Goal
    phase: str
    total_sessions: int
    blocks: list[TrainingBlock]
    warning: Optional[str] = None
    profile_note: Optional[str] = None


class StatusUpdate(BaseModel):
    status: ClientStatus


class CoachNoteUpdate(BaseModel):
    coach_notes: str = Field(default="", max_length=4000)


class ProgressEntry(BaseModel):
    metric_type: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(default="", max_length=30)
    measured_at: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=500)


class WorkoutLog(BaseModel):
    workout_date: str = Field(default_factory=lambda: date.today().isoformat())
    workout_name: str = Field(default="Тренировка", max_length=150)
    completed: bool = True
    rpe: Optional[int] = Field(default=None, ge=1, le=10)
    wellbeing: Optional[int] = Field(default=None, ge=1, le=10)
    pain: bool = False
    duration_min: Optional[int] = Field(default=None, ge=0, le=1000)
    distance_km: Optional[float] = Field(default=None, ge=0, le=1000)
    avg_hr: Optional[int] = Field(default=None, ge=30, le=240)
    comment: Optional[str] = Field(default=None, max_length=1000)


class WorkoutFeedback(BaseModel):
    completion_status: str = Field(pattern="^(COMPLETED|PARTIAL|NOT_DONE)$")
    rpe: Optional[int] = Field(default=None, ge=1, le=10)
    wellbeing: int = Field(ge=1, le=10)
    pain: bool = False
    pain_location: Optional[str] = Field(default=None, max_length=300)
    reason: Optional[str] = Field(default=None, max_length=300)
    comment: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_rpe(self):
        if self.completion_status in {"COMPLETED", "PARTIAL"} and self.rpe is None:
            raise ValueError("RPE обязателен для выполненной или частично выполненной тренировки")
        return self


class ProgramCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=150)
    activate: bool = True
    generate_workouts: bool = True
    start_date: Optional[str] = None


class SubscriptionUpdate(BaseModel):
    start_date: Optional[str] = None
    paid_until: Optional[str] = None
    grace_until: Optional[str] = None
    amount: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="RUB", max_length=8)
    payment_status: str = Field(default="UNPAID", pattern="^(UNPAID|PAID|DUE|REFUNDED)$")
    access_enabled: bool = False


class VacationSettings(BaseModel):
    start_date: str
    end_date: str
    gym_type: str = Field(default="PARTIAL_GYM", pattern="^(NO_EQUIPMENT|MINIMAL|PARTIAL_GYM|FULL_GYM)$")
    equipment: list[str] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_dates(self):
        start = date.fromisoformat(self.start_date)
        end = date.fromisoformat(self.end_date)
        if end < start:
            raise ValueError("Дата окончания отпуска не может быть раньше даты начала")
        return self


class WorkoutCreate(BaseModel):
    workout_date: str
    title: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=10000)


class WorkoutEdit(BaseModel):
    workout_date: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=150)
    text: Optional[str] = Field(default=None, max_length=10000)


# WebSocket connections: client_id -> set of sockets
WS_CONNECTIONS: dict[str, set[WebSocket]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pg_sql(sql: str) -> str:
    return sql.replace("?", "%s")


class DBConnection:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        if USE_POSTGRES:
            if psycopg is None:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            if exc_type:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            self.conn.close()
        return False

    def execute(self, sql: str, params=()):
        return self.conn.execute(_pg_sql(sql) if USE_POSTGRES else sql, params)

    def commit(self):
        self.conn.commit()


def db_conn():
    return DBConnection()


def table_columns(conn, table: str) -> set[str]:
    if USE_POSTGRES:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
            (table,),
        ).fetchall()
        return {r["column_name"] for r in rows}
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                telegram_user_id TEXT PRIMARY KEY,
                telegram_username TEXT,
                name TEXT NOT NULL,
                sex TEXT NOT NULL,
                age INTEGER NOT NULL,
                height_cm REAL,
                weight_kg REAL,
                experience_years REAL NOT NULL,
                primary_goal TEXT NOT NULL,
                level TEXT NOT NULL,
                days_per_week INTEGER NOT NULL,
                hours_per_week REAL NOT NULL,
                weeks_to_event INTEGER,
                equipment_json TEXT NOT NULL,
                limitations TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'WAITING_APPROVAL',
                coach_notes TEXT,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                client_type TEXT NOT NULL DEFAULT 'NEW_CLIENT',
                goals_json TEXT NOT NULL DEFAULT '[]',
                body_focus_json TEXT NOT NULL DEFAULT '[]',
                goal_details_json TEXT NOT NULL DEFAULT '{}',
                training_days_json TEXT NOT NULL DEFAULT '[]',
                day_time_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        cols = table_columns(conn, "clients")
        migrations = {
            "status": "ALTER TABLE clients ADD COLUMN status TEXT NOT NULL DEFAULT 'WAITING_APPROVAL'",
            "coach_notes": "ALTER TABLE clients ADD COLUMN coach_notes TEXT",
            "created_at": "ALTER TABLE clients ADD COLUMN created_at TEXT",
            "client_type": "ALTER TABLE clients ADD COLUMN client_type TEXT NOT NULL DEFAULT 'NEW_CLIENT'",
            "goals_json": "ALTER TABLE clients ADD COLUMN goals_json TEXT NOT NULL DEFAULT '[]'",
            "body_focus_json": "ALTER TABLE clients ADD COLUMN body_focus_json TEXT NOT NULL DEFAULT '[]'",
            "goal_details_json": "ALTER TABLE clients ADD COLUMN goal_details_json TEXT NOT NULL DEFAULT '{}'",
            "training_days_json": "ALTER TABLE clients ADD COLUMN training_days_json TEXT NOT NULL DEFAULT '[]'",
            "day_time_json": "ALTER TABLE clients ADD COLUMN day_time_json TEXT NOT NULL DEFAULT '{}'",
        }
        for col, sql in migrations.items():
            if col not in cols:
                conn.execute(sql)
        now = utc_now()
        conn.execute("UPDATE clients SET created_at = COALESCE(created_at, updated_at, ?)", (now,))

        id_type = "BIGSERIAL" if USE_POSTGRES else "INTEGER"
        id_suffix = "" if USE_POSTGRES else " AUTOINCREMENT"
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS progress_metrics (
                id {id_type} PRIMARY KEY{id_suffix}, client_id TEXT NOT NULL, metric_type TEXT NOT NULL,
                value REAL NOT NULL, unit TEXT, measured_at TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS workout_logs (
                id {id_type} PRIMARY KEY{id_suffix}, client_id TEXT NOT NULL, workout_date TEXT NOT NULL,
                workout_name TEXT NOT NULL, completed INTEGER NOT NULL, rpe INTEGER, wellbeing INTEGER,
                pain INTEGER NOT NULL DEFAULT 0, duration_min INTEGER, distance_km REAL, avg_hr INTEGER,
                comment TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS programs (
                id {id_type} PRIMARY KEY{id_suffix}, client_id TEXT NOT NULL, title TEXT NOT NULL,
                goal TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL, plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                client_id TEXT PRIMARY KEY, start_date TEXT, paid_until TEXT, grace_until TEXT,
                amount REAL, currency TEXT NOT NULL DEFAULT 'RUB', payment_status TEXT NOT NULL DEFAULT 'UNPAID',
                access_enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS workout_assignments (
                id {id_type} PRIMARY KEY{id_suffix}, client_id TEXT NOT NULL, program_id INTEGER,
                workout_date TEXT NOT NULL, title TEXT NOT NULL, content_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'SCHEDULED', version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS workout_feedback (
                id {id_type} PRIMARY KEY{id_suffix}, workout_id INTEGER NOT NULL, client_id TEXT NOT NULL,
                completion_status TEXT NOT NULL, rpe INTEGER, wellbeing INTEGER NOT NULL, pain INTEGER NOT NULL DEFAULT 0,
                pain_location TEXT, reason TEXT, comment TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS workout_revisions (
                id {id_type} PRIMARY KEY{id_suffix}, workout_id INTEGER NOT NULL, client_id TEXT NOT NULL,
                old_version INTEGER NOT NULL, old_date TEXT NOT NULL, old_title TEXT NOT NULL, old_content_json TEXT NOT NULL,
                changed_by TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS payment_reminders (
                id {id_type} PRIMARY KEY{id_suffix}, client_id TEXT NOT NULL, paid_until TEXT NOT NULL,
                days_before INTEGER NOT NULL, sent_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vacations (
                client_id TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                gym_type TEXT NOT NULL DEFAULT 'PARTIAL_GYM', equipment_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT, updated_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.commit()


def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    if not TOKEN or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0") or "0")
        if auth_date:
            age_seconds = datetime.now(timezone.utc).timestamp() - auth_date
            if age_seconds > 24 * 3600:
                return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") is not None else None
    except Exception:
        return None


def current_user(init_data: Optional[str], required: bool = True) -> Optional[dict]:
    user = verify_telegram_init_data(init_data or "")
    if required and not user:
        raise HTTPException(status_code=401, detail="Open M17 inside Telegram to authenticate")
    return user


def coach_user(init_data: Optional[str]) -> dict:
    user = current_user(init_data, required=True)
    if str(user["id"]) not in COACH_IDS:
        raise HTTPException(status_code=403, detail="Coach access required")
    return user


def is_coach_id(user_id: str) -> bool:
    return str(user_id) in COACH_IDS


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def phase(req: PlanRequest) -> str:
    if req.weeks_to_event is None:
        return "general"
    if req.weeks_to_event <= 2:
        return "taper"
    if req.weeks_to_event <= 6:
        return "specific"
    if req.weeks_to_event <= 14:
        return "build"
    return "base"


def cap_sessions(blocks: list[TrainingBlock], max_sessions: int) -> list[TrainingBlock]:
    total = sum(b.sessions for b in blocks)
    if total <= max_sessions:
        return blocks
    order = ["Мобилити / профилактика", "Функциональная сила", "Кондиционная работа", "Zone 2", "Сила"]
    for name in order:
        for block in blocks:
            while total > max_sessions and block.name == name and block.sessions > 0:
                block.sessions -= 1
                total -= 1
    while total > max_sessions:
        candidates = [b for b in blocks if b.sessions > 1]
        if not candidates:
            break
        candidates[-1].sessions -= 1
        total -= 1
    return [b for b in blocks if b.sessions > 0]


def profile_note(req: PlanRequest) -> Optional[str]:
    notes = []
    if req.experience_years is not None and req.experience_years >= 5:
        notes.append("Стаж 5+ лет: следующий цикл выбирается с учётом тренировочной истории, а не с нуля.")
    if req.age is not None and req.age >= 50:
        notes.append("Восстановление и переносимость объёма контролируем по фактической реакции спортсмена.")
    if req.limitations:
        notes.append("Есть ограничения: упражнения требуют допустимых замен и масштабирования.")
    return " ".join(notes) or None


def build_plan(req: PlanRequest) -> PlanPreview:
    p = phase(req)
    if req.goal in {Goal.ironman, Goal.half_ironman, Goal.olympic, Goal.sprint}:
        swim, bike, run, brick = {
            Goal.ironman: (2, 2, 2, 1), Goal.half_ironman: (2, 2, 2, 1),
            Goal.olympic: (2, 2, 2, 1), Goal.sprint: (1, 2, 2, 1),
        }[req.goal]
        strength = 2 if p in {"base", "general"} else 1
        functional = 1 if p in {"base", "build", "general"} else 0
        blocks = [
            TrainingBlock(name="Плавание", sessions=swim, priority="primary", note="Техника + аэробная / интервальная работа"),
            TrainingBlock(name="Велосипед", sessions=bike, priority="primary", note="Выносливость, темп, порог, специфика дистанции"),
            TrainingBlock(name="Бег", sessions=run, priority="primary", note="Лёгкий, длинный, темповой / пороговый бег"),
            TrainingBlock(name="Брик", sessions=brick, priority="primary", note="Адаптация велосипед → бег"),
            TrainingBlock(name="Сила", sessions=strength, priority="support", note="Сила всего тела, задняя цепь, корпус, unilateral"),
            TrainingBlock(name="Функциональная сила", sessions=functional, priority="support", note="Переноски, sled, гири, простая функциональная работа"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Плечи, грудной отдел, таз, голеностоп, стопа"),
        ]
    elif req.goal == Goal.running:
        runs = 3 if req.days_per_week >= 4 else 2
        blocks = [
            TrainingBlock(name="Лёгкий бег", sessions=max(1, runs - 2), priority="primary", note="Аэробная база / Z2"),
            TrainingBlock(name="Качественная беговая работа", sessions=1, priority="primary", note="Темп, порог или интервалы по этапу"),
            TrainingBlock(name="Длительный бег", sessions=1, priority="primary", note="Постепенное развитие длительной выносливости"),
            TrainingBlock(name="Сила", sessions=2 if req.days_per_week >= 5 else 1, priority="support", note="Ноги, ягодичные, стопа, корпус"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Стопа, голеностоп, таз, задняя цепь"),
        ]
    elif req.goal == Goal.crossfit:
        wl = 2 if req.level in {Level.advanced, Level.competitive} else 1
        gym = 2 if req.level in {Level.advanced, Level.competitive} else 1
        metcon = 3 if req.level == Level.competitive else 2
        blocks = [
            TrainingBlock(name="Сила", sessions=2, priority="primary", note="Присед, тяга, жим, тяговые движения"),
            TrainingBlock(name="Тяжёлая атлетика", sessions=wl, priority="primary", note="Рывок, взятие, толчок: техника + мощность"),
            TrainingBlock(name="Гимнастика", sessions=gym, priority="primary", note="Подтягивания, TTB, HSPU, стойка, muscle-up"),
            TrainingBlock(name="Аэробная работа", sessions=2, priority="primary", note="Бег, гребля, велосипед, SkiErg"),
            TrainingBlock(name="Меткон", sessions=metcon, priority="primary", note="Couplets, triplets, chippers, EMOM / AMRAP / For Time"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Подвижность и качество движений"),
        ]
    elif req.goal == Goal.fat_loss:
        strength = 3 if req.days_per_week <= 4 else 4
        blocks = [
            TrainingBlock(name="Сила", sessions=strength, priority="primary", note="Сохранение / набор мышечной массы"),
            TrainingBlock(name="Zone 2", sessions=2, priority="support", note="Низкоударная аэробная работа"),
            TrainingBlock(name="Кондиционная работа", sessions=1, priority="support", note="Масштабируемая функциональная тренировка"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Качество движения и восстановление"),
        ]
    elif req.goal == Goal.strength:
        blocks = [
            TrainingBlock(name="Сила", sessions=min(req.days_per_week, 4), priority="primary", note="Базовые движения + индивидуальные слабые звенья"),
            TrainingBlock(name="Zone 2", sessions=1, priority="support", note="Поддержание аэробной формы"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Рабочая амплитуда и восстановление"),
        ]
    else:
        strength = 3 if req.days_per_week == 3 else min(req.days_per_week, 5)
        blocks = [
            TrainingBlock(name="Гипертрофия / силовая работа", sessions=strength, priority="primary", note="6–15 повторов, прогрессивная перегрузка, контроль RIR/RPE"),
            TrainingBlock(name="Zone 2", sessions=1, priority="support", note="Поддержание аэробной формы с низкой утомляемостью"),
            TrainingBlock(name="Мобилити / профилактика", sessions=1, priority="support", note="Рабочая амплитуда и устойчивость тканей"),
        ]
    max_sessions = max(req.days_per_week, int(req.hours_per_week))
    blocks = cap_sessions(blocks, max_sessions)
    warning = None
    if req.goal == Goal.ironman and req.hours_per_week < 5:
        warning = "Для полного Ironman доступное время очень ограничено. Нужно приоритизировать ключевые endurance-сессии."
    if req.goal == Goal.crossfit and req.level == Level.competitive and req.days_per_week < 5:
        warning = "Для соревновательного кроссфита часть компонентов придётся совмещать внутри одной сессии."
    return PlanPreview(goal=req.goal, phase=p, total_sessions=sum(b.sessions for b in blocks), blocks=blocks, warning=warning, profile_note=profile_note(req))


def safe_json(value: Optional[str], fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def row_to_profile(row) -> SavedClientProfile:
    d = dict(row)
    goals_raw = safe_json(d.get("goals_json"), [])
    if not goals_raw:
        goals_raw = [{"goal": d["primary_goal"], "priority": 1}]
    return SavedClientProfile(
        telegram_user_id=d["telegram_user_id"], telegram_username=d.get("telegram_username"), name=d["name"],
        sex=d["sex"], age=d["age"], height_cm=d.get("height_cm"), weight_kg=d.get("weight_kg"),
        experience_years=d["experience_years"], client_type=d.get("client_type") or "NEW_CLIENT",
        primary_goal=d["primary_goal"], goals=goals_raw, body_focus=safe_json(d.get("body_focus_json"), []),
        goal_details=safe_json(d.get("goal_details_json"), {}), level=d["level"],
        training_days=(safe_json(d.get("training_days_json"), []) or {1:[2],2:[1,5],3:[0,2,5],4:[0,1,3,5],5:[0,1,2,4,5],6:[0,1,2,3,4,5],7:list(range(7))}.get(int(d.get("days_per_week") or 3), [0,2,4])),
        day_time_min=safe_json(d.get("day_time_json"), {}), days_per_week=d["days_per_week"],
        hours_per_week=d["hours_per_week"], weeks_to_event=d.get("weeks_to_event"),
        equipment=safe_json(d.get("equipment_json"), []), limitations=d.get("limitations"), notes=d.get("notes"),
        status=d.get("status") or "WAITING_APPROVAL", coach_notes=d.get("coach_notes"),
        created_at=d.get("created_at") or d["updated_at"], updated_at=d["updated_at"],
    )


def plan_request_from_row(row) -> PlanRequest:
    return PlanRequest(
        goal=row["primary_goal"], level=row["level"], days_per_week=row["days_per_week"],
        hours_per_week=row["hours_per_week"], weeks_to_event=row.get("weeks_to_event") if isinstance(row, dict) else row["weeks_to_event"],
        age=row["age"], experience_years=row["experience_years"], limitations=row["limitations"],
    )


def latest_program(conn, client_id: str):
    return conn.execute("SELECT * FROM programs WHERE client_id=? ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()


def subscription_row(conn, client_id: str):
    return conn.execute("SELECT * FROM subscriptions WHERE client_id=?", (client_id,)).fetchone()


def access_state_from_rows(client_row, sub_row, coach_override=False) -> dict:
    if coach_override:
        return {"state": "ACTIVE", "allowed": True, "message": "Coach override"}
    status = client_row["status"]
    if status in {"SUSPENDED", "PAUSED", "ARCHIVE"}:
        return {"state": status, "allowed": False, "message": "Доступ приостановлен тренером."}
    if not sub_row:
        return {"state": "WAITING_PAYMENT", "allowed": False, "message": "Доступ к тренировкам ещё не активирован тренером."}
    if not bool(sub_row["access_enabled"]):
        return {"state": "DISABLED", "allowed": False, "message": "Доступ к тренировкам выключен тренером."}
    today = date.today()
    start = parse_iso_date(sub_row["start_date"])
    paid = parse_iso_date(sub_row["paid_until"])
    grace = parse_iso_date(sub_row["grace_until"])
    if start and today < start:
        return {"state": "PENDING_START", "allowed": False, "message": f"Тренировки начнутся {start.isoformat()}."}
    if paid and today <= paid:
        return {"state": "ACTIVE", "allowed": True, "message": f"Оплачено до {paid.isoformat()}."}
    if grace and today <= grace:
        return {"state": "PAYMENT_DUE", "allowed": True, "message": f"Оплата просрочена. Льготный доступ до {grace.isoformat()}."}
    return {"state": "EXPIRED", "allowed": False, "message": "Оплаченный период закончился. Свяжитесь с тренером."}


def access_state(conn, client_id: str, coach_override=False) -> dict:
    client = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone()
    if not client:
        return {"state": "NO_PROFILE", "allowed": False, "message": "Анкета не заполнена."}
    return access_state_from_rows(client, subscription_row(conn, client_id), coach_override)


def client_alert(conn, client_id: str) -> Optional[str]:
    feedback = conn.execute(
        "SELECT * FROM workout_feedback WHERE client_id=? ORDER BY id DESC LIMIT 3", (client_id,)
    ).fetchall()
    if feedback:
        if any(bool(r["pain"]) for r in feedback):
            return "Отмечена боль / дискомфорт"
        rpes = [r["rpe"] for r in feedback if r["rpe"] is not None]
        if len(rpes) >= 2 and sum(rpes) / len(rpes) >= 9:
            return "Высокий RPE в последних тренировках"
        wellbeing = [r["wellbeing"] for r in feedback if r["wellbeing"] is not None]
        if len(wellbeing) >= 2 and sum(wellbeing) / len(wellbeing) <= 4:
            return "Низкое самочувствие"
        if sum(1 for r in feedback if r["completion_status"] == "NOT_DONE") >= 2:
            return "Две или больше невыполненных тренировок"
    logs = conn.execute("SELECT * FROM workout_logs WHERE client_id=? ORDER BY id DESC LIMIT 3", (client_id,)).fetchall()
    if logs and any(bool(r["pain"]) for r in logs):
        return "Отмечена боль / дискомфорт"
    return None


async def telegram_api(method: str, payload: dict):
    if not TOKEN:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload)
        response.raise_for_status()
        return response.json()


async def notify_client(client_id: str, payload: dict):
    sockets = list(WS_CONNECTIONS.get(str(client_id), set()))
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            WS_CONNECTIONS.get(str(client_id), set()).discard(ws)


GOAL_LABELS = {
    "ironman": "Ironman 140.6", "half_ironman": "Ironman 70.3", "olympic": "Олимпийская дистанция",
    "sprint": "Спринт-триатлон", "running": "Бег", "crossfit": "Кроссфит",
    "fat_loss": "Похудение", "muscle_gain": "Набор мышц", "strength": "Сила",
}


def selected_goals(row) -> list[dict]:
    goals = safe_json(row.get("goals_json") if isinstance(row, dict) else row["goals_json"], [])
    if not goals:
        goals = [{"goal": row["primary_goal"], "priority": 1}]
    return sorted(goals, key=lambda g: int(g.get("priority", 2)))


def workout_text(kind: str, level: str, week: int, focus: list[str]) -> str:
    novice = level == "beginner"
    glute = "glutes" in focus or "Ягодичные" in focus
    core = "core" in focus or "Пресс / core" in focus
    if kind == "run_easy":
        mins = 30 + min(15, (week - 1) * 5)
        return f"Разминка: 5–8 мин ходьба + лёгкая беговая разминка.\nОсновная работа: {mins} мин лёгкого бега, разговорный темп / Z2, RPE 3–4.\nЗаминка: 5 мин ходьбы + мобилити стопы и голеностопа."
    if kind == "run_quality":
        reps = 5 + min(2, week - 1)
        return f"Разминка: 12–15 мин легко + 4 ускорения по 15–20 сек.\nОсновная работа: {reps}×2 мин RPE 7–8 / 2 мин лёгкого бега.\nЗаминка: 10 мин легко. Не увеличивать скорость, если техника распадается."
    if kind == "run_long":
        mins = 45 + min(20, (week - 1) * 5)
        return f"Длительный бег: {mins} мин в Z2 / RPE 3–4. Первые 10 мин особенно спокойно.\nЦель — закончить с ощущением запаса, без финишного спринта."
    if kind == "strength_glute":
        squat = "Присед с гирей у груди 3×8 @ RPE 6–7" if novice else "Присед со штангой на спине 4×6 @ RPE 7"
        hip = "Ягодичный мост 3×10 @ RPE 7" if novice else "Ягодичный мост 4×8 @ RPE 7–8"
        split = "Сплит-присед 3×8/8" if novice else "Болгарские приседания 3×8/8 @ RPE 7"
        items = ["Разминка: 8–10 мин + таз/голеностоп.", squat, hip, split, "Румынская тяга 3×8 @ RPE 7"]
        if glute:
            items.append("Отведение бедра с резиной 3×12–15")
        if core:
            items.append("«Мёртвый жук» 3×8/8 + боковая планка 3×30 сек/сторона")
        items.append("Отдых 90–150 сек. Оставлять 2–3 повтора в запасе.")
        return "\n".join(items)
    if kind == "strength_full":
        return "Разминка: 8–10 мин.\nПрисед 4×5 @ RPE 7.\nЖим гантелей/штанги 4×6–8 @ RPE 7.\nТяга горизонтальная 4×8.\nРумынская тяга 3×8.\nФермерская прогулка 4×30 м.\nCore 3 подхода. Отдых 90–150 сек."
    if kind == "crossfit_intro":
        return "Разминка 10–12 мин.\nТехника: присед + hinge + жим, 3 спокойных круга.\nСила: Присед с гирей у груди 4×6 @ RPE 6–7.\nSkill: 10 мин — тяга колец / лопаточные подтягивания / практика двойных прыжков по уровню.\nConditioning 12 мин AMRAP: 8 калорий гребля, 8 становых тяг с гантелями, 8 зашагиваний на коробку. RPE 6–7, без отказа.\nMobility 8 мин."
    if kind == "crossfit_advanced":
        return "Разминка 12–15 мин.\nСила: Присед со штангой на спине 5×3 @ 75–80%, RPE ≤8.\nSkill: EMOM 10 — 1) гимнастический навык 20–30 сек, 2) 40–50 двойных прыжков.\nMetcon: 5 раундов, 12 калорий SkiErg + 8 умеренных рывков гантели + 6 берпи с перепрыгиванием коробки; отдых 1:00. Цель — ровная плотность, не отказ.\nAccessory: задняя цепь + ротаторы плеча."
    if kind == "zone2":
        return "30–45 мин Zone 2 на байк / гребля / SkiErg / быстрая ходьба. RPE 3–4. Дышать контролируемо. После — 10 мин мобилити."
    if kind == "swim":
        return "Плавание 45–60 мин.\n200–400 м легко.\nТехника 6×50 м, отдых 20–30 сек.\nОсновная: 6×100 м аэробно, ровно, отдых 20–30 сек.\n200 м легко. Не ускоряться ценой техники."
    if kind == "bike":
        return "Велосипед 60–75 мин.\n15 мин легко.\n3×8 мин темпово / RPE 6, между 4 мин легко.\nОстаток времени Z2. Каденс комфортный, без силового продавливания."
    if kind == "tri_run":
        return "Бег 35–50 мин Z2 / RPE 3–4. В конце 4×20 сек лёгких ускорений с полным восстановлением."
    if kind == "brick":
        return "Велосипед 45–60 мин Z2, последние 10 мин RPE 5. Быстрый переход.\nБег 10–20 мин легко, задача — техника и адаптация ног, не скорость."
    if kind == "hypertrophy_upper":
        return "Жим 4×8 @ RPE 7–8.\nТяга горизонтальная 4×8–10.\nВертикальная тяга/подтягивания 3×8–10.\nЖим над головой 3×8.\nЗадняя дельта 3×12–15.\nБицепс + трицепс 3×10–15. Отдых 60–120 сек."
    if kind == "hypertrophy_lower":
        return "Присед / жим ногами 4×8 @ RPE 7–8.\nЯгодичный мост 4×8–10.\nрумынская тяга 3×8–10.\nсплит-присед 3×10/10.\nСгибание голени 3×12.\nИкры 3×12–15."
    return "Индивидуальная тренировка. Тренер уточнит содержание."


def training_day_indexes(row) -> list[int]:
    d = dict(row)
    raw = safe_json(d.get("training_days_json"), [])
    days = sorted({int(x) for x in raw if str(x).isdigit() and 0 <= int(x) <= 6})
    if days:
        return days
    count = max(1, min(7, int(d.get("days_per_week") or 3)))
    return {1:[2],2:[1,5],3:[0,2,5],4:[0,1,3,5],5:[0,1,2,4,5],6:[0,1,2,3,4,5],7:list(range(7))}[count]


def vacation_row(conn, client_id: str):
    return conn.execute("SELECT * FROM vacations WHERE client_id=?", (client_id,)).fetchone()


def vacation_info(vac) -> Optional[dict]:
    if not vac:
        return None
    d = dict(vac)
    start = parse_iso_date(d.get("start_date"))
    end = parse_iso_date(d.get("end_date"))
    today = date.today()
    state = "PLANNED"
    if start and end and start <= today <= end:
        state = "ACTIVE"
    elif end and today > end:
        state = "COMPLETED"
    d["state"] = state
    d["equipment"] = safe_json(d.pop("equipment_json", "[]"), [])
    return d


def earliest_open_workout(conn, client_id: str):
    return conn.execute(
        "SELECT * FROM workout_assignments WHERE client_id=? AND status IN ('SCHEDULED','OPENED') ORDER BY workout_date,id LIMIT 1",
        (client_id,),
    ).fetchone()


def normalize_workout_title_ru(title: str) -> str:
    """Приводит названия старых тренировок к русскому интерфейсу.

    Старые назначения сохраняются в БД без потери истории, но клиент и
    тренер всегда видят русскую версию. Спортивные сокращения EMOM/AMRAP,
    Zone 2 оставляем как договорились.
    """
    text = str(title or "Тренировка")
    replacements = {
        "CrossFit": "Кроссфит",
        "Strength": "Силовая",
        "Running": "Бег",
        "Run": "Бег",
        "Swim": "Плавание",
        "Bike": "Велосипед",
        "Hypertrophy": "Гипертрофия",
        "Recovery": "Восстановление",
        "Mobility": "Мобилити",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_workout_text_ru(text: str) -> str:
    """Мягкая миграция старых тренировок в русскую терминологию.

    Ничего не удаляет из истории и не меняет числовые параметры. Замены
    применяются только к отображению и поэтому безопасны для уже выполненных
    тренировок.
    """
    text = str(text or "")
    replacements = [
        ("Back Squat", "Присед со штангой на спине"),
        ("Front Squat", "Фронтальный присед"),
        ("Goblet Squat", "Присед с гирей у груди"),
        ("Romanian Deadlift", "Румынская тяга"),
        ("Deadlift", "Становая тяга"),
        ("RDL", "Румынская тяга"),
        ("Bench Press", "Жим лёжа"),
        ("Strict Press", "Строгий жим стоя"),
        ("Push Press", "Жимовой швунг"),
        ("Ring Row", "Тяга на кольцах"),
        ("Pull-ups", "Подтягивания"),
        ("Pull-up", "Подтягивания"),
        ("Push-ups", "Отжимания"),
        ("Push-up", "Отжимания"),
        ("Step-ups", "Зашагивания на коробку"),
        ("Step-up", "Зашагивания на коробку"),
        ("Farmer Carry", "Фермерская прогулка"),
        ("Farmer Walk", "Фермерская прогулка"),
        ("Hang Power Clean", "Взятие с виса в стойку"),
        ("Power Clean", "Взятие в стойку"),
        ("Clean & Jerk", "Взятие и толчок"),
        ("Snatch", "Рывок"),
        ("Burpees", "Берпи"),
        ("Burpee", "Берпи"),
        ("Box Jump", "Прыжки на коробку"),
        ("Box Step", "Зашагивания на коробку"),
        ("SkiErg", "лыжи"),
        ("BikeErg", "Байк Эрг"),
        ("Row", "гребля"),
        ("Skill:", "Практика:"),
        ("Skill", "Практика"),
        ("Conditioning:", "Кондиционная работа:"),
        ("Conditioning", "Кондиционная работа"),
        ("Accessory:", "Закачка:"),
        ("Accessory", "Закачка"),
        ("Mobility:", "Мобилити:"),
        ("Mobility", "Мобилити"),
        ("Core", "Корпус"),
        ("RPE", "ИВН"),
        ("Rest", "Отдых"),
        ("rest", "отдых"),
        ("For time", "На время"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def client_workout_view(row, include_content: bool = False) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["title"] = normalize_workout_title_ru(d.get("title") or "Тренировка")
    content = safe_json(d.pop("content_json", "{}"), {})
    if isinstance(content, dict) and "text" in content:
        content["text"] = normalize_workout_text_ru(content.get("text") or "")
    if include_content:
        d["content"] = content
    else:
        d["content"] = {}
    return d



def weekly_kinds(row) -> list[str]:
    goals = selected_goals(dict(row))
    primary = goals[0]["goal"] if goals else row["primary_goal"]
    all_goals = {g["goal"] for g in goals}
    days = len(training_day_indexes(row))
    level = row["level"]
    focus = set(safe_json(row["body_focus_json"], []))

    def take(seq):
        if days >= len(seq):
            return seq[:days]
        return seq[:days]

    if primary in {"ironman", "half_ironman", "olympic", "sprint"}:
        variants = {
            1: ["bike"],
            2: ["swim", "bike"],
            3: ["swim", "bike", "tri_run"],
            4: ["swim", "bike", "tri_run", "strength_full"],
            5: ["swim", "bike", "tri_run", "strength_full", "brick"],
            6: ["swim", "bike", "tri_run", "strength_full", "swim", "brick"],
            7: ["swim", "bike", "tri_run", "strength_full", "swim", "brick", "bike"],
        }
        return variants[days]
    if primary == "running" or "running" in all_goals:
        strength_kind = "strength_glute" if ("glutes" in focus or "fat_loss" in all_goals or "muscle_gain" in all_goals) else "strength_full"
        variants = {
            1: ["run_easy"],
            2: ["run_quality", "run_long"],
            3: [strength_kind, "run_quality", "run_long"],
            4: [strength_kind, "run_easy", "run_quality", "run_long"],
            5: [strength_kind, "run_easy", "strength_glute" if "glutes" in focus else "zone2", "run_quality", "run_long"],
            6: [strength_kind, "run_easy", "strength_glute" if "glutes" in focus else "strength_full", "run_quality", "zone2", "run_long"],
            7: [strength_kind, "run_easy", "strength_glute" if "glutes" in focus else "strength_full", "run_quality", "zone2", "run_long", "zone2"],
        }
        return variants[days]
    if primary == "crossfit":
        cf = "crossfit_intro" if level == "beginner" else "crossfit_advanced"
        variants = {
            1: [cf], 2: [cf, "zone2"], 3: [cf, "strength_full", cf],
            4: [cf, "zone2", "strength_full", cf],
            5: [cf, "zone2", cf, "strength_full", cf],
            6: [cf, "zone2", cf, "strength_full", cf, "zone2"],
            7: [cf, "zone2", cf, "strength_full", cf, "zone2", cf],
        }
        return variants[days]
    if primary == "muscle_gain":
        seq = ["hypertrophy_lower", "hypertrophy_upper", "zone2", "hypertrophy_lower", "hypertrophy_upper", "strength_glute", "zone2"]
    elif primary == "fat_loss":
        seq = ["strength_glute" if "glutes" in focus else "strength_full", "zone2", "strength_full", "run_easy" if "running" in all_goals else "zone2", "strength_glute", "zone2", "strength_full"]
    else:
        seq = ["strength_full", "zone2", "strength_full", "zone2", "strength_full", "zone2", "strength_full"]
    return seq[:days]


def insert_workout(conn, client_id: str, program_id: Optional[int], workout_date: str, title: str, text: str):
    now = utc_now()
    content = json.dumps({"text": text}, ensure_ascii=False)
    if USE_POSTGRES:
        cur = conn.execute(
            "INSERT INTO workout_assignments(client_id,program_id,workout_date,title,content_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'SCHEDULED',1,?,?) RETURNING id",
            (client_id, program_id, workout_date, title, content, now, now),
        )
        row = cur.fetchone()
        return row["id"] if row else None
    cur = conn.execute(
        "INSERT INTO workout_assignments(client_id,program_id,workout_date,title,content_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'SCHEDULED',1,?,?)",
        (client_id, program_id, workout_date, title, content, now, now),
    )
    return cur.lastrowid


def generate_intro_workouts(conn, row, program_id: Optional[int], start: date, weeks=4):
    kinds = weekly_kinds(row)
    day_indexes = training_day_indexes(row)
    # Ключевую длительную сессию ставим на выбранный клиентом день с наибольшим доступным временем.
    time_map = safe_json(dict(row).get("day_time_json"), {})
    if day_indexes and time_map:
        longest_pos = max(range(len(day_indexes)), key=lambda i: int(time_map.get(str(day_indexes[i]), 60) or 60))
        preferred = "run_long" if "run_long" in kinds else ("bike" if row["primary_goal"] in {"ironman", "half_ironman", "olympic", "sprint"} and "bike" in kinds else None)
        if preferred:
            kind_pos = max(i for i, k in enumerate(kinds) if k == preferred)
            kinds[longest_pos], kinds[kind_pos] = kinds[kind_pos], kinds[longest_pos]
    focus = safe_json(row["body_focus_json"], [])
    level = row["level"]
    labels = {
        "run_easy": "Лёгкий бег", "run_quality": "Бег · интервалы / порог", "run_long": "Длительный бег",
        "strength_glute": "Сила · ягодичные + корпус", "strength_full": "Силовая · всё тело",
        "crossfit_intro": "Кроссфит · техника + кондиция", "crossfit_advanced": "Кроссфит · сила + навык + меткон",
        "zone2": "Zone 2 / восстановление", "swim": "Плавание", "bike": "Велосипед", "tri_run": "Бег · триатлон",
        "brick": "Брик · велосипед → бег", "hypertrophy_upper": "Гипертрофия · верх тела", "hypertrophy_lower": "Гипертрофия · низ тела",
    }
    conn.execute("DELETE FROM workout_assignments WHERE client_id=? AND status='SCHEDULED' AND workout_date>=?", (row["telegram_user_id"], start.isoformat()))
    for week in range(1, weeks + 1):
        monday = start + timedelta(days=(7 - start.weekday()) % 7) if week == 1 else None
        if week == 1:
            base = start - timedelta(days=start.weekday())
        else:
            base = (start - timedelta(days=start.weekday())) + timedelta(days=(week - 1) * 7)
        for kind, idx in zip(kinds, day_indexes):
            d = base + timedelta(days=idx)
            if d < start:
                continue
            text = workout_text(kind, level, week, focus)
            insert_workout(conn, row["telegram_user_id"], program_id, d.isoformat(), labels.get(kind, "Тренировка"), text)


def payment_summary(conn):
    rows = conn.execute(
        "SELECT c.telegram_user_id,c.name,c.status,s.* FROM clients c LEFT JOIN subscriptions s ON c.telegram_user_id=s.client_id WHERE c.status!='ARCHIVE' ORDER BY s.paid_until ASC"
    ).fetchall()
    result = []
    today = date.today()
    due_7 = 0
    expired = 0
    for r in rows:
        rr = dict(r)
        state = access_state_from_rows(r, r if rr.get("client_id") else None, False)
        paid = parse_iso_date(rr.get("paid_until"))
        days_left = (paid - today).days if paid else None
        if days_left is not None and 0 <= days_left <= 7:
            due_7 += 1
        if state["state"] == "EXPIRED":
            expired += 1
        result.append({
            "client_id": r["telegram_user_id"], "name": r["name"], "paid_until": rr.get("paid_until"),
            "start_date": rr.get("start_date"), "grace_until": rr.get("grace_until"), "amount": rr.get("amount"),
            "currency": rr.get("currency") or "RUB", "payment_status": rr.get("payment_status") or "UNPAID",
            "access_enabled": bool(rr.get("access_enabled") or 0), "access": state, "days_left": days_left,
        })
    return {"items": result, "due_7": due_7, "expired": expired}


@app.on_event("startup")
async def startup():
    init_db()
    if TOKEN and PUBLIC_URL:
        payload = {"url": f"{PUBLIC_URL}/telegram/webhook", "drop_pending_updates": False}
        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET
        app_url = f"{PUBLIC_URL}/?v={APP_VERSION}"
        try:
            await telegram_api("setWebhook", payload)
            await telegram_api("setChatMenuButton", {
                "menu_button": {
                    "type": "web_app",
                    "text": "Открыть M17",
                    "web_app": {"url": app_url},
                }
            })
            print(f"Telegram webhook registered; menu app URL={app_url}")
        except Exception as exc:
            print(f"Telegram startup configuration failed: {exc}")


@app.get("/")
def index():
    # Mini App обновляется часто. Просим WebView всегда перепроверять HTML у сервера.
    return FileResponse(
        BASE_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, private, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-M17-Version": APP_VERSION,
        },
    )


@app.head("/")
def index_head():
    return {"ok": True}


@app.get("/health")
def health():
    return {
        "status": "ok", "telegram_configured": bool(TOKEN and PUBLIC_URL), "version": APP_VERSION,
        "storage": "postgres" if USE_POSTGRES else "sqlite", "coach_configured": bool(COACH_IDS),
        "features": ["multi_goal", "training_days", "triathlon_distances", "running_distances", "trail_running", "sequential_workouts", "vacation_mode", "subscriptions", "mandatory_feedback", "live_edit"],
    }


@app.get("/auth/me")
def auth_me(x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        has_profile = conn.execute("SELECT 1 AS ok FROM clients WHERE telegram_user_id=?", (uid,)).fetchone() is not None
    return {"telegram_user_id": uid, "name": user.get("first_name", ""), "username": user.get("username"), "is_coach": is_coach_id(uid), "roles": (["COACH", "CLIENT"] if is_coach_id(uid) else ["CLIENT"]), "has_client_profile": has_profile}


@app.websocket("/ws")
async def ws_updates(websocket: WebSocket):
    init_data = websocket.query_params.get("initData", "")
    user = verify_telegram_init_data(init_data)
    if not user:
        await websocket.close(code=4401)
        return
    uid = str(user["id"])
    await websocket.accept()
    WS_CONNECTIONS.setdefault(uid, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        WS_CONNECTIONS.get(uid, set()).discard(websocket)
    except Exception:
        WS_CONNECTIONS.get(uid, set()).discard(websocket)


@app.post("/clients/profile", response_model=SavedClientProfile)
def save_profile(profile: ClientProfile, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    profile.telegram_user_id = str(user["id"])
    profile.telegram_username = user.get("username") or profile.telegram_username
    now = utc_now()
    with db_conn() as conn:
        old = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (profile.telegram_user_id,)).fetchone()
        created_at = old["created_at"] if old and old["created_at"] else now
        # New profiles wait for coach approval. Existing profiles keep current status.
        status = old["status"] if old and old["status"] else ClientStatus.waiting_approval.value
        coach_notes = old["coach_notes"] if old else None
        conn.execute(
            """
            INSERT INTO clients (telegram_user_id,telegram_username,name,sex,age,height_cm,weight_kg,experience_years,primary_goal,level,days_per_week,hours_per_week,weeks_to_event,equipment_json,limitations,notes,status,coach_notes,created_at,updated_at,client_type,goals_json,body_focus_json,goal_details_json,training_days_json,day_time_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET telegram_username=excluded.telegram_username,name=excluded.name,sex=excluded.sex,age=excluded.age,height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,experience_years=excluded.experience_years,primary_goal=excluded.primary_goal,level=excluded.level,days_per_week=excluded.days_per_week,hours_per_week=excluded.hours_per_week,weeks_to_event=excluded.weeks_to_event,equipment_json=excluded.equipment_json,limitations=excluded.limitations,notes=excluded.notes,client_type=excluded.client_type,goals_json=excluded.goals_json,body_focus_json=excluded.body_focus_json,goal_details_json=excluded.goal_details_json,training_days_json=excluded.training_days_json,day_time_json=excluded.day_time_json,updated_at=excluded.updated_at
            """,
            (
                profile.telegram_user_id, profile.telegram_username, profile.name, profile.sex.value, profile.age,
                profile.height_cm, profile.weight_kg, profile.experience_years, profile.primary_goal.value, profile.level.value,
                profile.days_per_week, profile.hours_per_week, profile.weeks_to_event,
                json.dumps(profile.equipment, ensure_ascii=False), profile.limitations, profile.notes, status, coach_notes,
                created_at, now, profile.client_type.value, json.dumps([g.model_dump(mode="json") for g in profile.goals], ensure_ascii=False),
                json.dumps(profile.body_focus, ensure_ascii=False), json.dumps(profile.goal_details, ensure_ascii=False),
                json.dumps(profile.training_days, ensure_ascii=False), json.dumps(profile.day_time_min, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (profile.telegram_user_id,)).fetchone()
    return row_to_profile(row)


@app.get("/clients/profile/{telegram_user_id}", response_model=SavedClientProfile)
def get_profile(telegram_user_id: str, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    if str(user["id"]) != telegram_user_id and not is_coach_id(str(user["id"])):
        raise HTTPException(status_code=403, detail="Access denied")
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Client profile not found")
    return row_to_profile(row)


@app.post("/plans/preview", response_model=PlanPreview)
def preview_plan(payload: PlanRequest):
    return build_plan(payload)


@app.get("/client/home")
def client_home(x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (uid,)).fetchone()
        if not row:
            return {"has_profile": False, "user": user}
        sub = subscription_row(conn, uid)
        access = access_state_from_rows(row, sub, coach_override=is_coach_id(uid))
        program = latest_program(conn, uid)
        metrics = conn.execute("SELECT * FROM progress_metrics WHERE client_id=? ORDER BY measured_at DESC,id DESC LIMIT 6", (uid,)).fetchall()
        current = earliest_open_workout(conn, uid)
        history = conn.execute("SELECT * FROM workout_assignments WHERE client_id=? AND status IN ('COMPLETED','PARTIAL','NOT_DONE') ORDER BY workout_date DESC,id DESC LIMIT 12", (uid,)).fetchall()
        vac = vacation_info(vacation_row(conn, uid))
        pending_count_row = conn.execute("SELECT COUNT(*) AS n FROM workout_assignments WHERE client_id=? AND status IN ('SCHEDULED','OPENED')", (uid,)).fetchone()
        pending_count = int(pending_count_row["n"] if pending_count_row else 0)
    current_view = None
    next_locked = None
    if current and access["allowed"]:
        cdate = parse_iso_date(current["workout_date"])
        if cdate and cdate <= date.today():
            current_view = client_workout_view(current, include_content=(current["status"] == "OPENED"))
            current_view["can_open"] = current["status"] == "SCHEDULED"
            current_view["can_finish"] = current["status"] == "OPENED"
        else:
            next_locked = {"workout_date": current["workout_date"]}
    return {
        "has_profile": True, "profile": row_to_profile(row).model_dump(), "access": access,
        "subscription": dict(sub) if sub else None,
        "program": (dict(program) | {"plan": safe_json(program["plan_json"], {})}) if (program and access["allowed"]) else None,
        "metrics": [dict(x) for x in metrics],
        "current_workout": current_view, "next_locked": next_locked, "pending_count": pending_count,
        "vacation": vac,
    }


@app.get("/client/workouts")
def client_workouts(x_telegram_init_data: Optional[str] = Header(default=None)):
    """Только текущая доступная тренировка клиента.

    Будущие назначения намеренно не возвращаются клиентскому API. Это
    защищает последовательность программы даже если пользователь вручную
    обращается к API, а не только скрывает элементы в интерфейсе.
    """
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        access = access_state(conn, uid, coach_override=is_coach_id(uid))
        if not access["allowed"]:
            raise HTTPException(status_code=403, detail=access["message"])
        current = earliest_open_workout(conn, uid)
    current_view = None
    next_date = None
    if current:
        cdate = parse_iso_date(current["workout_date"])
        if cdate and cdate <= date.today():
            current_view = client_workout_view(current, include_content=(current["status"] == "OPENED"))
            current_view["locked_by_date"] = False
        elif cdate:
            next_date = current["workout_date"]
    return {"current": current_view, "next_available_date": next_date}


@app.get("/client/archive")
def client_archive(x_telegram_init_data: Optional[str] = Header(default=None)):
    """Личный архив уже закрытых тренировок клиента.

    Клиент видит только собственные выполненные/частичные/пропущенные
    тренировки. Будущих тренировок здесь нет.
    """
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        access = access_state(conn, uid, coach_override=is_coach_id(uid))
        if not access["allowed"]:
            raise HTTPException(status_code=403, detail=access["message"])
        rows = conn.execute(
            "SELECT * FROM workout_assignments WHERE client_id=? AND status IN ('COMPLETED','PARTIAL','NOT_DONE') ORDER BY workout_date DESC,id DESC LIMIT 200",
            (uid,),
        ).fetchall()
        feedback_rows = conn.execute(
            "SELECT * FROM workout_feedback WHERE client_id=? ORDER BY id DESC",
            (uid,),
        ).fetchall()
    feedback_by_workout = {int(f["workout_id"]): dict(f) for f in feedback_rows}
    items = []
    for row in rows:
        item = client_workout_view(row, include_content=True)
        item["feedback"] = feedback_by_workout.get(int(row["id"]))
        items.append(item)
    return {"archive": items}


@app.post("/client/workouts/{workout_id}/open")
def open_workout(workout_id: int, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        access = access_state(conn, uid, coach_override=is_coach_id(uid))
        if not access["allowed"]:
            raise HTTPException(status_code=403, detail=access["message"])
        current = earliest_open_workout(conn, uid)
        if not current or int(current["id"]) != int(workout_id):
            raise HTTPException(status_code=409, detail="Сначала закройте предыдущую тренировку")
        wdate = parse_iso_date(current["workout_date"])
        if wdate and wdate > date.today():
            raise HTTPException(status_code=409, detail=f"Тренировка станет доступна {wdate.isoformat()}")
        if current["status"] == "SCHEDULED":
            conn.execute("UPDATE workout_assignments SET status='OPENED',updated_at=? WHERE id=?", (utc_now(), workout_id))
            conn.commit()
        row = conn.execute("SELECT * FROM workout_assignments WHERE id=?", (workout_id,)).fetchone()
    return {"ok": True, "workout": client_workout_view(row, include_content=True)}


@app.get("/client/vacation")
def get_client_vacation(x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        return vacation_info(vacation_row(conn, uid)) or {}


@app.put("/client/vacation")
async def save_client_vacation(payload: VacationSettings, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        if not conn.execute("SELECT 1 AS ok FROM clients WHERE telegram_user_id=?", (uid,)).fetchone():
            raise HTTPException(status_code=404, detail="Сначала заполните анкету")
        conn.execute(
            """INSERT INTO vacations(client_id,start_date,end_date,gym_type,equipment_json,notes,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(client_id) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,gym_type=excluded.gym_type,equipment_json=excluded.equipment_json,notes=excluded.notes,updated_at=excluded.updated_at""",
            (uid, payload.start_date, payload.end_date, payload.gym_type, json.dumps(payload.equipment, ensure_ascii=False), payload.notes, utc_now()),
        )
        conn.commit()
    for coach_id in COACH_IDS:
        try:
            await telegram_api("sendMessage", {"chat_id": int(coach_id), "text": f"M17: клиент {user.get('first_name','')} запланировал отпуск {payload.start_date} — {payload.end_date}. Проверьте доступное оборудование и программу."})
        except Exception:
            pass
    return {"ok": True}


@app.delete("/client/vacation")
def delete_client_vacation(x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        conn.execute("DELETE FROM vacations WHERE client_id=?", (uid,))
        conn.commit()
    return {"ok": True}


@app.post("/client/workouts/{workout_id}/feedback")
def submit_feedback(workout_id: int, payload: WorkoutFeedback, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM workout_assignments WHERE id=? AND client_id=?", (workout_id, uid)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workout not found")
        if row["status"] != "OPENED":
            raise HTTPException(status_code=409, detail="Сначала откройте текущую тренировку")
        # one final feedback per workout; replace if client corrects it
        conn.execute("DELETE FROM workout_feedback WHERE workout_id=? AND client_id=?", (workout_id, uid))
        conn.execute(
            "INSERT INTO workout_feedback(workout_id,client_id,completion_status,rpe,wellbeing,pain,pain_location,reason,comment,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (workout_id, uid, payload.completion_status, payload.rpe, payload.wellbeing, 1 if payload.pain else 0,
             payload.pain_location, payload.reason, payload.comment, utc_now()),
        )
        conn.execute("UPDATE workout_assignments SET status=?,updated_at=? WHERE id=?", (payload.completion_status, utc_now(), workout_id))
        conn.commit()
    return {"ok": True, "alert": bool(payload.pain or payload.wellbeing <= 3 or (payload.rpe or 0) >= 10)}


@app.post("/client/progress")
def add_progress(entry: ProgressEntry, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    measured_at = entry.measured_at or utc_now()
    with db_conn() as conn:
        conn.execute("INSERT INTO progress_metrics(client_id,metric_type,value,unit,measured_at,note,created_at) VALUES(?,?,?,?,?,?,?)", (uid, entry.metric_type, entry.value, entry.unit, measured_at, entry.note, utc_now()))
        conn.commit()
    return {"ok": True}


@app.post("/client/workout-log")
def add_workout_log(log: WorkoutLog, x_telegram_init_data: Optional[str] = Header(default=None)):
    # Backward-compatible free-form log. New scheduled workouts use mandatory /feedback endpoint.
    user = current_user(x_telegram_init_data, required=True)
    uid = str(user["id"])
    with db_conn() as conn:
        conn.execute("INSERT INTO workout_logs(client_id,workout_date,workout_name,completed,rpe,wellbeing,pain,duration_min,distance_km,avg_hr,comment,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uid, log.workout_date, log.workout_name, 1 if log.completed else 0, log.rpe, log.wellbeing, 1 if log.pain else 0, log.duration_min, log.distance_km, log.avg_hr, log.comment, utc_now()))
        conn.commit()
    return {"ok": True}


@app.get("/coach/dashboard")
def coach_dashboard(x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM clients WHERE status!='ARCHIVE' ORDER BY updated_at DESC").fetchall()
        new_count = sum(1 for r in rows if r["status"] in {"NEW", "WAITING_APPROVAL"})
        active_count = sum(1 for r in rows if r["status"] == "ACTIVE")
        alerts = []
        for r in rows:
            a = client_alert(conn, r["telegram_user_id"])
            if a:
                alerts.append({"client_id": r["telegram_user_id"], "name": r["name"], "message": a})
        pay = payment_summary(conn)
        vac_rows = conn.execute("SELECT v.*,c.name FROM vacations v JOIN clients c ON c.telegram_user_id=v.client_id WHERE c.status!='ARCHIVE'").fetchall()
        vacations = [vacation_info(v) | {"name": v["name"]} for v in vac_rows if vacation_info(v) and vacation_info(v)["state"] == "ACTIVE"]
    return {"clients": len(rows), "new_clients": new_count, "active_clients": active_count, "vacation_count": len(vacations), "vacation_clients": vacations[:20], "alerts_count": len(alerts), "alerts": alerts[:10], "payments_due_7": pay["due_7"], "payments_expired": pay["expired"]}


@app.get("/coach/clients")
def coach_clients(status: Optional[str] = None, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        if status:
            rows = conn.execute("SELECT * FROM clients WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clients ORDER BY CASE status WHEN 'WAITING_APPROVAL' THEN 0 WHEN 'NEW' THEN 0 WHEN 'ASSESSMENT' THEN 1 WHEN 'ACTIVE' THEN 2 ELSE 3 END, updated_at DESC").fetchall()
        result = []
        for r in rows:
            program = latest_program(conn, r["telegram_user_id"])
            sub = subscription_row(conn, r["telegram_user_id"])
            access = access_state_from_rows(r, sub, False)
            vac = vacation_info(vacation_row(conn, r["telegram_user_id"]))
            result.append({
                "telegram_user_id": r["telegram_user_id"], "name": r["name"], "username": r["telegram_username"],
                "status": r["status"], "goal": r["primary_goal"], "goals": selected_goals(dict(r)), "level": r["level"],
                "experience_years": r["experience_years"], "weeks_to_event": r["weeks_to_event"], "updated_at": r["updated_at"],
                "alert": client_alert(conn, r["telegram_user_id"]), "access": access, "vacation": vac,
                "program": {"title": program["title"], "phase": program["phase"], "status": program["status"]} if program else None,
            })
    return result


@app.get("/coach/clients/{client_id}")
def coach_client_detail(client_id: str, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        programs = conn.execute("SELECT * FROM programs WHERE client_id=? ORDER BY id DESC LIMIT 10", (client_id,)).fetchall()
        metrics = conn.execute("SELECT * FROM progress_metrics WHERE client_id=? ORDER BY measured_at DESC,id DESC LIMIT 30", (client_id,)).fetchall()
        feedback = conn.execute("SELECT f.*,w.workout_date,w.title FROM workout_feedback f JOIN workout_assignments w ON w.id=f.workout_id WHERE f.client_id=? ORDER BY f.id DESC LIMIT 30", (client_id,)).fetchall()
        workouts = conn.execute("SELECT * FROM workout_assignments WHERE client_id=? ORDER BY workout_date DESC,id DESC LIMIT 250", (client_id,)).fetchall()
        sub = subscription_row(conn, client_id)
        vac = vacation_info(vacation_row(conn, client_id))
        alert = client_alert(conn, client_id)
    ws=[]
    for w in workouts:
        d = client_workout_view(w, include_content=True)
        ws.append(d)
    return {
        "profile": row_to_profile(row).model_dump(), "alert": alert, "subscription": dict(sub) if sub else None, "vacation": vac,
        "access": access_state_from_rows(row, sub, False),
        "programs": [dict(p) | {"plan": safe_json(p["plan_json"], {})} for p in programs],
        "metrics": [dict(x) for x in metrics], "feedback": [dict(x) for x in feedback], "workouts": ws,
    }


@app.patch("/coach/clients/{client_id}/status")
def coach_set_status(client_id: str, payload: StatusUpdate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        cur = conn.execute("UPDATE clients SET status=?,updated_at=? WHERE telegram_user_id=?", (payload.status.value, utc_now(), client_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True, "status": payload.status.value}


@app.patch("/coach/clients/{client_id}/notes")
def coach_set_notes(client_id: str, payload: CoachNoteUpdate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        cur = conn.execute("UPDATE clients SET coach_notes=?,updated_at=? WHERE telegram_user_id=?", (payload.coach_notes, utc_now(), client_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True}


@app.post("/coach/clients/{client_id}/program")
async def coach_create_program(client_id: str, payload: ProgramCreate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach = coach_user(x_telegram_init_data)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        preview = build_plan(plan_request_from_row(row))
        if payload.activate:
            conn.execute("UPDATE programs SET status='ARCHIVE',updated_at=? WHERE client_id=? AND status='ACTIVE'", (utc_now(), client_id))
        status = "ACTIVE" if payload.activate else "DRAFT"
        title = payload.title or f"{GOAL_LABELS.get(row['primary_goal'], row['primary_goal'])} · {preview.phase}"
        plan_json = json.dumps(preview.model_dump(mode="json"), ensure_ascii=False)
        now = utc_now()
        if USE_POSTGRES:
            cur = conn.execute("INSERT INTO programs(client_id,title,goal,phase,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (client_id, title, row["primary_goal"], preview.phase, status, plan_json, now, now))
            inserted = cur.fetchone(); program_id = inserted["id"] if inserted else None
        else:
            cur = conn.execute("INSERT INTO programs(client_id,title,goal,phase,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (client_id, title, row["primary_goal"], preview.phase, status, plan_json, now, now))
            program_id = cur.lastrowid
        if payload.activate and row["status"] in {"NEW", "WAITING_APPROVAL", "ASSESSMENT"}:
            conn.execute("UPDATE clients SET status='ACTIVE',updated_at=? WHERE telegram_user_id=?", (now, client_id))
        if payload.generate_workouts:
            start = parse_iso_date(payload.start_date) or date.today()
            generate_intro_workouts(conn, row, program_id, start, weeks=4)
        conn.commit()
    await notify_client(client_id, {"type": "program_updated"})
    try:
        await telegram_api("sendMessage", {"chat_id": int(client_id), "text": "Тренер назначил/обновил вашу программу M17. Откройте приложение, чтобы увидеть тренировки."})
    except Exception:
        pass
    return {"ok": True, "program_id": program_id, "status": status, "plan": preview.model_dump(mode="json")}


@app.patch("/coach/clients/{client_id}/subscription")
async def coach_subscription(client_id: str, payload: SubscriptionUpdate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    now = utc_now()
    with db_conn() as conn:
        if not conn.execute("SELECT 1 AS ok FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Client not found")
        conn.execute(
            """
            INSERT INTO subscriptions(client_id,start_date,paid_until,grace_until,amount,currency,payment_status,access_enabled,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_id) DO UPDATE SET start_date=excluded.start_date,paid_until=excluded.paid_until,grace_until=excluded.grace_until,amount=excluded.amount,currency=excluded.currency,payment_status=excluded.payment_status,access_enabled=excluded.access_enabled,updated_at=excluded.updated_at
            """,
            (client_id, payload.start_date, payload.paid_until, payload.grace_until, payload.amount, payload.currency, payload.payment_status, 1 if payload.access_enabled else 0, now),
        )
        if payload.access_enabled and payload.payment_status == "PAID":
            conn.execute("UPDATE clients SET status='ACTIVE',updated_at=? WHERE telegram_user_id=?", (now, client_id))
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone()
        sub = subscription_row(conn, client_id)
        state = access_state_from_rows(row, sub, False)
    await notify_client(client_id, {"type": "access_updated", "access": state})
    return {"ok": True, "access": state}


@app.get("/coach/payments")
def coach_payments(x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        return payment_summary(conn)


async def send_payment_reminders_internal():
    """Send exactly one payment reminder, 2 days before paid_until.

    The reminder is idempotent per client and paid-until date. Expiry/access
    rules are handled separately by the subscription/access logic.
    """
    sent = 0
    today = date.today()
    with db_conn() as conn:
        rows = conn.execute("SELECT c.telegram_user_id,c.name,s.paid_until,s.access_enabled FROM clients c JOIN subscriptions s ON c.telegram_user_id=s.client_id WHERE s.paid_until IS NOT NULL AND s.access_enabled=1").fetchall()
        for r in rows:
            paid = parse_iso_date(r["paid_until"])
            if not paid:
                continue
            days = (paid - today).days
            if days != 2:
                continue
            exists = conn.execute("SELECT 1 AS ok FROM payment_reminders WHERE client_id=? AND paid_until=? AND days_before=?", (r["telegram_user_id"], r["paid_until"], 2)).fetchone()
            if exists:
                continue
            try:
                text = f"M17: напоминание об оплате. До окончания оплаченного периода осталось 2 дня. Оплачено до {paid.isoformat()}. Для продления свяжитесь с тренером."
                await telegram_api("sendMessage", {"chat_id": int(r["telegram_user_id"]), "text": text})
                conn.execute("INSERT INTO payment_reminders(client_id,paid_until,days_before,sent_at) VALUES(?,?,?,?)", (r["telegram_user_id"], r["paid_until"], 2, utc_now()))
                sent += 1
            except Exception:
                pass
        conn.commit()
    return {"ok": True, "sent": sent, "days_before": 2}


@app.post("/coach/payments/send-reminders")
async def coach_send_payment_reminders(x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    return await send_payment_reminders_internal()


@app.post("/tasks/payment-reminders")
async def task_payment_reminders(x_cron_secret: Optional[str] = Header(default=None)):
    if not CRON_SECRET or not x_cron_secret or not hmac.compare_digest(x_cron_secret, CRON_SECRET):
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    return await send_payment_reminders_internal()


@app.post("/coach/clients/{client_id}/workouts")
async def coach_add_workout(client_id: str, payload: WorkoutCreate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        if not conn.execute("SELECT 1 AS ok FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Client not found")
        wid = insert_workout(conn, client_id, None, payload.workout_date, payload.title, payload.text)
        conn.commit()
    await notify_client(client_id, {"type": "workout_updated", "workout_id": wid})
    try:
        await telegram_api("sendMessage", {"chat_id": int(client_id), "text": f"Тренер добавил тренировку на {payload.workout_date}: {payload.title}."})
    except Exception:
        pass
    return {"ok": True, "workout_id": wid}


@app.patch("/coach/workouts/{workout_id}")
async def coach_edit_workout(workout_id: int, payload: WorkoutEdit, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach = coach_user(x_telegram_init_data)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM workout_assignments WHERE id=?", (workout_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workout not found")
        conn.execute(
            "INSERT INTO workout_revisions(workout_id,client_id,old_version,old_date,old_title,old_content_json,changed_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (workout_id, row["client_id"], row["version"], row["workout_date"], row["title"], row["content_json"], str(coach["id"]), utc_now()),
        )
        old_content = safe_json(row["content_json"], {})
        new_text = payload.text if payload.text is not None else old_content.get("text", "")
        new_date = payload.workout_date or row["workout_date"]
        new_title = payload.title or row["title"]
        new_version = int(row["version"]) + 1
        conn.execute("UPDATE workout_assignments SET workout_date=?,title=?,content_json=?,version=?,updated_at=? WHERE id=?", (new_date, new_title, json.dumps({"text": new_text}, ensure_ascii=False), new_version, utc_now(), workout_id))
        conn.commit()
        client_id = str(row["client_id"])
    await notify_client(client_id, {"type": "workout_updated", "workout_id": workout_id, "version": new_version})
    try:
        await telegram_api("sendMessage", {"chat_id": int(client_id), "text": f"Тренер изменил тренировку «{new_title}» на {new_date}. Откройте M17, чтобы увидеть обновление."})
    except Exception:
        pass
    return {"ok": True, "version": new_version}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text", "")
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = from_user.get("id")
    if chat_id and text.startswith("/id"):
        await telegram_api("sendMessage", {"chat_id": chat_id, "text": f"Ваш Telegram ID: {user_id}"})
    elif chat_id and text.startswith("/start"):
        role = "Тренер + спортсмен" if is_coach_id(str(user_id)) else "Кабинет клиента"
        app_url = f"{PUBLIC_URL}/?v={APP_VERSION}"
        await telegram_api("sendMessage", {"chat_id": chat_id, "text": f"M17 · {role}. Откройте приложение.", "reply_markup": {"inline_keyboard": [[{"text": "Открыть M17", "web_app": {"url": app_url}}]]}})
    return {"ok": True}
