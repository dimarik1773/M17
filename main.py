import os
from enum import Enum
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

app = FastAPI(title="Training Telegram Mini App", version="0.2-tablet")


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


class PlanRequest(BaseModel):
    goal: Goal
    level: Level = Level.beginner
    days_per_week: int = Field(default=4, ge=2, le=7)
    hours_per_week: float = Field(default=6, ge=2, le=30)
    weeks_to_event: Optional[int] = Field(default=None, ge=1, le=104)


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
    order = ["Mobility / Prehab", "Functional Strength", "Conditioning", "Strength", "Zone 2"]
    for name in order:
        for block in blocks:
            while total > max_sessions and block.name == name and block.sessions > 0:
                block.sessions -= 1
                total -= 1
    return [b for b in blocks if b.sessions > 0]


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
            TrainingBlock(name="Run", sessions=run, priority="primary", note="Легкий, длинный, темповой / пороговый бег"),
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
    return PlanPreview(
        goal=req.goal,
        phase=p,
        total_sessions=sum(b.sessions for b in blocks),
        blocks=blocks,
        warning=warning,
    )


async def telegram_api(method: str, payload: dict):
    if not TOKEN:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload)
        response.raise_for_status()
        return response.json()


@app.on_event("startup")
async def register_webhook():
    if TOKEN and PUBLIC_URL:
        payload = {"url": f"{PUBLIC_URL}/telegram/webhook", "drop_pending_updates": False}
        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET
        try:
            await telegram_api("setWebhook", payload)
        except Exception as exc:
            print(f"Webhook registration failed: {exc}")


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/health")
def health():
    return {"status": "ok", "telegram_configured": bool(TOKEN and PUBLIC_URL)}


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
                "text": "Выберите цель и получите структуру тренировочного плана.",
                "reply_markup": {
                    "inline_keyboard": [[{
                        "text": "Открыть тренировки",
                        "web_app": {"url": PUBLIC_URL},
                    }]]
                },
            },
        )
    return {"ok": True}
