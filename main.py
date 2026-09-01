import hashlib
import hmac
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("M17_DB_PATH", str(BASE_DIR / "m17_clients.db")))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
COACH_IDS = {x.strip() for x in os.getenv("COACH_TELEGRAM_IDS", "").split(",") if x.strip()}

app = FastAPI(title="M17 Training System", version="0.3")


class Goal(str, Enum):
    ironman = "ironman"
    half_ironman = "half_ironman"
    olympic = "olympic"
    sprint = "sprint"
    crossfit = "crossfit"
    fat_loss = "fat_loss"
    muscle_gain = "muscle_gain"


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
    assessment = "ASSESSMENT"
    active = "ACTIVE"
    paused = "PAUSED"
    rehab = "REHAB"
    completed = "COMPLETED"
    archive = "ARCHIVE"


class ClientProfile(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    telegram_username: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    sex: Sex = Sex.not_specified
    age: int = Field(ge=14, le=90)
    height_cm: Optional[float] = Field(default=None, ge=120, le=230)
    weight_kg: Optional[float] = Field(default=None, ge=35, le=300)
    experience_years: float = Field(default=0, ge=0, le=60)
    primary_goal: Goal
    level: Level = Level.beginner
    days_per_week: int = Field(default=4, ge=2, le=7)
    hours_per_week: float = Field(default=6, ge=2, le=30)
    weeks_to_event: Optional[int] = Field(default=None, ge=1, le=104)
    equipment: list[str] = Field(default_factory=list)
    limitations: Optional[str] = Field(default=None, max_length=1000)
    notes: Optional[str] = Field(default=None, max_length=2000)


class SavedClientProfile(ClientProfile):
    status: ClientStatus = ClientStatus.new
    coach_notes: Optional[str] = None
    created_at: str
    updated_at: str


class PlanRequest(BaseModel):
    goal: Goal
    level: Level = Level.beginner
    days_per_week: int = Field(default=4, ge=2, le=7)
    hours_per_week: float = Field(default=6, ge=2, le=30)
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
    wellbeing: Optional[int] = Field(default=None, ge=1, le=5)
    pain: bool = False
    duration_min: Optional[int] = Field(default=None, ge=0, le=1000)
    distance_km: Optional[float] = Field(default=None, ge=0, le=1000)
    avg_hr: Optional[int] = Field(default=None, ge=30, le=240)
    comment: Optional[str] = Field(default=None, max_length=1000)


class ProgramCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=150)
    activate: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table: str) -> set[str]:
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
                status TEXT NOT NULL DEFAULT 'NEW',
                coach_notes TEXT,
                created_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        cols = table_columns(conn, "clients")
        migrations = {
            "status": "ALTER TABLE clients ADD COLUMN status TEXT NOT NULL DEFAULT 'NEW'",
            "coach_notes": "ALTER TABLE clients ADD COLUMN coach_notes TEXT",
            "created_at": "ALTER TABLE clients ADD COLUMN created_at TEXT",
        }
        for col, sql in migrations.items():
            if col not in cols:
                conn.execute(sql)
        now = utc_now()
        conn.execute("UPDATE clients SET created_at = COALESCE(created_at, updated_at, ?)", (now,))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT,
                measured_at TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                workout_date TEXT NOT NULL,
                workout_name TEXT NOT NULL,
                completed INTEGER NOT NULL,
                rpe INTEGER,
                wellbeing INTEGER,
                pain INTEGER NOT NULL DEFAULT 0,
                duration_min INTEGER,
                distance_km REAL,
                avg_hr INTEGER,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(telegram_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
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
            if age_seconds > 7 * 24 * 3600:
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
    order = ["Mobility / Prehab", "Functional Strength", "Conditioning", "Zone 2", "Strength"]
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
        notes.append("Стаж 5+ лет: следующий цикл нужно выбирать с учётом прошлой тренировочной истории и ограничителей.")
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
            TrainingBlock(name="Swim", sessions=swim, priority="primary", note="Техника + аэробная / интервальная работа"),
            TrainingBlock(name="Bike", sessions=bike, priority="primary", note="Выносливость, темп, порог, специфика дистанции"),
            TrainingBlock(name="Run", sessions=run, priority="primary", note="Лёгкий, длинный, темповой / пороговый бег"),
            TrainingBlock(name="Brick", sessions=brick, priority="primary", note="Адаптация велосипед → бег"),
            TrainingBlock(name="Strength", sessions=strength, priority="support", note="Сила всего тела, задняя цепь, корпус, unilateral"),
            TrainingBlock(name="Functional Strength", sessions=functional, priority="support", note="Переноски, sled, гири, простая функциональная работа"),
            TrainingBlock(name="Mobility / Prehab", sessions=1, priority="support", note="Плечи, грудной отдел, таз, голеностоп, стопа"),
        ]
    elif req.goal == Goal.crossfit:
        wl = 2 if req.level in {Level.advanced, Level.competitive} else 1
        gym = 2 if req.level in {Level.advanced, Level.competitive} else 1
        metcon = 3 if req.level == Level.competitive else 2
        blocks = [
            TrainingBlock(name="Strength", sessions=2, priority="primary", note="Присед, тяга, жим, тяговые движения"),
            TrainingBlock(name="Weightlifting", sessions=wl, priority="primary", note="Рывок, взятие, толчок: техника + мощность"),
            TrainingBlock(name="Gymnastics", sessions=gym, priority="primary", note="Подтягивания, TTB, HSPU, стойка, muscle-up"),
            TrainingBlock(name="Aerobic Conditioning", sessions=2, priority="primary", note="Бег, гребля, велосипед, SkiErg"),
            TrainingBlock(name="Metcon", sessions=metcon, priority="primary", note="Couplets, triplets, chippers, EMOM / AMRAP / For Time"),
            TrainingBlock(name="Mobility / Prehab", sessions=1, priority="support", note="Подвижность и качество движений"),
        ]
    elif req.goal == Goal.fat_loss:
        strength = 3 if req.days_per_week <= 4 else 4
        blocks = [
            TrainingBlock(name="Strength", sessions=strength, priority="primary", note="Сохранение / набор мышечной массы"),
            TrainingBlock(name="Zone 2", sessions=2, priority="support", note="Низкоударная аэробная работа"),
            TrainingBlock(name="Conditioning", sessions=1, priority="support", note="Масштабируемая функциональная тренировка"),
            TrainingBlock(name="Mobility / Prehab", sessions=1, priority="support", note="Качество движения и восстановление"),
        ]
    else:
        strength = 3 if req.days_per_week == 3 else min(req.days_per_week, 5)
        blocks = [
            TrainingBlock(name="Hypertrophy Strength", sessions=strength, priority="primary", note="6–15 повторов, прогрессивная перегрузка, контроль темпа"),
            TrainingBlock(name="Zone 2", sessions=1, priority="support", note="Поддержание аэробной формы с низкой утомляемостью"),
            TrainingBlock(name="Mobility / Prehab", sessions=1, priority="support", note="Рабочая амплитуда и устойчивость тканей"),
        ]
    max_sessions = max(req.days_per_week, int(req.hours_per_week))
    blocks = cap_sessions(blocks, max_sessions)
    warning = None
    if req.goal == Goal.ironman and req.hours_per_week < 5:
        warning = "Для полного Ironman доступное время очень ограничено. Нужно приоритизировать ключевые endurance-сессии."
    if req.goal == Goal.crossfit and req.level == Level.competitive and req.days_per_week < 5:
        warning = "Для соревновательного CrossFit часть компонентов придётся совмещать внутри одной сессии."
    return PlanPreview(goal=req.goal, phase=p, total_sessions=sum(b.sessions for b in blocks), blocks=blocks, warning=warning, profile_note=profile_note(req))


