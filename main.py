import json
import os
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("M17_DB_PATH", str(BASE_DIR / "m17_clients.db")))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

app = FastAPI(title="M17 Training System", version="0.2")


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


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


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
    # If there are still too many sessions, trim the lowest-volume secondary blocks.
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
        notes.append("Стаж 5+ лет: нужен анализ прошлых циклов и текущих ограничителей, а не стартовый шаблон.")
    if req.age is not None and req.age >= 50:
        notes.append("Восстановление и переносимость объёма нужно контролировать по фактической реакции, а не только по календарю.")
    if req.limitations:
        notes.append("Указаны ограничения: упражнения должны получать допустимые замены и масштабирование.")
    return " ".join(notes) or None


def build_plan(req: PlanRequest) -> PlanPreview:
    p = phase(req)

    if req.goal in {Goal.ironman, Goal.half_ironman, Goal.olympic, Goal.sprint}:
        swim, bike, run, brick = {
            Goal.ironman: (2, 2, 2, 1),
            Goal.half_ironman: (2, 2, 2, 1),
            Goal.olympic: (2, 2, 2, 1),
            Goal.sprint: (1, 2, 2, 1),
        }[req.goal]
        strength = 2 if p in {"base", "general"} else 1
        functional = 1 if p in {"base", "build", "general"} else 0
        blocks = [
            TrainingBlock(name="Swim", sessions=swim, priority="primary", note="Техника + аэробная / интервальная работа"),
            TrainingBlock(name="Bike", sessions=bike, priority="primary", note="Выносливость, темп, порог, специфика дистанции"),
            TrainingBlock(name="Run", sessions=run, priority="primary", note="Лёгкий, длинный, темповой / пороговый бег"),
            TrainingBlock(name="Brick", sessions=brick, priority="primary", note="Адаптация велосипед → бег"),
            TrainingBlock(name="Strength", sessions=strength, priority="support", note="Сила всего тела, односторонняя работа, задняя цепь, корпус"),
            TrainingBlock(name="Functional Strength", sessions=functional, priority="support", note="Переноски, sled, гири, простая функциональная работа"),
            TrainingBlock(name="Mobility / Prehab", sessions=1, priority="support", note="Плечи, грудной отдел, таз, голеностоп, стопа"),
        ]
    elif req.goal == Goal.crossfit:
        wl = 2 if req.level in {Level.advanced, Level.competitive} else 1
        gym = 2 if req.level in {Level.advanced, Level.competitive} else 1
        metcon = 3 if req.level == Level.competitive else 2
        blocks = [
            TrainingBlock(name="Strength", sessions=2, priority="primary", note="Присед, тяга, жим, тяговые движения"),
            TrainingBlock(name="Weightlifting", sessions=wl, priority="primary", note="Рывок, взятие, толчок, техника + мощность"),
            TrainingBlock(name="Gymnastics", sessions=gym, priority="primary", note="Подтягивания, TTB, HSPU, стойка, muscle-up прогрессии"),
            TrainingBlock(name="Aerobic Conditioning", sessions=2, priority="primary", note="Бег, гребля, велосипед, ski; устойчивый темп"),
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
        warning = "Для полного Ironman доступное время очень ограничено. Нужно приоритизировать ключевые endurance-сессии и оценить реалистичность цели."
    if req.goal == Goal.crossfit and req.level == Level.competitive and req.days_per_week < 5:
        warning = "Для соревновательного CrossFit при таком числе дней часть компонентов придётся совмещать внутри одной сессии."

    return PlanPreview(
        goal=req.goal,
        phase=p,
        total_sessions=sum(b.sessions for b in blocks),
        blocks=blocks,
        warning=warning,
        profile_note=profile_note(req),
    )


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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "telegram_configured": bool(TOKEN and PUBLIC_URL),
        "version": "0.2",
        "client_db": True,
    }


@app.post("/clients/profile", response_model=SavedClientProfile)
def save_profile(profile: ClientProfile):
    updated_at = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO clients (
                telegram_user_id, telegram_username, name, sex, age, height_cm, weight_kg,
                experience_years, primary_goal, level, days_per_week, hours_per_week,
                weeks_to_event, equipment_json, limitations, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username=excluded.telegram_username,
                name=excluded.name,
                sex=excluded.sex,
                age=excluded.age,
                height_cm=excluded.height_cm,
                weight_kg=excluded.weight_kg,
                experience_years=excluded.experience_years,
                primary_goal=excluded.primary_goal,
                level=excluded.level,
                days_per_week=excluded.days_per_week,
                hours_per_week=excluded.hours_per_week,
                weeks_to_event=excluded.weeks_to_event,
                equipment_json=excluded.equipment_json,
                limitations=excluded.limitations,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                profile.telegram_user_id, profile.telegram_username, profile.name, profile.sex.value,
                profile.age, profile.height_cm, profile.weight_kg, profile.experience_years,
                profile.primary_goal.value, profile.level.value, profile.days_per_week,
                profile.hours_per_week, profile.weeks_to_event, json.dumps(profile.equipment, ensure_ascii=False),
                profile.limitations, profile.notes, updated_at,
            ),
        )
        conn.commit()
    return SavedClientProfile(**profile.model_dump(), updated_at=updated_at)


@app.get("/clients/profile/{telegram_user_id}", response_model=SavedClientProfile)
def get_profile(telegram_user_id: str):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE telegram_user_id = ?", (telegram_user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Client profile not found")
    data = dict(row)
    return SavedClientProfile(
        telegram_user_id=data["telegram_user_id"],
        telegram_username=data["telegram_username"],
        name=data["name"],
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height_cm"],
        weight_kg=data["weight_kg"],
        experience_years=data["experience_years"],
        primary_goal=data["primary_goal"],
        level=data["level"],
        days_per_week=data["days_per_week"],
        hours_per_week=data["hours_per_week"],
        weeks_to_event=data["weeks_to_event"],
        equipment=json.loads(data["equipment_json"] or "[]"),
        limitations=data["limitations"],
        notes=data["notes"],
        updated_at=data["updated_at"],
    )


@app.post("/plans/preview", response_model=PlanPreview)
def preview_plan(payload: PlanRequest):
    return build_plan(payload)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text", "")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if chat_id and text.startswith("/start"):
        await telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "M17: заполните профиль, выберите цель и получите структуру тренировочного плана.",
                "reply_markup": {
                    "inline_keyboard": [[{
                        "text": "Открыть M17 Training",
                        "web_app": {"url": PUBLIC_URL},
                    }]]
                },
            },
        )
    return {"ok": True}
