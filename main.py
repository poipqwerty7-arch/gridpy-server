"""
GridPy Server — сервер для платформы GridPy
Функционал:
- Регистрация / логин (аккаунты в PostgreSQL)
- WebSocket синхронизация игроков в реальном времени
- Комнаты (rooms) — группы игроков в одной игре
"""

import os
import json
import bcrypt
import asyncpg
import jwt
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List

app = FastAPI(title="GridPy Server")

# ---------- Безопасный скриптовый язык GridPy ----------
# Игроки не пишут произвольный Python — только эти команды, по одной на строку.
# Это исключает выполнение вредоносного кода (никакого exec/eval).
import re

SCRIPT_COMMAND_PATTERNS = {
    "SET_GRAVITY": re.compile(r"^SET_GRAVITY\s+(-?\d+(\.\d+)?)$"),
    "SET_BACKGROUND": re.compile(r"^SET_BACKGROUND\s+#[0-9A-Fa-f]{6}$"),
    "ON_KEY_LEFT": re.compile(r"^ON_KEY_LEFT\s+MOVE\s+(-?\d+(\.\d+)?)$"),
    "ON_KEY_RIGHT": re.compile(r"^ON_KEY_RIGHT\s+MOVE\s+(-?\d+(\.\d+)?)$"),
    "ON_KEY_UP": re.compile(r"^ON_KEY_UP\s+JUMP\s+(\d+(\.\d+)?)$"),
    "PLATFORM": re.compile(r"^PLATFORM\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)$"),
    "WIN_ZONE": re.compile(r"^WIN_ZONE\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)$"),
}


def validate_script(code: str):
    """Проверяет, что каждая непустая строка соответствует одной из разрешённых
    команд. Возвращает (True, None) или (False, "текст ошибки с номером строки")."""
    lines = code.splitlines()
    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        keyword = line.split()[0]
        pattern = SCRIPT_COMMAND_PATTERNS.get(keyword)
        if not pattern or not pattern.match(line):
            return False, f"Строка {i}: неизвестная или неверная команда — «{line}»"
    return True, None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Настройки ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "change_this_secret_in_production")
JWT_ALGORITHM = "HS256"

db_pool: asyncpg.Pool | None = None


# ---------- Модели запросов ----------
class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- Подключение к БД ----------
@app.on_event("startup")
async def startup():
    global db_pool
    # Render даёт URL вида postgresql://..., asyncpg требует чуть другой формат
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    db_pool = await asyncpg.create_pool(url, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                skin_color TEXT DEFAULT '#5C6BFA',
                skin_shape TEXT DEFAULT 'circle',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS skin_color TEXT DEFAULT '#5C6BFA';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS skin_shape TEXT DEFAULT 'circle';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_base64 TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_approved BOOLEAN DEFAULT FALSE;")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id SERIAL PRIMARY KEY,
                owner_id INTEGER REFERENCES users(id),
                name TEXT NOT NULL,
                code TEXT NOT NULL,
                published BOOLEAN DEFAULT FALSE,
                max_players INTEGER DEFAULT 10,
                total_plays INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        # миграция для уже существующей таблицы (если сервер обновляется)
        await conn.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS published BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS max_players INTEGER DEFAULT 10;")
        await conn.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS total_plays INTEGER DEFAULT 0;")
    print("✅ База данных подключена и таблицы готовы")


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ---------- Вспомогательные функции ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token(user_id: int, username: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Невалидный токен")


# ---------- REST: Аккаунты ----------
@app.get("/")
async def root():
    return {"status": "GridPy Server работает 🚀"}


@app.post("/register")
async def register(req: RegisterRequest):
    if len(req.username) < 3:
        raise HTTPException(status_code=400, detail="Логин слишком короткий (мин. 3 символа)")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль слишком короткий (мин. 4 символа)")

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = $1", req.username)
        if existing:
            raise HTTPException(status_code=400, detail="Такой логин уже занят")

        password_hash = hash_password(req.password)
        row = await conn.fetchrow(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2) RETURNING id",
            req.username, password_hash
        )
        user_id = row["id"]

    token = create_token(user_id, req.username)
    return {"token": token, "user_id": user_id, "username": req.username,
            "skin_color": "#5C6BFA", "skin_shape": "circle"}


@app.post("/login")
async def login(req: LoginRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash, skin_color, skin_shape FROM users WHERE username = $1", req.username
        )

    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_token(row["id"], req.username)
    return {
        "token": token, "user_id": row["id"], "username": req.username,
        "skin_color": row["skin_color"] or "#5C6BFA",
        "skin_shape": row["skin_shape"] or "circle"
    }


class UpdateSkinRequest(BaseModel):
    token: str
    skin_color: str
    skin_shape: str = "circle"


@app.post("/profile/skin")
async def update_skin(req: UpdateSkinRequest):
    user = decode_token(req.token)

    if not req.skin_color.startswith("#") or len(req.skin_color) != 7:
        raise HTTPException(status_code=400, detail="Неверный формат цвета")
    if req.skin_shape not in ("circle", "square", "triangle"):
        raise HTTPException(status_code=400, detail="Неверная форма")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET skin_color = $1, skin_shape = $2 WHERE id = $3",
            req.skin_color, req.skin_shape, user["user_id"]
        )
    return {"status": "скин обновлён", "skin_color": req.skin_color, "skin_shape": req.skin_shape}