def row_to_profile(row: sqlite3.Row) -> SavedClientProfile:
    d = dict(row)
    return SavedClientProfile(
        telegram_user_id=d["telegram_user_id"], telegram_username=d["telegram_username"], name=d["name"],
        sex=d["sex"], age=d["age"], height_cm=d["height_cm"], weight_kg=d["weight_kg"],
        experience_years=d["experience_years"], primary_goal=d["primary_goal"], level=d["level"],
        days_per_week=d["days_per_week"], hours_per_week=d["hours_per_week"], weeks_to_event=d["weeks_to_event"],
        equipment=json.loads(d["equipment_json"] or "[]"), limitations=d["limitations"], notes=d["notes"],
        status=d.get("status") or "NEW", coach_notes=d.get("coach_notes"),
        created_at=d.get("created_at") or d["updated_at"], updated_at=d["updated_at"],
    )


def plan_request_from_row(row: sqlite3.Row) -> PlanRequest:
    return PlanRequest(
        goal=row["primary_goal"], level=row["level"], days_per_week=row["days_per_week"],
        hours_per_week=row["hours_per_week"], weeks_to_event=row["weeks_to_event"], age=row["age"],
        experience_years=row["experience_years"], limitations=row["limitations"],
    )


def latest_program(conn, client_id: str):
    return conn.execute("SELECT * FROM programs WHERE client_id=? ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()


def client_alert(conn, client_id: str) -> Optional[str]:
    logs = conn.execute(
        "SELECT * FROM workout_logs WHERE client_id=? ORDER BY workout_date DESC, id DESC LIMIT 3", (client_id,)
    ).fetchall()
    if not logs:
        return None
    if any(bool(r["pain"]) for r in logs):
        return "Отмечена боль / дискомфорт"
    rpes = [r["rpe"] for r in logs if r["rpe"] is not None]
    if len(rpes) >= 2 and sum(rpes) / len(rpes) >= 9:
        return "Высокий RPE в последних тренировках"
    wellbeing = [r["wellbeing"] for r in logs if r["wellbeing"] is not None]
    if len(wellbeing) >= 2 and sum(wellbeing) / len(wellbeing) <= 2:
        return "Низкое самочувствие"
    if sum(1 for r in logs if not bool(r["completed"])) >= 2:
        return "Две или больше невыполненных тренировок"
    return None


async def telegram_api(method: str, payload: dict):
    if not TOKEN:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload)
        response.raise_for_status()
        return response.json()


