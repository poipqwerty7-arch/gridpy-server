# GridPy Server

## Что тут есть
- `main.py` — FastAPI сервер: регистрация, логин, WebSocket синхронизация игроков
- `requirements.txt` — зависимости
- `client_example.py` — пример клиента на Kivy для Pydroid
- `render.yaml` — конфиг для автодеплоя на Render

## Как задеплоить на Render

1. Залей эти файлы в GitHub-репозиторий (например `gridpy-server`)
2. На Render: **New → Web Service → Git Provider → GitHub**
3. Выбери репозиторий `gridpy-server`
4. Render сам подхватит `render.yaml`, но проверь:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. В разделе **Environment** добавь переменную:
   - `DATABASE_URL` = строка подключения от твоей Postgres базы (gridpy-db)
   - `JWT_SECRET` = любая случайная строка (например сгенерируй на randomkeygen.com)
6. Нажми **Create Web Service**
7. Через пару минут получишь URL вида `https://gridpy-server.onrender.com`

## Проверка что сервер работает

Открой в браузере:
```
https://gridpy-server.onrender.com/
```
Должно показать: `{"status": "GridPy Server работает 🚀"}`

## API эндпоинты

- `POST /register` — {"username": "...", "password": "..."} → токен
- `POST /login` — {"username": "...", "password": "..."} → токен
- `POST /games/save` — сохранить игру (код на pygame)
- `GET /games/list` — список всех игр
- `WS /ws/{room_id}/{username}` — WebSocket для синхронизации в реальном времени

## Клиент (Pydroid)

1. В Pydroid: `pip install kivy websocket-client requests`
2. Скопируй `client_example.py`
3. Замени `SERVER_URL` на свой Render-URL
4. Запусти

⚠️ Важно: бесплатный план Render "засыпает" после 15 минут неактивности.
Первый запрос после сна может занять 30-50 секунд — это нормально.