class UploadAvatarRequest(BaseModel):
    token: str
    image_base64: str


@app.post("/profile/avatar")
async def upload_avatar(req: UploadAvatarRequest):
    user = decode_token(req.token)

    # грубая проверка размера (base64 ~ на треть больше исходных байт)
    if len(req.image_base64) > 900_000:  # ~ примерно до 650KB картинки
        raise HTTPException(status_code=400, detail="Файл слишком большой")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET avatar_base64 = $1, avatar_approved = FALSE WHERE id = $2",
            req.image_base64, user["user_id"]
        )
    return {"status": "Аватар отправлен на модерацию"}


@app.get("/profile/avatar/{username}")
async def get_avatar(username: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT avatar_base64, avatar_approved FROM users WHERE username = $1", username
        )
    if not row or not row["avatar_base64"]:
        return {"has_avatar": False, "approved": False, "image_base64": None}
    return {
        "has_avatar": True,
        "approved": row["avatar_approved"],
        "image_base64": row["avatar_base64"]
    }


# ---------- REST: Игры (сохранение кода) ----------
class SaveGameRequest(BaseModel):
    token: str
    name: str
    code: str


@app.post("/games/save")
async def save_game(req: SaveGameRequest):
    user = decode_token(req.token)

    is_valid, error = validate_script(req.code)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO games (owner_id, name, code)
               VALUES ($1, $2, $3) RETURNING id""",
            user["user_id"], req.name, req.code
        )
    return {"game_id": row["id"], "status": "сохранено"}


class UpdateGameCodeRequest(BaseModel):
    token: str
    game_id: int
    code: str


@app.post("/games/update_code")
async def update_game_code(req: UpdateGameCodeRequest):
    user = decode_token(req.token)

    is_valid, error = validate_script(req.code)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT owner_id FROM games WHERE id = $1", req.game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        if game["owner_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="Это не твоя игра")

        await conn.execute(
            "UPDATE games SET code = $1, updated_at = NOW() WHERE id = $2",
            req.code, req.game_id
        )
    return {"status": "обновлено"}


class GameSettingsRequest(BaseModel):
    token: str
    game_id: int
    name: str
    max_players: int


@app.post("/games/settings")
async def update_game_settings(req: GameSettingsRequest):
    user = decode_token(req.token)

    if req.max_players < 1 or req.max_players > 100:
        raise HTTPException(status_code=400, detail="Лимит игроков должен быть от 1 до 100")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Название не может быть пустым")

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT owner_id FROM games WHERE id = $1", req.game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        if game["owner_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="Это не твоя игра")

        await conn.execute(
            "UPDATE games SET name = $1, max_players = $2, updated_at = NOW() WHERE id = $3",
            req.name.strip(), req.max_players, req.game_id
        )
    return {"status": "настройки сохранены"}


class PublishRequest(BaseModel):
    token: str
    game_id: int
    published: bool = True


@app.post("/games/publish")
async def publish_game(req: PublishRequest):
    user = decode_token(req.token)

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT owner_id FROM games WHERE id = $1", req.game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        if game["owner_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="Это не твоя игра")

        await conn.execute(
            "UPDATE games SET published = $1, updated_at = NOW() WHERE id = $2",
            req.published, req.game_id
        )
    return {"status": "опубликовано" if req.published else "снято с публикации"}


@app.get("/games/list")
async def list_games():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT games.id, games.name, users.username, games.created_at,
                      games.max_players, games.total_plays
               FROM games JOIN users ON games.owner_id = users.id
               WHERE games.published = TRUE
               ORDER BY games.created_at DESC LIMIT 50"""
        )
    result = []
    for r in rows:
        d = dict(r)
        room = rooms.get(f"game_{d['id']}")
        d["online"] = len(room.players) if room else 0
        result.append(d)
    return result