@app.on_event("startup")
async def startup():
    init_db()
    if TOKEN and PUBLIC_URL:
        payload = {"url": f"{PUBLIC_URL}/telegram/webhook", "drop_pending_updates": False}
        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET
        try:
            await telegram_api("setWebhook", payload)
            print("Telegram webhook registered")
        except Exception as exc:
            print(f"Webhook registration failed: {exc}")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.head("/")
def index_head():
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok", "telegram_configured": bool(TOKEN and PUBLIC_URL), "version": "0.3", "client_db": True, "coach_configured": bool(COACH_IDS)}


@app.get("/auth/me")
def auth_me(x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=True)
    return {"telegram_user_id": str(user["id"]), "name": user.get("first_name", ""), "username": user.get("username"), "is_coach": is_coach_id(str(user["id"]))}


@app.post("/clients/profile", response_model=SavedClientProfile)
def save_profile(profile: ClientProfile, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=False)
    if user:
        profile.telegram_user_id = str(user["id"])
        profile.telegram_username = user.get("username") or profile.telegram_username
    now = utc_now()
    with db_conn() as conn:
        old = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (profile.telegram_user_id,)).fetchone()
        created_at = old["created_at"] if old and old["created_at"] else now
        status = old["status"] if old and old["status"] else ClientStatus.new.value
        coach_notes = old["coach_notes"] if old else None
        conn.execute(
            """
            INSERT INTO clients (telegram_user_id,telegram_username,name,sex,age,height_cm,weight_kg,experience_years,primary_goal,level,days_per_week,hours_per_week,weeks_to_event,equipment_json,limitations,notes,status,coach_notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET telegram_username=excluded.telegram_username,name=excluded.name,sex=excluded.sex,age=excluded.age,height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,experience_years=excluded.experience_years,primary_goal=excluded.primary_goal,level=excluded.level,days_per_week=excluded.days_per_week,hours_per_week=excluded.hours_per_week,weeks_to_event=excluded.weeks_to_event,equipment_json=excluded.equipment_json,limitations=excluded.limitations,notes=excluded.notes,updated_at=excluded.updated_at
            """,
            (profile.telegram_user_id, profile.telegram_username, profile.name, profile.sex.value, profile.age, profile.height_cm, profile.weight_kg, profile.experience_years, profile.primary_goal.value, profile.level.value, profile.days_per_week, profile.hours_per_week, profile.weeks_to_event, json.dumps(profile.equipment, ensure_ascii=False), profile.limitations, profile.notes, status, coach_notes, created_at, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (profile.telegram_user_id,)).fetchone()
    return row_to_profile(row)


@app.get("/clients/profile/{telegram_user_id}", response_model=SavedClientProfile)
def get_profile(telegram_user_id: str, x_telegram_init_data: Optional[str] = Header(default=None)):
    user = current_user(x_telegram_init_data, required=False)
    if user and str(user["id"]) != telegram_user_id and not is_coach_id(str(user["id"])):
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
        program = latest_program(conn, uid)
        metrics = conn.execute("SELECT * FROM progress_metrics WHERE client_id=? ORDER BY measured_at DESC,id DESC LIMIT 6", (uid,)).fetchall()
        logs = conn.execute("SELECT * FROM workout_logs WHERE client_id=? ORDER BY workout_date DESC,id DESC LIMIT 5", (uid,)).fetchall()
    return {
        "has_profile": True,
        "profile": row_to_profile(row).model_dump(),
        "program": dict(program) | {"plan": json.loads(program["plan_json"])} if program else None,
        "metrics": [dict(x) for x in metrics],
        "logs": [dict(x) for x in logs],
    }


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
        new_count = sum(1 for r in rows if r["status"] == "NEW")
        active_count = sum(1 for r in rows if r["status"] == "ACTIVE")
        alerts = []
        for r in rows:
            a = client_alert(conn, r["telegram_user_id"])
            if a:
                alerts.append({"client_id": r["telegram_user_id"], "name": r["name"], "message": a})
    return {"clients": len(rows), "new_clients": new_count, "active_clients": active_count, "alerts_count": len(alerts), "alerts": alerts[:10]}


@app.get("/coach/clients")
def coach_clients(status: Optional[str] = None, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        if status:
            rows = conn.execute("SELECT * FROM clients WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clients ORDER BY CASE status WHEN 'NEW' THEN 0 WHEN 'ASSESSMENT' THEN 1 WHEN 'ACTIVE' THEN 2 ELSE 3 END, updated_at DESC").fetchall()
        result = []
        for r in rows:
            program = latest_program(conn, r["telegram_user_id"])
            result.append({
                "telegram_user_id": r["telegram_user_id"], "name": r["name"], "username": r["telegram_username"],
                "status": r["status"], "goal": r["primary_goal"], "level": r["level"], "experience_years": r["experience_years"],
                "weeks_to_event": r["weeks_to_event"], "updated_at": r["updated_at"], "alert": client_alert(conn, r["telegram_user_id"]),
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
        logs = conn.execute("SELECT * FROM workout_logs WHERE client_id=? ORDER BY workout_date DESC,id DESC LIMIT 30", (client_id,)).fetchall()
        alert = client_alert(conn, client_id)
    return {
        "profile": row_to_profile(row).model_dump(),
        "alert": alert,
        "programs": [dict(p) | {"plan": json.loads(p["plan_json"])} for p in programs],
        "metrics": [dict(x) for x in metrics],
        "logs": [dict(x) for x in logs],
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
def coach_create_program(client_id: str, payload: ProgramCreate, x_telegram_init_data: Optional[str] = Header(default=None)):
    coach_user(x_telegram_init_data)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id=?", (client_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        preview = build_plan(plan_request_from_row(row))
        if payload.activate:
            conn.execute("UPDATE programs SET status='ARCHIVE',updated_at=? WHERE client_id=? AND status='ACTIVE'", (utc_now(), client_id))
        status = "ACTIVE" if payload.activate else "DRAFT"
        title = payload.title or f"{row['primary_goal']} · {preview.phase}"
        plan_json = json.dumps(preview.model_dump(mode="json"), ensure_ascii=False)
        now = utc_now()
        cur = conn.execute("INSERT INTO programs(client_id,title,goal,phase,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (client_id, title, row["primary_goal"], preview.phase, status, plan_json, now, now))
        if payload.activate and row["status"] in {"NEW", "ASSESSMENT"}:
            conn.execute("UPDATE clients SET status='ACTIVE',updated_at=? WHERE telegram_user_id=?", (now, client_id))
        conn.commit()
    return {"ok": True, "program_id": cur.lastrowid, "status": status, "plan": preview.model_dump(mode="json")}


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
        role = "Кабинет тренера" if is_coach_id(str(user_id)) else "Кабинет клиента"
        await telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": f"M17 · {role}. Откройте приложение.", "reply_markup": {"inline_keyboard": [[{"text": "Открыть M17", "web_app": {"url": PUBLIC_URL}}]]}},
        )
    return {"ok": True}
