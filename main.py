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
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
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
    return {"token": token, "user_id": user_id, "username": req.username}


@app.post("/login")
async def login(req: LoginRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, password_hash FROM users WHERE username = $1", req.username)

    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_token(row["id"], req.username)
    return {"token": token, "user_id": row["id"], "username": req.username}


# ---------- REST: Игры (сохранение кода) ----------
class SaveGameRequest(BaseModel):
    token: str
    name: str
    code: str


@app.post("/games/save")
async def save_game(req: SaveGameRequest):
    user = decode_token(req.token)

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

    async def broadcast(self, message: dict, exclude: str = None):
        data = json.dumps(message)
        for username, ws in list(self.players.items()):
            if username != exclude:
                try:
                    await ws.send_text(data)
                except Exception:
                    pass

    def add_player(self, username: str, ws: WebSocket):
        self.players[username] = ws
        self.player_state[username] = {"x": 0, "y": 0}

    def remove_player(self, username: str):
        self.players.pop(username, None)
        self.player_state.pop(username, None)


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
    room.add_player(username, websocket)

    # сообщаем всем, что игрок зашёл
    await room.broadcast({
        "type": "player_joined",
        "username": username,
        "players": list(room.players.keys())
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