@app.get("/games/my")
async def my_games(token: str):
    """Все игры текущего пользователя, включая неопубликованные (для редактора)"""
    user = decode_token(token)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, published, max_players, total_plays, created_at
               FROM games WHERE owner_id = $1 ORDER BY created_at DESC""",
            user["user_id"]
        )
    result = []
    for r in rows:
        d = dict(r)
        room = rooms.get(f"game_{d['id']}")
        d["online"] = len(room.players) if room else 0
        result.append(d)
    return result


@app.get("/games/{game_id}")
async def get_game(game_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM games WHERE id = $1", game_id)
    if not row:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    d = dict(row)
    room = rooms.get(f"game_{game_id}")
    d["online"] = len(room.players) if room else 0
    return d


# ---------- WebSocket: комнаты и синхронизация игроков ----------
class Room:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.players: Dict[str, WebSocket] = {}
        self.player_state: Dict[str, dict] = {}
        self.player_skins: Dict[str, dict] = {}

    async def broadcast(self, message: dict, exclude: str = None):
        data = json.dumps(message)
        for username, ws in list(self.players.items()):
            if username != exclude:
                try:
                    await ws.send_text(data)
                except Exception:
                    pass

    def add_player(self, username: str, ws: WebSocket, skin_color: str = "#5C6BFA", skin_shape: str = "circle"):
        self.players[username] = ws
        self.player_state[username] = {"x": 0, "y": 0}
        self.player_skins[username] = {"color": skin_color, "shape": skin_shape}

    def remove_player(self, username: str):
        self.players.pop(username, None)
        self.player_state.pop(username, None)
        self.player_skins.pop(username, None)


rooms: Dict[str, Room] = {}


@app.websocket("/ws/{room_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str):
    await websocket.accept()

    # если это комната опубликованной игры — проверяем лимит игроков
    if room_id.startswith("game_"):
        try:
            game_id = int(room_id.replace("game_", ""))
        except ValueError:
            game_id = None

        if game_id is not None:
            async with db_pool.acquire() as conn:
                game = await conn.fetchrow(
                    "SELECT max_players FROM games WHERE id = $1", game_id
                )
            max_players = game["max_players"] if game else 10

            existing_room = rooms.get(room_id)
            current_count = len(existing_room.players) if existing_room else 0

            if current_count >= max_players:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Сервер переполнен"
                }))
                await websocket.close()
                return

            # засчитываем "заход" в игру (простая метрика total_plays)
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE games SET total_plays = total_plays + 1 WHERE id = $1", game_id
                )

    if room_id not in rooms:
        rooms[room_id] = Room(room_id)
    room = rooms[room_id]

    # получаем цвет и форму скина игрока из БД
    skin_color = "#5C6BFA"
    skin_shape = "circle"
    async with db_pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT skin_color, skin_shape FROM users WHERE username = $1", username
        )
        if user_row:
            skin_color = user_row["skin_color"] or skin_color
            skin_shape = user_row["skin_shape"] or skin_shape

    room.add_player(username, websocket, skin_color, skin_shape)

    # сообщаем всем, что игрок зашёл
    await room.broadcast({
        "type": "player_joined",
        "username": username,
        "skin_color": skin_color,
        "skin_shape": skin_shape,
        "players": list(room.players.keys()),
        "skins": room.player_skins
    })

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "move":
                room.player_state[username] = {"x": data["x"], "y": data["y"]}
                await room.broadcast({
                    "type": "player_moved",
                    "username": username,
                    "x": data["x"],
                    "y": data["y"]
                }, exclude=username)

            elif data.get("type") == "chat":
                await room.broadcast({
                    "type": "chat",
                    "username": username,
                    "text": data["text"]
                })

            elif data.get("type") == "event":
                # произвольные игровые события (для GridPy-фреймворка)
                await room.broadcast({
                    "type": "event",
                    "username": username,
                    "payload": data.get("payload", {})
                }, exclude=username)

    except WebSocketDisconnect:
        room.remove_player(username)
        await room.broadcast({
            "type": "player_left",
            "username": username,
            "players": list(room.players.keys())
        })
        if not room.players:
            rooms.pop(room_id, None)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
