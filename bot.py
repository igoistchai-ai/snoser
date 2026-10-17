#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DROPP / Yerevan — весь сервис в одном bot.py. Python 3.10+, только stdlib.

ЗАПУСК:       python bot.py
BUILD CHECK:  python -m py_compile bot.py
ТЕСТЫ:        python bot.py --self-test
TELEGRAM:     python bot.py --setup-telegram
НАСТРОЙКИ:    python bot.py --print-env
ПРОВЕРКА ENV: python bot.py --check-config
СПРАВКА:      python bot.py --help

Откройте http://localhost:3000. Демо-роли переключаются сверху.
Демо-вход: admin@dropp.am / artur@dropp.am / elena@dropp.am.
Демо-пароль: DroppDemo!2026. Для production задайте NODE_ENV=production,
DEMO_MODE=false, HTTPS PUBLIC_URL, ADMIN_PASSWORD от 16 символов и НОВЫЙ DB_PATH.

Все настройки можно поменять прямо в CONFIG ниже; переменные окружения
переопределяют CONFIG. .env, requirements.txt, Node.js и npm не требуются.
HTML, CSS, JavaScript, карта, SQLite, Telegram и самопроверки встроены в этот файл.

База создаётся автоматически: data/dropp-python.sqlite. В ней находятся
пользователи, заказы, чаты, сессии и фотографии. Это данные, а не зависимость;
для сохранения между запусками нужен постоянный диск с этим файлом.

Карта — собственная офлайн-схема центра Еревана, без внешних картографических
API. Маршруты схематичны; поиска адресов и дорожной навигации нет.
Камера/GPS работают через HTTPS или localhost; GPS — пока открыт браузер.
Telegram подключается вашим токеном BotFather и публичным HTTPS-адресом:
заполните TELEGRAM_* / PUBLIC_URL, запустите --setup-telegram, затем bot.py;
каждый пользователь привязывает Telegram через свой профиль и кнопку «Старт».
Курьер должен быть подтверждён и на линии. Первый принявший получает заказ.
Внешние сообщения не отправляются без вашего токена. --self-test работает офлайн.

Размещение: один Python-процесс за HTTPS reverse proxy; постоянный DB_PATH.
Для /api/events отключите буферизацию proxy. HOST / PORT задаются ниже.
На Render пустой PUBLIC_URL берётся из RENDER_EXTERNAL_URL автоматически;
PORT использует значение хостинга. Переменные задавайте в Environment сервиса.
Для сохранения SQLite на Render подключите Persistent Disk и укажите DB_PATH
внутри его mount path, например /var/data/dropp.sqlite при mount /var/data.
Обычный относительный путь к SQLite не делает диск Render постоянным.
Health check: /api/health. В интерфейсе до 1000 последних заказов; экспорт
администратора содержит всю историю. Платёжный эквайринг и фактические выплаты
не подключены: суммы в интерфейсе — стоимость заказов и начисления курьерам.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import collections
import contextlib
import csv
import dataclasses
import datetime as dt
import hashlib
import hmac
import http.server
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ВСЕ ENV ВНУТРИ ОДНОГО ФАЙЛА. Заполняйте здесь или через окружение хостинга.
CONFIG = {
    "NODE_ENV": "development",          # production для рабочего размещения
    "DEMO_MODE": "",                    # пусто = авто; true / false
    "HOST": "0.0.0.0",
    "PORT": "3000",
    "PUBLIC_URL": "",                   # пусто = адрес Render или localhost
    "DB_PATH": "./data/dropp-python.sqlite",
    "ADMIN_EMAIL": "admin@dropp.am",     # первый администратор
    "ADMIN_PASSWORD": "",               # production: минимум 16 символов
    "TELEGRAM_BOT_TOKEN": "",            # секретный токен от @BotFather
    "TELEGRAM_BOT_USERNAME": "",         # имя без @
    "TELEGRAM_MODE": "polling",          # polling / webhook
    "TELEGRAM_WEBHOOK_SECRET": "",       # webhook: 32+ букв/цифр/_/-
    "TRUST_PROXY": "false",             # true только за ВАШИМ reverse proxy
}

ROOT = Path(__file__).resolve().parent
STATUS = {"new": "Новый", "assigned": "Курьер назначен", "picked_up": "В пути",
          "delivered": "Доставлен", "cancelled": "Отменён"}
ACTIVE = ("assigned", "picked_up")
VEHICLES = ("bike", "scooter", "car", "walk")
DEMO_PASSWORD = "DroppDemo!2026"
MAX_BODY = 6 * 1024 * 1024
MAX_PHOTO = 4 * 1024 * 1024


@dataclasses.dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 3000
    public_url: str = "http://localhost:3000"
    db_path: str = ":memory:"
    production: bool = False
    demo: bool = True
    admin_email: str = "admin@dropp.am"
    admin_password: str = ""
    token: str = ""
    bot_name: str = ""
    telegram_mode: str = "polling"
    webhook_secret: str = ""
    trust_proxy: bool = False

    @classmethod
    def from_env(cls):
        values = {key: os.environ.get(key, str(value)) for key, value in CONFIG.items()}
        prod = values["NODE_ENV"].strip().lower() == "production"
        mode = values["DEMO_MODE"].strip().lower()
        if mode not in ("", "true", "false"):
            raise ValueError("DEMO_MODE: используйте true, false или пустую строку.")
        demo = mode == "true" if mode else not prod
        try:
            port = int(values["PORT"].strip() or ("10000" if os.environ.get("RENDER") == "true" else "3000"))
        except ValueError:
            raise ValueError("PORT: укажите целое число от 1 до 65535.") from None
        if not 1 <= port <= 65535:
            raise ValueError("PORT должен быть от 1 до 65535.")
        public = (values["PUBLIC_URL"].strip() or os.environ.get("RENDER_EXTERNAL_URL", "").strip() or f"http://localhost:{port}").rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(public)
            valid_url = parsed.scheme in ("http", "https") and parsed.hostname and not (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password)
            parsed.port  # Validate an explicitly supplied port as well.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("PUBLIC_URL: укажите полный адрес вида https://delivery.example.com без пути. На Render можно оставить пустым для автоматического определения.")
        database = values["DB_PATH"].strip() or str(CONFIG["DB_PATH"])
        if database != ":memory:":
            database = str((ROOT / database).resolve())
        cfg = cls(values["HOST"].strip() or "0.0.0.0", port, public, database, prod, demo,
                  values["ADMIN_EMAIL"].strip().lower(), values["ADMIN_PASSWORD"],
                  values["TELEGRAM_BOT_TOKEN"].strip(), values["TELEGRAM_BOT_USERNAME"].strip().lstrip("@"),
                  values["TELEGRAM_MODE"].strip().lower(), values["TELEGRAM_WEBHOOK_SECRET"],
                  values["TRUST_PROXY"].lower() == "true")
        issues = []
        if prod:
            if demo:
                issues.append("DEMO_MODE: установите false для production.")
            if not cfg.admin_password:
                issues.append("ADMIN_PASSWORD: не задан. Добавьте пароль администратора длиной от 16 символов.")
            elif len(cfg.admin_password) < 16:
                issues.append("ADMIN_PASSWORD: слишком короткий. Нужно минимум 16 символов.")
            if parsed.scheme != "https":
                issues.append("PUBLIC_URL: в production нужен https://. Укажите адрес сайта; на Render удалите PUBLIC_URL для автоматического определения.")
            if database == ":memory:":
                issues.append("DB_PATH: режим :memory: не сохраняет данные. Укажите путь к файлу SQLite.")
        if issues:
            raise ValueError("Проверьте ENV в настройках хостинга:\n - " + "\n - ".join(issues))
        if cfg.telegram_mode not in ("polling", "webhook"):
            raise ValueError("TELEGRAM_MODE: polling или webhook.")
        if cfg.token and (parsed.scheme != "https" or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", cfg.bot_name)):
            raise ValueError("Для Telegram укажите публичный HTTPS PUBLIC_URL и корректный TELEGRAM_BOT_USERNAME.")
        if cfg.token and cfg.telegram_mode == "webhook" and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", cfg.webhook_secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET: 32–256 букв, цифр, _ или -.")
        return cfg


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def fail(status, message):
    raise APIError(status, message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def millis():
    return int(time.time() * 1000)


def token_hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_urlsafe(16)
    derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
    return salt + ":" + derived.hex()


def check_password(password, stored):
    try:
        salt, digest = stored.split(":", 1)
        derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(derived, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def text_field(body, key, maximum=200, required=True, strip=True):
    value = body.get(key, "")
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        fail(400, f"Проверьте поле «{key}».")
    value = str(value).strip() if strip else str(value)
    if (required and not value) or len(value) > maximum:
        fail(400, f"Проверьте поле «{key}» (до {maximum} символов).")
    return value


def number_field(body, key, minimum, maximum, integer=False):
    value = body.get(key)
    if value is None or value == "" or isinstance(value, bool):
        fail(400, f"Укажите {key}.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        fail(400, f"Некорректное значение {key}.")
    if not math.isfinite(value) or not minimum <= value <= maximum or (integer and not value.is_integer()):
        fail(400, f"Некорректное значение {key}.")
    return int(value) if integer else value


def validate_email(email):
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        fail(400, "Введите корректный email.")


def validate_phone(phone):
    if not re.fullmatch(r"\+?[\d\s()\-]{6,24}", phone):
        fail(400, "Введите корректный телефон.")


def coordinates(body, prefix):
    return {prefix + "_lat": number_field(body, prefix + "_lat", 40.08, 40.30),
            prefix + "_lng": number_field(body, prefix + "_lng", 44.38, 44.65)}


def pricing(body):
    a, b, c, d = (math.radians(body[k]) for k in ("pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng"))
    hav = min(1.0, math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2)
    km = 6371 * 2 * math.atan2(math.sqrt(hav), math.sqrt(1 - hav)) * 1.35
    fee = math.ceil((800 + km * 160 + body["weight"] * 50) * (1.4 if body["priority"] == "express" else 1) / 50) * 50
    return {"fee": fee, "payout": math.floor(fee * .72 / 50 + .5) * 50}


def safe_user(user):
    if not user:
        return None
    result = {k: user[k] for k in ("id", "role", "name", "email", "phone", "vehicle", "color", "lat", "lng", "located_at", "created_at")}
    result.update(approved=bool(user["approved"]), online=bool(user["online"]), telegram_connected=bool(user["telegram_id"]))
    return result


SCHEMA = """
  PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','courier','client')),
    name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 1, online INTEGER NOT NULL DEFAULT 0,
    vehicle TEXT NOT NULL DEFAULT 'bike', color TEXT NOT NULL DEFAULT '#c5f56b',
    lat REAL, lng REAL, located_at TEXT, telegram_id TEXT UNIQUE, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS sessions (
    hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, expires INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL REFERENCES users(id),
    courier_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'new',
    pickup TEXT NOT NULL, dropoff TEXT NOT NULL, pickup_lat REAL NOT NULL, pickup_lng REAL NOT NULL,
    dropoff_lat REAL NOT NULL, dropoff_lng REAL NOT NULL, recipient TEXT NOT NULL, phone TEXT NOT NULL,
    description TEXT NOT NULL, weight REAL NOT NULL, fee INTEGER NOT NULL, payout INTEGER NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal', note TEXT NOT NULL DEFAULT '', track_token TEXT UNIQUE NOT NULL,
    request_key TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT
  );
  CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), status TEXT NOT NULL,
    actor_id INTEGER REFERENCES users(id), comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), user_id INTEGER NOT NULL REFERENCES users(id),
    stage TEXT NOT NULL, mime TEXT NOT NULL, bytes BLOB NOT NULL, comment TEXT NOT NULL, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, thread TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS telegram_links (hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS telegram_updates (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL DEFAULT 0, sent_at TEXT, error TEXT NOT NULL DEFAULT ''
  );
  CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id, created_at);
  CREATE INDEX IF NOT EXISTS idx_orders_courier ON orders(courier_id, status);
  CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread, id);
  CREATE INDEX IF NOT EXISTS idx_history_order ON history(order_id, id);
  CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent_at, next_attempt);
"""


@dataclasses.dataclass
class SQLResult:
    lastrowid: int
    rowcount: int


class Application:
    def __init__(self, settings):
        self.cfg = settings
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.events = collections.deque(maxlen=256)
        self.sequence = 0
        self.stream_counts = collections.Counter()
        self.limits = {}
        self.limit_lock = threading.Lock()
        self.outbox_lock = threading.Lock()
        self.workers = []
        if settings.db_path != ":memory:":
            Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.invalid_password = password_hash(secrets.token_urlsafe(24))
        try:
            self.bootstrap()
        except BaseException:
            self.db.close()
            raise

    def query(self, sql, *args):
        with self.lock:
            cursor = self.db.execute(sql, args)
            try:
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def one(self, sql, *args):
        with self.lock:
            cursor = self.db.execute(sql, args)
            try:
                row = cursor.fetchone()
                return dict(row) if row is not None else None
            finally:
                cursor.close()

    def execute(self, sql, *args):
        with self.lock:
            cursor = self.db.execute(sql, args)
            result = SQLResult(cursor.lastrowid, cursor.rowcount)
            cursor.close()
            return result

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def history(self, order_id, status, actor, comment="", stamp=None):
        self.execute("INSERT INTO history(order_id,status,actor_id,comment,created_at) VALUES (?,?,?,?,?)", order_id, status, actor, comment, stamp or now())

    def bootstrap(self):
        with self.transaction():
            mode = self.one("SELECT value FROM settings WHERE key='install_mode'")
            expected = "demo" if self.cfg.demo else "live"
            if mode and mode["value"] != expected:
                raise ValueError("Для перехода между demo и production задайте НОВЫЙ DB_PATH.")
            self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('install_mode',?)", expected)
            if not self.one("SELECT id FROM users WHERE role='admin'"):
                password = self.cfg.admin_password or (DEMO_PASSWORD if self.cfg.demo else secrets.token_urlsafe(24))
                self.execute("INSERT INTO users(role,name,email,phone,password,created_at) VALUES (?,?,?,?,?,?)", "admin", "Арман Саркисян", self.cfg.admin_email, "+374 91 000 100", password_hash(password), now())
                if not self.cfg.demo and not self.cfg.admin_password:
                    print("Initial admin password (save now): " + password, flush=True)
            if self.cfg.demo and not self.one("SELECT id FROM users WHERE role='courier'"):
                self.seed_demo()

    def seed_demo(self):
        pw = password_hash(DEMO_PASSWORD)
        couriers = [
            ("Артур Мкртчян", "artur@dropp.am", "+374 91 123 401", "bike", "#c5f56b", 40.1865, 44.5136, 1),
            ("Мариам Геворгян", "mariam@dropp.am", "+374 91 123 402", "car", "#b3a0ff", 40.1825, 44.5170, 1),
            ("Давид Акопян", "david@dropp.am", "+374 91 123 403", "scooter", "#f3b783", 40.1932, 44.5030, 1),
            ("Нарек Петросян", "narek@dropp.am", "+374 91 123 404", "bike", "#79b8ee", 40.1774, 44.5075, 1),
            ("Ани Ованнисян", "ani@dropp.am", "+374 91 123 405", "scooter", "#eea3cb", 40.1905, 44.5206, 1),
            ("Гор Саркисян", "gor@dropp.am", "+374 91 123 406", "car", "#d5cb83", 40.1740, 44.5170, 0),
        ]
        for name, email, phone, vehicle, color, lat, lng, online in couriers:
            self.execute("INSERT INTO users(role,name,email,phone,password,vehicle,color,lat,lng,online,located_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", "courier", name, email, phone, pw, vehicle, color, lat, lng, online, now(), now())
        for name, email, phone in [("Елена Арутюнян", "elena@dropp.am", "+374 99 205 031"), ("Кафе «Пончикано»", "cafe@dropp.am", "+374 99 205 032"), ("Александр Мирзоян", "alex@dropp.am", "+374 99 205 033"), ("Студия Bloom", "bloom@dropp.am", "+374 99 205 034")]:
            self.execute("INSERT INTO users(role,name,email,phone,password,created_at) VALUES (?,?,?,?,?,?)", "client", name, email, phone, pw, now())
        clients = self.query("SELECT id FROM users WHERE role='client' ORDER BY id")
        couriers = self.query("SELECT id FROM users WHERE role='courier' ORDER BY id")
        places = [
            ("ул. Абовяна, 12", 40.1828, 44.5169, "пр. Маштоца, 24", 40.1856, 44.5080, "Кофе и десерты", .8),
            ("ул. Туманяна, 19", 40.1858, 44.5189, "ул. Сарьяна, 8", 40.1888, 44.5074, "Букет пионов", 1.2),
            ("Северный проспект, 6", 40.1837, 44.5149, "пр. Баграмяна, 15", 40.1920, 44.5047, "Документы А4", .3),
            ("ул. Амиряна, 4", 40.1795, 44.5087, "ул. Исаакяна, 28", 40.1921, 44.5160, "Посылка · одежда", 2.5),
            ("ул. Московян, 22", 40.1892, 44.5107, "ул. Вардананц, 14", 40.1777, 44.5185, "Обед на двоих", 1.8),
            ("ул. Парпеци, 16", 40.1878, 44.5095, "Каскад, ул. Таманяна", 40.1918, 44.5156, "Керамическая ваза", 3.2),
        ]
        recent = ["picked_up", "assigned", "new", "new", "picked_up", "assigned", "delivered", "new", "cancelled", "assigned", "new", "picked_up", "new"]
        for i in range(42):
            place, client = places[i % 6], clients[i % 4]["id"]
            status = "delivered" if i < 29 else recent[i - 29]
            courier = None if status in ("new", "cancelled") else couriers[i % 5]["id"]
            ago = (14 - i // 3) * 86400 if i < 29 else (42 - i) * 420
            stamp = dt.datetime.fromtimestamp(time.time() - ago, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            fee = 1100 + i % 5 * 250
            payout = math.floor(fee * .72 / 50 + .5) * 50
            order = self.execute("""INSERT INTO orders(client_id,courier_id,status,pickup,dropoff,pickup_lat,pickup_lng,dropoff_lat,dropoff_lng,recipient,phone,description,weight,fee,payout,priority,note,track_token,created_at,updated_at,delivered_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", client, courier, status, place[0], place[3], place[1], place[2], place[4], place[5], "Получатель заказа", "+374 99 100 200", place[6], place[7], fee, payout, "express" if i % 4 == 0 else "normal", "Позвонить за 5 минут до приезда" if i % 3 == 0 else "", secrets.token_urlsafe(24), stamp, stamp, stamp if status == "delivered" else None).lastrowid
            self.history(order, "new", client, "Демонстрационный заказ", stamp)
            if courier:
                self.history(order, "assigned", courier, "Курьер принял заказ", stamp)
            if status in ("picked_up", "delivered"):
                self.history(order, "picked_up", courier, "Демонстрационный заказ", stamp)
            if status == "delivered":
                self.history(order, "delivered", courier, "Демонстрационный заказ", stamp)
            if status == "cancelled":
                self.history(order, "cancelled", client, "Демонстрационный заказ", stamp)
        client = clients[0]["id"]
        admin = self.one("SELECT id FROM users WHERE role='admin'")["id"]
        self.execute("INSERT INTO messages(thread,user_id,body,created_at) VALUES (?,?,?,?)", f"support:{client}", client, "Здравствуйте! Можно доставить букет к определённому времени?", now())
        self.execute("INSERT INTO messages(thread,user_id,body,created_at) VALUES (?,?,?,?)", f"support:{client}", admin, "Здравствуйте, Елена! Конечно. Укажите время в комментарии к заказу — диспетчер его увидит.", now())

    def broadcast(self, kind="refresh", order_id=None):
        payload = {"type": kind}
        if order_id is not None:
            payload["orderId"] = order_id
        with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, json.dumps(payload)))
            self.condition.notify_all()

    def get_session(self, headers):
        found = re.search(r"(?:^|;\s*)dropp_session=([A-Za-z0-9_-]+)", headers.get("Cookie", ""))
        if not found:
            return None
        return self.one("SELECT s.csrf,s.hash,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?", token_hash(found[1]), millis())

    def session(self, handler):
        user = self.get_session(handler.headers)
        if not user:
            fail(401, "Войдите в аккаунт.")
        return user

    def new_session(self, handler, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.execute("INSERT INTO sessions(hash,user_id,csrf,expires) VALUES (?,?,?,?)", token_hash(token), user_id, csrf, millis() + 7 * 86400000)
        handler.extra_headers["Set-Cookie"] = f"dropp_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800" + ("; Secure" if self.cfg.public_url.startswith("https:") else "")
        return csrf

    @staticmethod
    def admin(user):
        if user["role"] != "admin":
            fail(403, "Доступ только у администратора.")

    def order_access(self, user, order_id):
        order = self.one("SELECT * FROM orders WHERE id=?", order_id)
        if not order:
            fail(404, "Заказ не найден.")
        if user["role"] != "admin" and user["id"] not in (order["client_id"], order["courier_id"]):
            fail(403, "Нет доступа к этому заказу.")
        return order

    def thread_access(self, user, thread):
        if re.fullmatch(r"order:\d+", thread):
            return self.order_access(user, int(thread[6:]))
        if re.fullmatch(r"support:\d+", thread):
            user_id = int(thread[8:])
            if user["role"] != "admin" and user_id != user["id"]:
                fail(403, "Чужой диалог.")
            if not self.one("SELECT id FROM users WHERE id=?", user_id):
                fail(404, "Пользователь не найден.")
            return True
        fail(400, "Неизвестный диалог.")

    def snapshot(self, user):
        with self.lock:
            uid, role = user["id"], user["role"]
            if role == "admin":
                orders = self.query("SELECT * FROM orders ORDER BY id DESC LIMIT 1000")
                people = [safe_user(p) for p in self.query("SELECT * FROM users ORDER BY created_at DESC")]
            elif role == "courier":
                orders = self.query("SELECT * FROM orders WHERE courier_id=? OR (status='new' AND ?=1) ORDER BY id DESC LIMIT 1000", uid, user["approved"])
                for order in orders:
                    if order["courier_id"] != uid:
                        order.update(recipient="Получатель", phone="", note="", client_id=None)
                        order.pop("track_token", None)
                        order.pop("request_key", None)
                people = [safe_user(user)] + [{k: p[k] for k in ("id", "name", "role", "phone", "color")} for p in self.query("SELECT * FROM users WHERE id IN (SELECT client_id FROM orders WHERE courier_id=?)", uid)]
            else:
                orders = self.query("SELECT * FROM orders WHERE client_id=? ORDER BY id DESC LIMIT 1000", uid)
                people = [safe_user(user)]
                for p in self.query("SELECT * FROM users WHERE id IN (SELECT courier_id FROM orders WHERE client_id=?)", uid):
                    visible = any(o["courier_id"] == p["id"] and o["status"] in ACTIVE for o in orders)
                    p = {k: p[k] for k in ("id", "name", "role", "phone", "color", "lat", "lng", "located_at", "online", "vehicle")}
                    p["online"] = visible and bool(p["online"])
                    if not visible:
                        p.update(lat=None, lng=None, located_at=None)
                    people.append(p)
            sql = "SELECT m.thread,MAX(m.id) last_id,MAX(m.created_at) updated_at,COUNT(*) count FROM messages m "
            threads = self.query(sql + "GROUP BY m.thread ORDER BY last_id DESC") if role == "admin" else self.query(sql + "WHERE m.thread=? OR m.thread IN (SELECT 'order:'||id FROM orders WHERE client_id=? OR courier_id=?) GROUP BY m.thread ORDER BY last_id DESC", f"support:{uid}", uid, uid)
            telegram = {"configured": bool(self.cfg.token), "username": self.cfg.bot_name}
            if role == "admin":
                telegram["pending"] = self.one("SELECT COUNT(*) c FROM outbox WHERE sent_at IS NULL")["c"]
            support = self.one("SELECT value FROM settings WHERE key='support'")
            return {"user": safe_user(user), "orders": orders, "people": people, "threads": threads,
                    "csrf": user["csrf"], "serverTime": now(), "demo": self.cfg.demo,
                    "telegram": telegram, "settings": {"support": support["value"] if support else "+374 91 000 100"}}

    def claim(self, user, order_id, inside=False):
        with (contextlib.nullcontext() if inside else self.transaction()):
            fresh = self.one("SELECT * FROM users WHERE id=?", user["id"])
            if not fresh or fresh["role"] != "courier" or not fresh["approved"]:
                fail(403, "Курьер должен быть подтверждён администратором.")
            if not fresh["online"]:
                fail(400, "Включите режим «На линии».")
            if self.one("SELECT COUNT(*) c FROM orders WHERE courier_id=? AND status IN ('assigned','picked_up')", user["id"])["c"] >= 3:
                fail(409, "У вас уже три активных заказа.")
            result = self.execute("UPDATE orders SET courier_id=?,status='assigned',updated_at=? WHERE id=? AND status='new' AND courier_id IS NULL", user["id"], now(), order_id)
            if not result.rowcount:
                fail(409, "Этот заказ уже забрал другой курьер или он недоступен.")
            self.history(order_id, "assigned", user["id"], "Заказ принят курьером")
            order = self.one("SELECT * FROM orders WHERE id=?", order_id)
            self.notify_order(order, "Курьер принял заказ.")
            return order

    def rate_limit(self, handler, key, maximum=200, seconds=60):
        ip = handler.client_address[0]
        if self.cfg.trust_proxy:
            ip = handler.headers.get("X-Forwarded-For", ip).split(",")[0].strip()
        identity = (ip, key)
        stamp = time.monotonic()
        with self.limit_lock:
            if len(self.limits) > 10000:
                self.limits = {k: v for k, v in self.limits.items() if v[1] > stamp}
            count, expiry = self.limits.get(identity, (0, stamp + seconds))
            if expiry <= stamp:
                count, expiry = 0, stamp + seconds
            self.limits[identity] = count + 1, expiry
            if count >= maximum:
                fail(429, "Слишком много запросов. Попробуйте чуть позже.")

    def enqueue(self, chat_id, payload):
        if self.cfg.token and chat_id:
            self.execute("INSERT INTO outbox(chat_id,payload) VALUES (?,?)", str(chat_id), json.dumps(payload, ensure_ascii=False))

    def announce_order(self, order):
        for courier in self.query("SELECT telegram_id FROM users WHERE role='courier' AND approved=1 AND online=1 AND telegram_id IS NOT NULL"):
            self.enqueue(courier["telegram_id"], {
                "text": f"🟢 Новый заказ #DR-{order['id']:04d}\n{order['description']}\n{order['pickup']} → {order['dropoff']}\nВес: {order['weight']} кг · Ваш доход: {order['payout']} ֏",
                "reply_markup": {"inline_keyboard": [[{"text": "Забрать заказ →", "callback_data": f"claim:{order['id']}"}],
                                                     [{"text": "Открыть кабинет", "web_app": {"url": self.cfg.public_url + "/?app=courier"}}]]}})

    def notify_order(self, order, message):
        for user in self.query("SELECT telegram_id FROM users WHERE id IN (?,?) AND telegram_id IS NOT NULL", order["client_id"], order["courier_id"] or 0):
            self.enqueue(user["telegram_id"], {"text": f"DROPP · #DR-{order['id']:04d}\n{message}"})

    def telegram_api(self, method, payload):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.cfg.token}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=35 if method == "getUpdates" else 10) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read(65536))
            except (ValueError, OSError):
                raise APIError(502, "Telegram API вернул ошибку.") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise APIError(502, "Нет соединения с Telegram API.") from None
        if not isinstance(result, dict) or not result.get("ok"):
            error = APIError(502, str(result.get("description", "Ошибка Telegram API.")) if isinstance(result, dict) else "Ошибка Telegram API.")
            error.retry_after = result.get("parameters", {}).get("retry_after", 0) if isinstance(result, dict) else 0
            raise error
        return result.get("result")

    def flush_outbox(self):
        if not self.cfg.token or not self.outbox_lock.acquire(blocking=False):
            return
        try:
            for item in self.query("SELECT * FROM outbox WHERE sent_at IS NULL AND next_attempt<? AND attempts<8 ORDER BY id LIMIT 20", millis()):
                if self.stop_event.is_set():
                    break
                try:
                    self.telegram_api("sendMessage", {"chat_id": item["chat_id"], **json.loads(item["payload"])})
                    self.execute("UPDATE outbox SET sent_at=?,error='' WHERE id=?", now(), item["id"])
                except APIError as error:
                    wait = int(getattr(error, "retry_after", 0)) * 1000 or min(3600000, 5000 * 2 ** item["attempts"])
                    self.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=?,error=? WHERE id=?", millis() + wait, str(error)[:300], item["id"])
        finally:
            self.outbox_lock.release()

    def telegram_update(self, update):
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return
        callback, answer = None, "Действие недоступно."
        with self.transaction():
            if self.one("SELECT id FROM telegram_updates WHERE id=?", update_id):
                return
            self.execute("INSERT INTO telegram_updates(id,created_at) VALUES (?,?)", update_id, now())
            message = update.get("message") or update.get("edited_message") or {}
            if message.get("chat", {}).get("type") == "private":
                chat_id = message["chat"]["id"]
                from_id = str(message.get("from", {}).get("id", ""))
                match = re.match(r"^/start(?:@\w+)?(?:\s+([A-Za-z0-9_-]+))?", message.get("text", ""))
                menu = {"inline_keyboard": [[{"text": "Открыть DROPP", "web_app": {"url": self.cfg.public_url + "/?app=telegram"}}]]}
                if match and match[1]:
                    link = self.one("SELECT * FROM telegram_links WHERE hash=? AND expires>?", token_hash(match[1]), millis())
                    if link:
                        used = self.one("SELECT id FROM users WHERE telegram_id=?", from_id)
                        if used and used["id"] != link["user_id"]:
                            self.enqueue(chat_id, {"text": "Этот Telegram уже привязан к другому аккаунту."})
                        else:
                            self.execute("UPDATE users SET telegram_id=? WHERE id=?", from_id, link["user_id"])
                            self.execute("DELETE FROM telegram_links WHERE hash=?", link["hash"])
                            self.enqueue(chat_id, {"text": "Готово! Аккаунт DROPP подключён. Уведомления о заказах будут приходить сюда.", "reply_markup": menu})
                    else:
                        self.enqueue(chat_id, {"text": "Ссылка истекла. Создайте новую в профиле DROPP."})
                elif match:
                    self.enqueue(chat_id, {"text": "Добро пожаловать в DROPP. Войдите на сайт и подключите Telegram в профиле.", "reply_markup": menu})
                elif message.get("location"):
                    user = self.one("SELECT * FROM users WHERE telegram_id=?", from_id)
                    if user and user["role"] == "courier" and user["approved"] and user["online"]:
                        lat, lng = message["location"].get("latitude"), message["location"].get("longitude")
                        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and 40.08 <= lat <= 40.30 and 44.38 <= lng <= 44.65:
                            self.execute("UPDATE users SET lat=?,lng=?,located_at=? WHERE id=?", lat, lng, now(), user["id"])
            callback = update.get("callback_query")
            if callback:
                try:
                    user = self.one("SELECT * FROM users WHERE telegram_id=?", str(callback.get("from", {}).get("id", "")))
                    match = re.fullmatch(r"claim:(\d+)", callback.get("data", ""))
                    if not user:
                        fail(403, "Сначала подключите Telegram в профиле сайта.")
                    if match:
                        self.claim(user, int(match[1]), inside=True)
                        answer = "Заказ ваш! Откройте кабинет."
                except APIError as error:
                    answer = str(error)
        if callback:
            try:
                self.telegram_api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": answer[:180], "show_alert": True})
            except APIError:
                pass
        self.broadcast()

    def telegram_user(self, init_data):
        if not self.cfg.token:
            fail(503, "Telegram не подключён.")
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, max_num_fields=50)
        params = dict(pairs)
        supplied = params.get("hash", "")
        data = "\n".join(key + "=" + value for key, value in sorted(pairs) if key != "hash")
        key = hmac.new(b"WebAppData", self.cfg.token.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not re.fullmatch(r"[a-f0-9]{64}", supplied) or not hmac.compare_digest(supplied, expected):
            fail(403, "Подпись Telegram недействительна.")
        try:
            age = time.time() - int(params["auth_date"])
            tg = json.loads(params["user"])
        except (KeyError, ValueError, TypeError):
            fail(400, "Нет пользователя или времени авторизации Telegram.")
        if not -60 <= age <= 3600:
            fail(403, "Сессия Telegram истекла. Откройте приложение заново.")
        if not isinstance(tg, dict):
            fail(400, "Нет пользователя Telegram.")
        user = self.one("SELECT * FROM users WHERE telegram_id=?", str(tg.get("id", "")))
        if not user:
            fail(403, "Сначала привяжите Telegram в профиле на сайте.")
        return user

    def setup_telegram(self):
        if not self.cfg.token or not self.cfg.bot_name:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME и HTTPS PUBLIC_URL в CONFIG или ENV.")
        bot = self.telegram_api("getMe", {})
        if bot["username"].lower() != self.cfg.bot_name.lower():
            raise ValueError("Имя бота не соответствует токену.")
        self.telegram_api("setMyCommands", {"commands": [{"command": "start", "description": "Открыть DROPP / подключить аккаунт"}]})
        self.telegram_api("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Открыть DROPP", "web_app": {"url": self.cfg.public_url + "/?app=telegram"}}})
        if self.cfg.telegram_mode == "webhook":
            self.telegram_api("setWebhook", {"url": self.cfg.public_url + "/api/telegram/webhook", "secret_token": self.cfg.webhook_secret, "allowed_updates": ["message", "callback_query", "edited_message"]})
        else:
            self.telegram_api("deleteWebhook", {"drop_pending_updates": False})
        print("Telegram настроен. Запустите python bot.py и подключите аккаунт в профиле.")

    def cleanup(self):
        with self.transaction():
            self.execute("DELETE FROM sessions WHERE expires<?", millis())
            self.execute("DELETE FROM telegram_links WHERE expires<?", millis())
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            self.execute("DELETE FROM telegram_updates WHERE created_at<?", cutoff)
        with self.limit_lock:
            stamp = time.monotonic()
            self.limits = {k: v for k, v in self.limits.items() if v[1] > stamp}

    def start_workers(self):
        def maintenance():
            while not self.stop_event.wait(60):
                try:
                    self.cleanup()
                except sqlite3.Error:
                    logging.exception("Database maintenance failed")

        def outbox():
            while not self.stop_event.wait(2):
                self.flush_outbox()

        def polling():
            while not self.stop_event.is_set():
                try:
                    setting = self.one("SELECT value FROM settings WHERE key='telegram_offset'")
                    updates = self.telegram_api("getUpdates", {"offset": int(setting["value"]) if setting else 0, "timeout": 25, "allowed_updates": ["message", "callback_query", "edited_message"]})
                    for update in updates:
                        if self.stop_event.is_set():
                            return
                        self.telegram_update(update)
                        self.execute("INSERT INTO settings(key,value) VALUES ('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", str(update["update_id"] + 1))
                except (APIError, sqlite3.Error):
                    logging.warning("Telegram polling unavailable: check token, network and webhook mode.")
                    if self.stop_event.wait(4):
                        return

        jobs = [maintenance]
        if self.cfg.token:
            jobs.append(outbox)
            if self.cfg.telegram_mode == "polling":
                jobs.append(polling)
        for job in jobs:
            worker = threading.Thread(target=job, daemon=True, name="dropp-" + job.__name__)
            worker.start()
            self.workers.append(worker)

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        for worker in self.workers:
            worker.join(timeout=1)
        if not any(worker.is_alive() for worker in self.workers):
            with self.lock:
                self.db.close()

    def api(self, handler, path, query):
        method = handler.command
        respond = handler.send_json
        if path == "/api/health" and method == "GET":
            return respond({"ok": True, "service": "dropp", "version": "2.0.0-python"})
        if path == "/api/config" and method == "GET":
            return respond({"demo": self.cfg.demo, "telegram": bool(self.cfg.token), "botUsername": self.cfg.bot_name})
        self.rate_limit(handler, "api")
        if path == "/api/telegram/webhook" and method == "POST":
            supplied = handler.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not self.cfg.token or self.cfg.telegram_mode != "webhook" or not self.cfg.webhook_secret or not hmac.compare_digest(supplied.encode(), self.cfg.webhook_secret.encode()):
                fail(403, "Forbidden")
            self.telegram_update(handler.body())
            return respond({"ok": True})
        if method not in ("GET", "HEAD"):
            origin = handler.headers.get("Origin")
            host_origin = ("https" if self.cfg.public_url.startswith("https:") else "http") + "://" + handler.headers.get("Host", "")
            if origin and origin != self.cfg.public_url and (not self.cfg.demo or origin != host_origin):
                fail(403, "Источник запроса запрещён.")
            body = handler.body()
        else:
            body = {}
        if path.startswith("/api/auth/"):
            if method != "POST":
                fail(405, "Method not allowed")
            self.rate_limit(handler, "auth", 120 if self.cfg.demo else 20, 900)
            if path == "/api/auth/logout":
                user = self.session(handler)
                if handler.headers.get("X-CSRF-Token") != user["csrf"]:
                    fail(403, "Обновите страницу.")
                self.execute("DELETE FROM sessions WHERE hash=?", user["hash"])
                handler.extra_headers["Set-Cookie"] = "dropp_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" + ("; Secure" if self.cfg.public_url.startswith("https:") else "")
                self.broadcast()
                return respond({"ok": True})
            if path == "/api/auth/demo":
                if not self.cfg.demo:
                    fail(404, "Демо отключено.")
                role = body.get("role") if body.get("role") in ("admin", "courier", "client") else "admin"
                user = self.one("SELECT * FROM users WHERE role=? ORDER BY id LIMIT 1", role)
            elif path == "/api/auth/login":
                email = text_field(body, "email", 180).lower()
                password = text_field(body, "password", 128, strip=False)
                user = self.one("SELECT * FROM users WHERE email=?", email)
                valid = check_password(password, user["password"] if user else self.invalid_password)
                if not user or not valid:
                    fail(401, "Неверный email или пароль.")
            elif path == "/api/auth/register":
                name, email = text_field(body, "name", 90), text_field(body, "email", 180).lower()
                phone, password = text_field(body, "phone", 24), text_field(body, "password", 128, strip=False)
                validate_email(email)
                validate_phone(phone)
                if len(password) < 10:
                    fail(400, "Пароль: минимум 10 символов.")
                role = "courier" if body.get("role") == "courier" else "client"
                hashed = password_hash(password)
                with self.transaction():
                    if self.one("SELECT id FROM users WHERE email=?", email):
                        fail(409, "Этот email уже зарегистрирован.")
                    user_id = self.execute("INSERT INTO users(role,name,email,phone,password,approved,created_at) VALUES (?,?,?,?,?,?,?)", role, name, email, phone, hashed, 0 if role == "courier" else 1, now()).lastrowid
                    user = self.one("SELECT * FROM users WHERE id=?", user_id)
                self.broadcast()
            elif path == "/api/auth/telegram":
                user = self.telegram_user(text_field(body, "initData", 10000))
            else:
                fail(404, "Не найдено.")
            csrf = self.new_session(handler, user["id"])
            return respond({"user": safe_user(user), "csrf": csrf})

        if path.startswith("/api/track/") and method == "GET":
            self.rate_limit(handler, "track", 60)
            token = path[11:]
            if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
                fail(404, "Ссылка не найдена.")
            order = self.one("SELECT * FROM orders WHERE track_token=?", token)
            if not order:
                fail(404, "Заказ не найден.")
            courier = self.one("SELECT * FROM users WHERE id=?", order["courier_id"]) if order["courier_id"] else None
            if courier:
                courier = {"name": courier["name"].split()[0], "vehicle": courier["vehicle"], "color": courier["color"],
                           "lat": courier["lat"] if order["status"] in ACTIVE else None,
                           "lng": courier["lng"] if order["status"] in ACTIVE else None,
                           "located_at": courier["located_at"] if order["status"] in ACTIVE else None}
            return respond({"order": {k: order[k] for k in ("id", "status", "pickup", "dropoff", "pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng", "description", "created_at")},
                            "courier": courier, "history": self.query("SELECT status,created_at FROM history WHERE order_id=? ORDER BY id", order["id"]), "demo": self.cfg.demo})

        user = self.session(handler)
        if method not in ("GET", "HEAD") and handler.headers.get("X-CSRF-Token") != user["csrf"]:
            fail(403, "Сессия обновилась. Перезагрузите страницу.")
        uid, role = user["id"], user["role"]
        if path == "/api/me" and method == "GET":
            return respond(self.snapshot(user))
        if path == "/api/events" and method == "GET":
            return handler.send_events(user)
        if path == "/api/profile" and method == "PATCH":
            name, phone = text_field(body, "name", 90), text_field(body, "phone", 24)
            validate_phone(phone)
            vehicle = body.get("vehicle") if body.get("vehicle") in VEHICLES else user["vehicle"]
            self.execute("UPDATE users SET name=?,phone=?,vehicle=? WHERE id=?", name, phone, vehicle, uid)
            self.broadcast()
            return respond({"ok": True})
        if path == "/api/password" and method == "POST":
            self.rate_limit(handler, "password", 6, 900)
            if not check_password(text_field(body, "current", 128, strip=False), user["password"]):
                fail(400, "Текущий пароль не подходит.")
            password = text_field(body, "password", 128, strip=False)
            if len(password) < 10:
                fail(400, "Новый пароль: минимум 10 символов.")
            hashed = password_hash(password)
            with self.transaction():
                self.execute("UPDATE users SET password=? WHERE id=?", hashed, uid)
                self.execute("DELETE FROM sessions WHERE user_id=? AND hash<>?", uid, user["hash"])
            self.broadcast()
            return respond({"ok": True})
        if path == "/api/telegram/link" and method == "POST":
            if not self.cfg.token or not self.cfg.bot_name:
                fail(503, "Администратор ещё не настроил Telegram-бота.")
            code = secrets.token_urlsafe(24)
            with self.transaction():
                self.execute("DELETE FROM telegram_links WHERE user_id=?", uid)
                self.execute("INSERT INTO telegram_links(hash,user_id,expires) VALUES (?,?,?)", token_hash(code), uid, millis() + 600000)
            return respond({"url": f"https://t.me/{self.cfg.bot_name}?start={code}"})
        if path == "/api/telegram/unlink" and method == "POST":
            with self.transaction():
                self.execute("UPDATE users SET telegram_id=NULL WHERE id=?", uid)
                self.execute("DELETE FROM telegram_links WHERE user_id=?", uid)
            self.broadcast()
            return respond({"ok": True})
        if path == "/api/courier/online" and method == "POST":
            if role != "courier" or not user["approved"]:
                fail(403, "Дождитесь подтверждения администратора.")
            if not isinstance(body.get("online"), bool):
                fail(400, "online должен быть true или false.")
            self.execute("UPDATE users SET online=? WHERE id=?", int(body["online"]), uid)
            self.broadcast()
            return respond({"ok": True})
        if path == "/api/courier/location" and method == "POST":
            if role != "courier" or not user["approved"] or not user["online"]:
                fail(403, "Включите режим «На линии».")
            self.rate_limit(handler, "gps", 20)
            lat, lng = number_field(body, "lat", 40.08, 40.30), number_field(body, "lng", 44.38, 44.65)
            self.execute("UPDATE users SET lat=?,lng=?,located_at=? WHERE id=?", lat, lng, now(), uid)
            self.broadcast("location")
            return respond({"ok": True})
        if path == "/api/users" and method == "POST":
            self.admin(user)
            new_role = "courier" if body.get("role") == "courier" else "client"
            name, email = text_field(body, "name", 90), text_field(body, "email", 180).lower()
            phone, password = text_field(body, "phone", 24), text_field(body, "password", 128, strip=False)
            validate_email(email)
            validate_phone(phone)
            if len(password) < 10:
                fail(400, "Пароль: минимум 10 символов.")
            hashed = password_hash(password)
            vehicle = body.get("vehicle") if body.get("vehicle") in VEHICLES else "bike"
            with self.transaction():
                if self.one("SELECT id FROM users WHERE email=?", email):
                    fail(409, "Этот email уже используется.")
                user_id = self.execute("INSERT INTO users(role,name,email,phone,password,vehicle,created_at) VALUES (?,?,?,?,?,?,?)", new_role, name, email, phone, hashed, vehicle, now()).lastrowid
            self.broadcast()
            return respond({"id": user_id}, 201)
        approve = re.fullmatch(r"/api/users/(\d+)/approve", path)
        if approve and method == "POST":
            self.admin(user)
            result = self.execute("UPDATE users SET approved=1 WHERE id=? AND role='courier'", int(approve[1]))
            if not result.rowcount:
                fail(404, "Курьер не найден.")
            self.broadcast()
            return respond({"ok": True})

        if path == "/api/quote" and method == "POST":
            data = {**coordinates(body, "pickup"), **coordinates(body, "dropoff"),
                    "weight": number_field(body, "weight", .01, 50), "priority": body.get("priority", "normal")}
            return respond(pricing(data))
        if path == "/api/orders" and method == "POST":
            if role == "courier":
                fail(403, "Заказы создают клиенты и администраторы.")
            pickup, dropoff = text_field(body, "pickup", 200), text_field(body, "dropoff", 200)
            recipient, phone = text_field(body, "recipient", 90), text_field(body, "phone", 24)
            description, note = text_field(body, "description", 400), text_field(body, "note", 1000, False)
            validate_phone(phone)
            data = {**coordinates(body, "pickup"), **coordinates(body, "dropoff"), "weight": number_field(body, "weight", .01, 50),
                    "priority": "express" if body.get("priority") == "express" else "normal"}
            client = number_field(body, "client_id", 1, 10**9, True) if role == "admin" else uid
            if not self.one("SELECT id FROM users WHERE id=? AND role='client'", client):
                fail(400, "Выберите клиента.")
            quoted = pricing(data)
            fee = math.floor(number_field(body, "fee", 100, 1000000) + .5) if role == "admin" else quoted["fee"]
            payout = math.floor(number_field(body, "payout", 50, fee) + .5) if role == "admin" else quoted["payout"]
            key = text_field(body, "request_key", 80)
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
                fail(400, "Некорректный ключ запроса.")
            duplicate = False
            with self.transaction():
                existing = self.one("SELECT id,client_id FROM orders WHERE request_key=?", key)
                if existing:
                    if existing["client_id"] != client:
                        fail(409, "Ключ запроса занят.")
                    order_id, duplicate = existing["id"], True
                else:
                    order_id = self.execute("""INSERT INTO orders(client_id,pickup,dropoff,pickup_lat,pickup_lng,dropoff_lat,dropoff_lng,recipient,phone,description,weight,fee,payout,priority,note,track_token,request_key,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", client, pickup, dropoff, data["pickup_lat"], data["pickup_lng"], data["dropoff_lat"], data["dropoff_lng"], recipient, phone, description, data["weight"], fee, payout, data["priority"], note, secrets.token_urlsafe(24), key, now(), now()).lastrowid
                    self.history(order_id, "new", uid, "Заказ создан")
                    self.announce_order(self.one("SELECT * FROM orders WHERE id=?", order_id))
            if not duplicate:
                self.broadcast("order_created", order_id)
            return respond({"id": order_id, **({"duplicate": True} if duplicate else {})}, 200 if duplicate else 201)

        match = re.fullmatch(r"/api/orders/(\d+)(?:/(claim|assign|cancel|proof))?", path)
        if match:
            order_id, action = int(match[1]), match[2]
            if action == "claim" and method == "POST":
                self.claim(user, order_id)
                self.broadcast("claimed", order_id)
                return respond({"ok": True})
            order = self.order_access(user, order_id)
            if not action and method == "GET":
                return respond({"order": order, "history": self.query("SELECT h.*,u.name FROM history h LEFT JOIN users u ON u.id=h.actor_id WHERE order_id=? ORDER BY h.id", order_id),
                                "photos": self.query("SELECT id,stage,comment,created_at FROM photos WHERE order_id=? ORDER BY created_at,id", order_id)})
            if action == "assign" and method == "POST":
                self.admin(user)
                courier = number_field(body, "courier_id", 1, 10**9, True)
                with self.transaction():
                    if not self.one("SELECT id FROM users WHERE id=? AND role='courier' AND approved=1", courier):
                        fail(400, "Курьер недоступен.")
                    if self.one("SELECT COUNT(*) c FROM orders WHERE courier_id=? AND status IN ('assigned','picked_up')", courier)["c"] >= 3:
                        fail(409, "У курьера уже три активных заказа.")
                    if not self.execute("UPDATE orders SET courier_id=?,status='assigned',updated_at=? WHERE id=? AND status='new'", courier, now(), order_id).rowcount:
                        fail(409, "Заказ уже назначен.")
                    self.history(order_id, "assigned", uid, "Назначен диспетчером")
                    self.notify_order({**order, "courier_id": courier}, "К заказу назначен курьер.")
                self.broadcast()
                return respond({"ok": True})
            if action == "cancel" and method == "POST":
                reason = text_field(body, "reason", 400)
                if role == "courier" or (role == "client" and order["status"] != "new"):
                    fail(403, "Для отмены обратитесь в поддержку.")
                with self.transaction():
                    allowed = "status IN ('new','assigned','picked_up')" if role == "admin" else "status='new'"
                    if not self.execute(f"UPDATE orders SET status='cancelled',updated_at=? WHERE id=? AND {allowed}", now(), order_id).rowcount:
                        fail(409, "Заказ уже принят или завершён. Обратитесь в поддержку.")
                    self.history(order_id, "cancelled", uid, reason)
                    self.notify_order(order, "Заказ отменён: " + reason)
                self.broadcast()
                return respond({"ok": True})
            if action == "proof" and method == "POST":
                if role != "courier" or order["courier_id"] != uid:
                    fail(403, "Фото загружает назначенный курьер.")
                stage = text_field(body, "stage", 20)
                previous = {"picked_up": "assigned", "delivered": "picked_up"}.get(stage)
                if not previous:
                    fail(400, "Неверный этап.")
                comment = text_field(body, "comment", 800)
                photo = text_field(body, "photo", MAX_BODY)
                encoded = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", photo)
                if not encoded:
                    fail(400, "Нужна фотография JPEG, PNG или WebP.")
                try:
                    image = base64.b64decode(encoded[2], validate=True)
                except binascii.Error:
                    fail(400, "Некорректное содержимое фотографии.")
                if not 16 <= len(image) <= MAX_PHOTO:
                    fail(413, "Фото должно быть меньше 4 МБ.")
                mime = encoded[1]
                valid = ((mime == "image/jpeg" and image.startswith(b"\xff\xd8\xff")) or
                         (mime == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n")) or
                         (mime == "image/webp" and image[:4] == b"RIFF" and image[8:12] == b"WEBP"))
                if not valid:
                    fail(400, "Содержимое не соответствует формату фотографии.")
                with self.transaction():
                    result = self.execute("UPDATE orders SET status=?,updated_at=?,delivered_at=? WHERE id=? AND courier_id=? AND status=?", stage, now(), now() if stage == "delivered" else None, order_id, uid, previous)
                    if not result.rowcount:
                        fail(409, "Этап заказа уже изменился. Обновите страницу.")
                    self.execute("INSERT INTO photos(id,order_id,user_id,stage,mime,bytes,comment,created_at) VALUES (?,?,?,?,?,?,?,?)", secrets.token_urlsafe(24), order_id, uid, stage, mime, image, comment, now())
                    self.history(order_id, stage, uid, comment)
                    self.notify_order(order, STATUS[stage] + ". " + comment)
                self.broadcast()
                return respond({"ok": True})
            fail(405, "Действие недоступно.")

        if path.startswith("/api/photos/") and method == "GET":
            photo = self.one("SELECT * FROM photos WHERE id=?", path[12:])
            if not photo:
                fail(404, "Фото не найдено.")
            self.order_access(user, photo["order_id"])
            return handler.send_bytes(photo["bytes"], photo["mime"], headers={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"})
        if path == "/api/messages" and method == "GET":
            thread = query.get("thread", [""])[0]
            self.thread_access(user, thread)
            try:
                before = int(query.get("before", ["0"])[0])
            except ValueError:
                fail(400, "Некорректный номер сообщения.")
            rows = self.query("SELECT m.*,u.name,u.role,u.color FROM messages m JOIN users u ON u.id=m.user_id WHERE m.thread=? AND (?=0 OR m.id<?) ORDER BY m.id DESC LIMIT 100", thread, before, before)
            return respond({"messages": list(reversed(rows)), "hasMore": len(rows) == 100})
        if path == "/api/messages" and method == "POST":
            self.rate_limit(handler, "chat", 30)
            thread, message = text_field(body, "thread", 80), text_field(body, "body", 2000)
            self.thread_access(user, thread)
            message_id = self.execute("INSERT INTO messages(thread,user_id,body,created_at) VALUES (?,?,?,?)", thread, uid, message, now()).lastrowid
            self.broadcast("message")
            return respond({"id": message_id}, 201)
        if path == "/api/settings" and method == "PATCH":
            self.admin(user)
            support = text_field(body, "support", 24)
            validate_phone(support)
            self.execute("INSERT INTO settings(key,value) VALUES ('support',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", support)
            self.broadcast()
            return respond({"ok": True})
        if path == "/api/export" and method == "GET":
            if role == "client":
                fail(403, "Недоступно.")
            rows = self.query("SELECT * FROM orders ORDER BY id DESC") if role == "admin" else self.query("SELECT * FROM orders WHERE courier_id=? ORDER BY id DESC", uid)
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_ALL)
            writer.writerow(["ID", "Дата", "Статус", "Откуда", "Куда", "Вес, кг", "Стоимость, AMD", "Курьеру, AMD"])
            for order in rows:
                values = [order["id"], order["created_at"], STATUS[order["status"]], order["pickup"], order["dropoff"], order["weight"], order["fee"], order["payout"]]
                writer.writerow(["' " + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@")) or str(v).startswith(("\t", "\r")) else v for v in values])
            return handler.send_bytes(("\ufeff" + buffer.getvalue()).encode(), "text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="dropp-orders.csv"'})
        fail(404, "Маршрут не найден.")


# EMBEDDED_FRONTEND
CLIENT_HTML = r"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#111312"><meta name="description" content="DROPP — доставка в ритме Еревана. Диспетчерская, кабинет курьера и отслеживание заказов."><title>DROPP — город в движении</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='18' fill='%23c5f56b'/%3E%3Cpath d='M18 17h14l-7 15h14l-7 15H18l7-15H11z' fill='%23111312'/%3E%3C/svg%3E"><style nonce="%%NONCE%%">
:root{color-scheme:dark;--bg:#101211;--panel:#181b18;--panel2:#1d201d;--line:#2b302a;--muted:#858d80;--text:#f0f2eb;--lime:#c5f56b;--purple:#b6a3f5;--orange:#edb88b;--red:#ef9390;--radius:18px;--side:224px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:13px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}button,input,textarea,select{font:inherit}button,a,input,textarea,select{-webkit-tap-highlight-color:transparent}button{cursor:pointer}button:disabled{cursor:wait;opacity:.5}a{color:inherit;text-decoration:none}button{color:inherit;background:none;border:0}svg{display:inline-block;vertical-align:middle;flex-shrink:0}input,textarea,select{width:100%;background:#141714;border:1px solid #353c32;color:var(--text);border-radius:10px;padding:12px 13px;outline:0;transition:.2s}input:focus,textarea:focus,select:focus{border-color:var(--lime);box-shadow:0 0 0 3px #c5f56b10}input::placeholder,textarea::placeholder{color:#60695d}select{appearance:auto}label{display:block;color:#b9c0b4;font-size:12px;margin-bottom:7px;font-weight:500}textarea{resize:vertical;min-height:88px}h1,h2,h3,p{margin:0}h1{font-size:32px;letter-spacing:-1.35px;line-height:1.2;font-weight:600}h2{font-size:16px;letter-spacing:-.35px;font-weight:600}h3{font-size:14px;font-weight:600}p{line-height:1.65}small{font-size:11px}.muted{color:var(--muted)}.lime{color:var(--lime)}.purple{color:var(--purple)}.danger{color:var(--red)}.grow{flex:1;min-width:0}.mono{font-variant-numeric:tabular-nums;font-feature-settings:'tnum'}
.boot{height:100dvh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:26px}.boot p{color:var(--muted)}.boot-line{width:170px;height:2px;background:var(--line);overflow:hidden}.boot-line:after{content:'';display:block;width:55%;height:100%;background:var(--lime);animation:loading 1.3s infinite}.brand{font-size:38px;line-height:1;font-weight:800;letter-spacing:-2.9px;display:flex;align-items:center;gap:9px}.brand span{font-size:12px;letter-spacing:0;align-self:flex-start;margin-top:2px}.brand-mark{width:32px;height:36px;color:var(--lime)}
.sidebar{position:fixed;inset:0 auto 0 0;width:var(--side);border-right:1px solid var(--line);padding:32px 18px 0;display:flex;flex-direction:column;background:#141713;z-index:40}.sidebar .brand{padding:0 13px 29px}.workspace{border:1px solid var(--line);border-radius:11px;display:flex;gap:10px;align-items:center;padding:11px 12px;margin-bottom:31px}.city-flag{height:28px;width:28px;border-radius:50%;background:linear-gradient(#df5750 0 33%,#5371ba 33% 66%,#dcaa60 66%);border:6px solid #22271f}.workspace b{font-size:12px;font-weight:600}.workspace small{display:block;color:var(--muted);margin-top:3px;font-size:10px}.nav-label{font-size:9px;color:#717a69;letter-spacing:1.8px;padding:0 13px;margin-bottom:12px}.nav{display:flex;flex-direction:column;gap:5px}.nav-item{display:flex;align-items:center;gap:12px;padding:12px 13px;border-radius:9px;color:#9da594;text-align:left;transition:.15s;font-size:12px;position:relative;width:100%}.nav-item svg{width:18px;height:18px;opacity:.9}.nav-item:hover{background:#21271e;color:#f0f2eb}.nav-item.active{background:#c5f56b;color:#1c2610;font-weight:600}.nav-item .count{margin-left:auto;background:#323a2b;border-radius:5px;font-size:10px;padding:2px 6px;min-width:20px;text-align:center;color:#bcc5b3}.nav-item.active .count{background:#a5ce5a;color:#273813}.nav-separator{height:1px;background:var(--line);margin:22px 13px}.sidebar-bottom{margin-top:auto;padding-top:20px}.city-card{position:relative;border:1px solid #3c4532;background:linear-gradient(135deg,#252e1d,#171b15);border-radius:12px;padding:17px 13px 16px;overflow:hidden;margin-bottom:17px}.city-card p{font-size:15px;letter-spacing:-.45px;font-weight:500;line-height:1.4;max-width:140px;position:relative;z-index:2}.city-card small{color:#9ba98b;display:block;margin-top:9px;font-size:10px}.city-card svg{position:absolute;right:-6px;bottom:6px;opacity:.45}.user-card{display:flex;align-items:center;gap:10px;padding:18px 4px;border-top:1px solid var(--line)}.user-card b{font-size:11px}.user-card small{display:block;color:var(--muted);font-size:10px;margin-top:4px}.avatar{height:34px;width:34px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;background:#2d3326;color:#c5e4a1;border:1px solid #46523b;flex-shrink:0}.avatar.square{border-radius:11px}.avatar.violet{background:#312b41;border-color:#4c415f;color:#c2b1e7}.avatar.orange{background:#3d3024;border-color:#584534;color:#eac39d}.avatar.blue{background:#23323d;border-color:#354959;color:#a9c9e4}.avatar.pink{background:#3b2934;border-color:#58404d;color:#e8b5d1}.avatar.small{width:27px;height:27px;font-size:9px}.avatar.large{width:64px;height:64px;font-size:20px}.avatar-stack{display:flex;padding-left:5px}.avatar-stack .avatar{margin-left:-5px;border:2px solid var(--panel)}
.main{margin-left:var(--side);min-height:100dvh}.topbar{height:76px;border-bottom:1px solid var(--line);padding:0 34px;display:flex;align-items:center;gap:16px}.breadcrumb{font-size:10px;color:#727c6c;letter-spacing:.7px;display:flex;gap:12px;align-items:center}.breadcrumb b{font-weight:500;color:#c6cdbf}.topbar-right{margin-left:auto;display:flex;align-items:center;gap:13px}.topbar .divider{width:1px;height:25px;background:var(--line);margin:0 5px}.icon-btn{height:34px;min-width:34px;display:inline-flex;align-items:center;justify-content:center;border-radius:9px;color:#a8b09e;transition:.15s;position:relative}.icon-btn:hover{background:#2b3126;color:var(--text)}.icon-btn svg{width:18px;height:18px}.icon-btn.border{border:1px solid var(--line)}.notification-dot{position:absolute;width:5px;height:5px;background:var(--lime);border-radius:50%;top:6px;right:6px}.keycap{font-size:10px;border:1px solid #373e32;padding:2px 5px;border-radius:4px;margin-left:10px;color:#8c9683}.topbar-search{display:flex;align-items:center;color:#85907c;font-size:11px;gap:8px}.demo-badge{font-size:9px;letter-spacing:.7px;border:1px solid #454b35;background:#23291c;color:#c3d69f;border-radius:5px;padding:5px 7px}.role-select{padding:6px 8px;background:transparent;font-size:10px;width:126px;border-color:var(--line);color:#c2cab9}.mobile-menu{display:none}.content{padding:29px 34px 28px;max-width:1800px;margin:0 auto}.page-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:27px}.eyebrow{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--lime);display:flex;align-items:center;gap:7px;margin-bottom:12px}.subtitle{font-size:12px;color:var(--muted);margin-top:9px}.page-actions{display:flex;align-items:center;gap:10px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 15px;border:1px solid #3c4434;border-radius:9px;color:#dbe0d5;background:#20261c;font-size:11px;font-weight:500;min-height:36px;transition:transform .12s,background .15s;white-space:nowrap}.btn svg{width:15px;height:15px}.btn:hover{background:#303927;border-color:#556344}.btn:active{transform:translateY(1px)}.btn.primary{background:var(--lime);color:#1d2b0d;border-color:var(--lime);font-weight:650;box-shadow:0 3px 12px #0002}.btn.primary:hover{background:#d0ff7d;border-color:#d0ff7d}.btn.ghost{background:transparent;border-color:var(--line);color:#aeb7a3}.btn.ghost:hover{background:#262d20;color:var(--text)}.btn.danger{background:#352521;color:#f0a7a0;border-color:#57332e}.btn.large{padding:14px 21px;font-size:13px;border-radius:10px}.btn.full{width:100%}.btn.link{padding:3px 0;border:0;background:none;color:var(--lime);min-height:auto}.btn.small{padding:7px 10px;font-size:10px;min-height:29px}.dot{display:inline-block;width:5px;height:5px;flex-shrink:0;border-radius:50%;background:currentColor}.live{display:inline-flex;align-items:center;gap:6px;font-size:9px;color:var(--lime);letter-spacing:.8px;font-weight:500}.live .dot{box-shadow:0 0 0 3px #c5f56b12}.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);min-width:0}.card-head{padding:20px 21px;display:flex;align-items:center;gap:10px}.card-head h2{font-size:14px}.card-head .right{margin-left:auto;display:flex;align-items:center;gap:10px}.card-head p{font-size:10px;color:var(--muted);margin-top:5px}.pill{display:inline-flex;align-items:center;gap:5px;border-radius:5px;padding:4px 7px;font-size:9px;font-weight:500;white-space:nowrap}.pill.new{color:#b6a3f5;background:#b6a3f512;border:1px solid #b6a3f520}.pill.assigned{color:#a4bcff;background:#a4bcff0d;border:1px solid #a4bcff22}.pill.picked_up{color:#c5f56b;background:#c5f56b0b;border:1px solid #c5f56b22}.pill.delivered{color:#8bbfa3;background:#8bbfa30b;border:1px solid #8bbfa322}.pill.cancelled{color:#a39289;background:#a392890b;border:1px solid #a3928922}.pill.express{color:#e8b78b;background:#e8b78b12;border:1px solid #e8b78b22}.pill.neutral{border:1px solid #3a4232;color:#b5bfa8;background:#242b1e}.period{padding:7px 8px;width:auto;font-size:10px;background:transparent;color:#aeb7a3;border:1px solid var(--line);border-radius:7px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:22px}.stat{padding:18px 19px;position:relative;overflow:hidden;min-height:116px}.stat-top{display:flex;align-items:center;color:#a0a995;font-size:10px;gap:8px}.stat-icon{color:#8a977e;margin-left:auto}.stat-bottom{display:flex;align-items:flex-end;gap:8px;margin-top:19px;position:relative;z-index:1}.stat-value{font-size:29px;line-height:1;letter-spacing:-1px;font-weight:500}.stat-value small{font-size:14px;color:#8b967f;margin-left:5px}.stat-change{font-size:9px;color:#b8d899;display:flex;align-items:center;gap:3px}.stat-foot{font-size:9px;color:#6f7c63;margin-top:10px;display:flex;align-items:center;gap:6px}.spark{margin-left:auto;width:77px;height:28px;opacity:.8}.grid-main{display:grid;grid-template-columns:minmax(0,1fr) 286px;gap:20px;margin-bottom:23px;align-items:stretch}.map-card{overflow:hidden}.map-toolbar{display:flex;align-items:center;gap:5px;margin-left:8px}.map-toolbar .map-tab{font-size:9px;color:#87917c;padding:5px 8px;border-radius:5px}.map-toolbar .map-tab.active{color:#d5ddca;background:#2a3223}.map-wrap{position:relative;height:359px;background:#171c18;overflow:hidden;isolation:isolate}.city-map{width:100%;height:100%;display:block;touch-action:none;cursor:grab}.city-map.dragging{cursor:grabbing}.map-viewport{transform-origin:0 0}.city-map .street-name{fill:#66735f;font-size:10px;letter-spacing:1px;font-weight:450;paint-order:stroke;stroke:#171d18;stroke-width:4px;stroke-linejoin:round}.city-map .area-name{fill:#75806b;font-size:13px;letter-spacing:5px;font-weight:500;opacity:.65}.city-map .landmark{fill:#a0ad92;font-size:9px;letter-spacing:.3px;paint-order:stroke;stroke:#171d18;stroke-width:3px}.city-map .marker{cursor:pointer;outline:none}.city-map .marker:hover .marker-ring,.city-map .marker:focus .marker-ring{stroke-width:5;stroke:#c5f56b}.city-map .marker.selected .marker-ring{stroke:#d7ff9c;stroke-width:3}.city-map .route-line{stroke-dasharray:5 7;animation:route-flow 35s linear infinite}.map-overlay-top{position:absolute;left:18px;top:14px;display:flex;gap:8px;pointer-events:none}.map-chip{display:flex;align-items:center;gap:7px;border:1px solid #3d4737;border-radius:6px;background:#1b241cdd;padding:7px 10px;font-size:9px;color:#c3cfb8;backdrop-filter:blur(8px)}.map-chip svg{width:12px;height:12px}.map-controls{position:absolute;right:16px;bottom:45px;display:flex;flex-direction:column;border-radius:9px;overflow:hidden;border:1px solid #46503c;background:#222b1ee8}.map-controls button{width:32px;height:31px;color:#b8c8a8;display:grid;place-items:center}.map-controls button+button{border-top:1px solid #46503c}.map-controls button:hover{background:#39472b}.map-controls svg{width:15px;height:15px}.map-status{position:absolute;left:17px;bottom:17px;border:1px solid #455138;background:#1b2419ee;border-radius:9px;display:flex;gap:10px;padding:9px 11px;align-items:center;box-shadow:0 4px 25px #0002}.map-status b{font-size:10px}.map-status p{font-size:8px;color:#91a17f;margin-top:3px}.map-attribution{position:absolute;right:12px;bottom:9px;font-size:8px;color:#7c896e;text-shadow:0 1px #111}.map-bottom{padding:12px 20px;display:flex;align-items:center;gap:18px;border-top:1px solid var(--line);font-size:9px;color:#8a967e}.map-bottom .legend{display:flex;align-items:center;gap:6px}.map-bottom .legend .dot{width:5px;height:5px}.map-bottom .last{margin-left:auto;font-size:8px}.map-tooltip{position:absolute;top:56px;left:18px;z-index:5;border:1px solid #536143;background:#20291cee;backdrop-filter:blur(12px);border-radius:11px;box-shadow:0 10px 40px #0004;padding:13px;min-width:185px}.map-tooltip .top{display:flex;gap:9px;align-items:center;margin-bottom:12px}.map-tooltip b{font-size:11px}.map-tooltip small{font-size:9px;color:#94a783}.map-large .map-wrap{height:calc(100dvh - 270px);min-height:430px}.map-large .map-status{bottom:25px}.map-full-grid{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:20px}.map-side{padding:6px 15px}.map-side .courier-list-row{padding:15px 0}.tracking-map .map-wrap{height:420px}
.activity-card{display:flex;flex-direction:column}.activity-card .card-head{padding-bottom:16px}.activity-list{padding:0 18px;flex:1}.activity{display:flex;align-items:flex-start;gap:11px;padding:15px 0;border-top:1px solid var(--line)}.activity:first-child{border-top:0;padding-top:3px}.activity-id{font-size:10px;color:#d5ddca;font-weight:600}.activity-meta{font-size:9px;color:#7d8a72;margin-top:5px;line-height:1.5}.activity-amount{font-size:10px;font-weight:500;text-align:right;white-space:nowrap;margin-top:4px}.activity-label{font-size:8px;margin-top:5px;color:#7d8a72}.activity-icon{width:31px;height:31px;border-radius:9px;display:grid;place-items:center;border:1px solid #3b452f;background:#232b1d;color:#c5f56b}.activity-icon.purple{color:#b6a3f5;border-color:#443c52;background:#292530}.activity-icon.orange{color:#e8b78b;border-color:#4a3e31;background:#2c261f}.activity-icon svg{height:14px;width:14px}.activity-foot{margin:10px 18px 18px;border-top:1px solid var(--line);padding-top:15px;display:flex;align-items:center;justify-content:space-between;color:#97a789;font-size:10px}.active-pulse{display:flex;align-items:center;gap:7px}
.orders-card{overflow:hidden}.table-toolbar{display:flex;align-items:center;gap:18px;padding:0 20px;border-bottom:1px solid var(--line);min-height:50px}.tabs{display:flex;gap:20px;align-self:stretch}.tab{position:relative;color:#8f9a83;font-size:11px;padding:15px 0;display:flex;align-items:center;gap:7px;white-space:nowrap}.tab.active{color:var(--lime)}.tab.active:after{position:absolute;content:'';height:2px;bottom:-1px;left:0;right:0;background:var(--lime)}.tab-count{font-size:8px;background:#2a3323;color:#95a582;padding:2px 5px;border-radius:4px}.tab.active .tab-count{background:#c5f56b15;color:#c5f56b}.table-search{margin-left:auto;width:180px;position:relative}.table-search input{padding:7px 10px 7px 29px;font-size:10px;background:#161a14;border-color:#303a28;border-radius:7px}.table-search svg{position:absolute;left:10px;top:8px;height:12px;width:12px;color:#76856b}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;text-align:left;white-space:nowrap}th{font-size:9px;color:#79896e;font-weight:450;padding:13px 18px;border-bottom:1px solid var(--line);background:#181d16}td{font-size:10px;padding:15px 18px;border-bottom:1px solid #293025;color:#b9c4ae}tr:last-child td{border-bottom:0}tbody tr{transition:background .15s;cursor:pointer}tbody tr:hover{background:#21291b}td .order-id{color:#e5ebdf;font-weight:550;display:flex;align-items:center;gap:7px}td .sub{display:block;color:#738467;font-size:8px;margin-top:5px}td .route-address{max-width:180px;overflow:hidden;text-overflow:ellipsis;display:block}td .route-address+.route-address{margin-top:6px;color:#7e8e71}td .route-address:before{content:'';display:inline-block;width:4px;height:4px;background:#98a78c;border-radius:50%;margin-right:7px;vertical-align:2px}td .route-address+.route-address:before{background:var(--lime);border-radius:1px}.courier-cell{display:flex;align-items:center;gap:8px}.courier-cell .avatar{width:26px;height:26px;font-size:8px}.table-footer{display:flex;align-items:center;gap:9px;padding:13px 20px;border-top:1px solid var(--line);font-size:9px;color:#738667}.table-footer .pages{margin-left:auto;display:flex;gap:5px}.page-num{width:24px;height:24px;display:grid;place-items:center;border:1px solid #34422b;border-radius:5px;color:#8b9e7a}.page-num.active{color:var(--lime);background:#c5f56b10;border-color:#c5f56b44}.footer{margin-top:22px;display:flex;align-items:center;justify-content:space-between;font-size:8px;color:#5c6b51;letter-spacing:.4px}.footer .wordmark{font-size:12px;font-weight:700;letter-spacing:-.6px;color:#849775}.footer .wordmark span{font-size:7px;letter-spacing:.3px;font-weight:400;margin-left:8px}.notice{display:flex;gap:9px;align-items:center;padding:11px 15px;border:1px solid #465337;background:#26311b;border-radius:10px;color:#bfd4a9;font-size:11px;margin-bottom:20px}.notice svg{width:16px;height:16px;flex-shrink:0}.notice.warning{border-color:#574a32;background:#30291c;color:#d6c29f}.notice .btn{margin-left:auto}.empty{padding:50px 20px;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center;gap:15px;color:#819373}.empty svg{width:32px;height:32px;color:#596b4b}.empty b{font-size:14px;color:#d0ddc5}.empty p{font-size:11px;max-width:310px}.people-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:17px}.person-card{padding:22px;transition:border-color .2s}.person-card:hover{border-color:#536444}.person-head{display:flex;align-items:center;gap:12px;margin-bottom:22px}.person-head h3{font-size:13px}.person-head p{font-size:10px;color:var(--muted);margin-top:5px}.person-details{display:grid;grid-template-columns:1fr 1fr;gap:16px;border-top:1px solid var(--line);padding-top:17px;margin-bottom:20px}.person-details small{display:block;color:#7c8d6f;font-size:9px;margin-bottom:7px}.person-details b{font-size:14px;font-weight:500}.person-contact{font-size:11px;display:flex;align-items:center;gap:8px;color:#a8b69d;margin:10px 0}.person-contact svg{width:13px;height:13px;color:#6e8360}.person-bottom{display:flex;align-items:center;gap:8px;margin-top:20px}.person-bottom .btn{flex:1}.courier-list-row{display:flex;align-items:center;gap:10px;padding:13px 0;border-bottom:1px solid var(--line)}.courier-list-row:last-child{border-bottom:0}.courier-list-row b{font-size:11px}.courier-list-row p{font-size:9px;color:#809271;margin-top:4px}.courier-list-row .online-dot{height:6px;width:6px;border-radius:50%;background:var(--lime);margin-left:auto}.courier-list-row .online-dot.offline{background:#5a6652}.search-row{display:flex;align-items:center;gap:10px;margin-bottom:22px}.search-row input{max-width:340px;padding:11px 14px}.search-row .btn{margin-left:auto}.chips{display:flex;gap:8px;margin-bottom:20px}.chip{border:1px solid #34422a;border-radius:7px;padding:7px 12px;color:#8fa27e;font-size:10px}.chip.active{background:#c5f56b15;border-color:#c5f56b44;color:var(--lime)}
.modal-backdrop{position:fixed;inset:0;z-index:100;background:#070b08b8;backdrop-filter:blur(7px);display:flex;align-items:center;justify-content:center;padding:25px;animation:fade-in .18s ease}.modal{width:660px;max-width:100%;max-height:93dvh;display:flex;flex-direction:column;background:#1b2119;border:1px solid #48553c;border-radius:20px;box-shadow:0 35px 100px #0008;animation:slide-in .2s ease;overflow:hidden}.modal.wide{width:820px}.modal.narrow{width:460px}.modal-header{display:flex;align-items:center;gap:13px;padding:22px 25px;border-bottom:1px solid #323e2a}.modal-header h2{font-size:19px}.modal-header p{font-size:10px;color:#829775;margin-top:6px}.modal-header .icon-btn{margin-left:auto}.modal-body{padding:23px 25px;overflow:auto}.modal-footer{padding:17px 25px;display:flex;align-items:center;justify-content:flex-end;gap:10px;border-top:1px solid #323e2a}.modal-footer small{margin-right:auto;color:#91a381;font-size:9px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:17px}.field.full{grid-column:1/-1}.form-section{display:flex;align-items:center;gap:8px;color:#d3dfc9;font-size:11px;font-weight:600;margin:24px 0 17px}.form-section:first-child{margin-top:0}.form-section span{width:22px;height:22px;background:#c5f56b17;color:var(--lime);border:1px solid #c5f56b22;border-radius:6px;display:grid;place-items:center;font-size:9px}.form-help{font-size:9px;color:#829773;line-height:1.6;margin-top:6px}.form-error{font-size:11px;color:#f0b2a7;background:#492d24;border:1px solid #744236;padding:10px 12px;border-radius:8px;margin-top:15px;display:none}.form-error:not(:empty){display:block}.order-summary{padding:17px;background:#141c11;border:1px solid #36462b;border-radius:11px;display:flex;align-items:center;justify-content:space-between;margin-top:20px}.order-summary small{font-size:9px;color:#8a9e7b}.order-summary b{font-size:23px;font-weight:500;color:var(--lime);display:block;margin-top:5px}.map-select{height:190px;margin-top:16px;border:1px solid #3e4b33;border-radius:10px;overflow:hidden;position:relative}.map-select .map-wrap{height:190px}.map-select .map-bottom,.map-select .map-status,.map-select .map-controls,.map-select .map-attribution{display:none}.map-select .city-map{cursor:crosshair}.address-choices{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}.address-choices button{font-size:9px;background:#273220;border:1px solid #3a4a2e;border-radius:5px;padding:5px 7px;color:#a2b68f}.address-choices button:hover{border-color:var(--lime);color:var(--lime)}.detail-route{padding:18px;background:#141c11;border:1px solid #34462a;border-radius:11px;margin:20px 0}.route-stop{display:flex;align-items:center;gap:12px}.route-stop .stop{height:22px;width:22px;border:1px solid #5f754d;border-radius:50%;display:grid;place-items:center;color:#9db18b;font-size:9px;flex-shrink:0}.route-stop.end .stop{background:var(--lime);border-color:var(--lime);border-radius:6px;color:#263718}.route-stop p{font-size:12px}.route-stop small{font-size:9px;color:#8a9c7c;display:block;margin-top:3px}.route-dash{height:25px;border-left:1px dashed #5b7149;margin-left:10px}.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;padding:10px 0 20px}.detail-grid small{font-size:9px;color:#829a71;display:block;margin-bottom:8px}.detail-grid b{font-size:13px;font-weight:500}.history-list{margin:15px 0}.history-event{position:relative;display:flex;gap:13px;padding:10px 0 15px}.history-event:before{position:absolute;content:'';left:5px;top:20px;bottom:-6px;border-left:1px solid #3e5332}.history-event:last-child:before{display:none}.history-event .dot{width:11px;height:11px;border:3px solid #293920;outline:1px solid #718d56;background:var(--lime);margin-top:3px}.history-event b{font-size:11px;font-weight:500}.history-event p{font-size:10px;color:#8da37b;margin-top:4px}.history-event small{color:#657c55;font-size:9px;margin-left:auto;padding-top:2px;white-space:nowrap}.photo-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.proof-photo img{width:100%;aspect-ratio:1.3;object-fit:cover;border:1px solid #405732;border-radius:8px}.proof-photo p{font-size:9px;color:#8ca277;margin-top:6px}.proof-uploader{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;padding:30px 20px;background:#121b0e;border:1px dashed #5a7148;border-radius:12px;cursor:pointer;text-align:center;color:#9eb58a}.proof-uploader svg{width:32px;height:32px;color:var(--lime)}.proof-uploader b{font-size:12px;color:#cbddb9}.proof-uploader small{font-size:10px}.proof-uploader img{max-width:100%;max-height:220px;object-fit:contain;border-radius:8px}.proof-uploader input{max-width:250px}.confirm-text{font-size:12px;color:#a5b69a;line-height:1.8}.toast{position:fixed;z-index:200;bottom:28px;left:calc(50% + 80px);transform:translateX(-50%);max-width:calc(100vw - 30px);border:1px solid #6a8250;border-radius:12px;background:#293a1f;box-shadow:0 10px 50px #0005;padding:15px 20px;display:flex;align-items:center;gap:10px;color:#d7ebc3;font-size:12px;animation:toast-in .25s ease;white-space:normal}.toast.error{border-color:#915f48;background:#422d22;color:#f2d1bd}.toast svg{height:17px;width:17px}
.courier-hero{position:relative;background:linear-gradient(115deg,#283820,#1c2718);border:1px solid #435a32;border-radius:20px;padding:27px;overflow:hidden;margin-bottom:23px;display:flex;gap:20px;align-items:center}.courier-hero h2{font-size:25px;letter-spacing:-1px}.courier-hero p{color:#9fb38d;font-size:11px;margin-top:10px}.shift-toggle{display:flex;gap:11px;align-items:center;padding:12px 17px;border:1px solid #667d50;border-radius:10px;background:#35482a;flex-shrink:0}.switch{width:32px;height:18px;background:#424d3a;border-radius:30px;position:relative}.switch:after{content:'';width:12px;height:12px;position:absolute;top:3px;left:3px;border-radius:50%;background:#91a580;transition:.2s}.switch.on{background:#bce965}.switch.on:after{left:17px;background:#233619}.shift-toggle b{font-size:11px}.job-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.job-card{padding:22px}.job-card .job-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.job-card .job-head b{font-size:12px}.job-card .detail-route{margin:18px 0;border:0;background:#121a0e;padding:15px}.job-details{display:flex;align-items:center;gap:18px;font-size:10px;color:#92a37f;margin-bottom:18px}.job-bottom{display:flex;align-items:center;gap:15px}.job-bottom .pay{color:var(--lime);font-size:25px;font-weight:500;letter-spacing:-.8px}.job-bottom .pay small{display:block;color:#91a27f;font-size:8px;letter-spacing:0;margin-top:4px}.job-bottom .btn{margin-left:auto}.section-title{display:flex;align-items:center;gap:10px;margin:24px 0 17px}.section-title h2{font-size:16px}.section-title .pill{font-size:10px}.location-info{display:flex;gap:8px;align-items:center;font-size:10px;color:#91a67d}.tracking-grid{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:20px}.track-progress{padding:22px}.track-progress .track-eta{font-size:35px;letter-spacing:-1.5px;margin:13px 0 5px}.track-progress .track-eta small{font-size:14px;color:#91a77a;letter-spacing:0}.track-progress>p{font-size:10px;color:#91a77a}.track-courier{display:flex;align-items:center;gap:12px;padding:17px 0;margin-top:20px;border-top:1px solid #33462a;border-bottom:1px solid #33462a}.track-courier b{font-size:12px}.track-courier p{font-size:9px;color:#87a272;margin-top:5px}.track-courier a{margin-left:auto;border:1px solid #4c653b;border-radius:50%;height:32px;width:32px;display:grid;place-items:center;color:#b9d89e}.order-selector{max-width:260px;padding:8px 12px;font-size:11px}.profile-grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:22px}.profile-form{padding:25px}.profile-intro{display:flex;align-items:center;gap:17px;padding-bottom:25px;margin-bottom:25px;border-bottom:1px solid var(--line)}.profile-intro h2{font-size:20px}.profile-intro p{font-size:10px;color:#90a27f;margin-top:7px}.settings-card{padding:23px;margin-bottom:18px}.settings-card h2{display:flex;align-items:center;gap:9px;font-size:14px;margin-bottom:14px}.settings-card h2 svg{width:19px;height:19px;color:var(--lime)}.settings-card p{color:#91a27f;font-size:11px;margin-bottom:17px}.settings-card .row{display:flex;align-items:center;justify-content:space-between;font-size:11px;padding:12px 0;border-bottom:1px solid var(--line)}.settings-card .row:last-child{border:0}.settings-card .row small{color:#81946d;font-size:10px}.chart-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:22px;margin-bottom:23px}.chart-card{padding:23px}.chart-card .card-head{padding:0;margin-bottom:25px}.chart{height:220px;display:flex;align-items:flex-end;gap:12px;border-bottom:1px solid #3a4a2e;padding:0 10px;position:relative;background:repeating-linear-gradient(to top,transparent 0,transparent calc(25% - 1px),#3c4c2e55 25%);margin-bottom:32px}.bar-col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;gap:3px}.bar{width:100%;min-height:2px;border-radius:5px 5px 0 0;background:#c5f56b;position:relative;opacity:.9;transition:.3s}.bar-col:nth-child(even) .bar{background:#9fc950}.bar:hover{opacity:1;filter:brightness(1.2)}.bar-label{position:absolute;bottom:-22px;left:0;right:0;text-align:center;font-size:9px;color:#81966d}.bar-value{font-size:8px;color:#b8cf9e;text-align:center;margin-bottom:7px}.donut{width:145px;height:145px;border-radius:50%;margin:20px auto;background:conic-gradient(var(--lime) 0 70%,var(--purple) 70% 90%,#5c6b50 90%);position:relative;display:grid;place-items:center}.donut:before{content:'';position:absolute;inset:16px;background:var(--panel);border-radius:50%}.donut div{position:relative;text-align:center;font-size:26px;font-weight:500}.donut small{display:block;font-size:9px;color:#869c74;margin-top:5px}.chart-legend{display:flex;align-items:center;gap:9px;padding:9px 0;font-size:10px;color:#98ad85}.chart-legend b{margin-left:auto;color:#d3e3c4;font-weight:500}
.chat-shell{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:550px;height:calc(100dvh - 255px);overflow:hidden}.chat-sidebar{border-right:1px solid var(--line);overflow:auto;background:#161d12}.chat-sidebar h3{padding:20px;font-size:12px;color:#8fa87c}.chat-thread{display:flex;gap:10px;align-items:center;padding:17px 18px;width:100%;text-align:left;border-top:1px solid #2c3b24}.chat-thread.active{background:#27371c;border-left:2px solid var(--lime)}.chat-thread b{font-size:11px}.chat-thread p{font-size:9px;color:#789663;margin-top:6px}.chat-main{display:flex;flex-direction:column;min-width:0;overflow:hidden}.chat-header{display:flex;gap:10px;align-items:center;padding:16px 22px;border-bottom:1px solid var(--line)}.chat-header b{font-size:12px}.chat-header p{font-size:9px;color:#88a572;margin-top:4px}.chat-messages{padding:23px;overflow:auto;flex:1;display:flex;flex-direction:column;gap:16px;scroll-behavior:smooth}.message{max-width:80%;align-self:flex-start}.message.mine{align-self:flex-end}.message-name{font-size:9px;color:#8fa47d;margin:0 0 6px 2px}.message-bubble{font-size:12px;line-height:1.65;padding:12px 15px;background:#293423;border:1px solid #3a4a2f;border-radius:0 12px 12px 12px;white-space:pre-wrap;overflow-wrap:anywhere}.message.mine .message-bubble{background:#c5f56b;color:#1c2d0e;border-color:#c5f56b;border-radius:12px 0 12px 12px}.message-time{font-size:8px;color:#6f875c;text-align:right;margin-top:5px}.chat-compose{padding:16px 20px;border-top:1px solid var(--line);display:flex;gap:10px}.chat-compose input{font-size:12px;background:#121c0e}.chat-compose .btn{padding:11px 14px}.chat-empty{display:flex;align-items:center;justify-content:center;color:#8b9f79;flex:1;font-size:12px}.thread-loader{text-align:center;font-size:10px;color:#95b17f}.chat-more{align-self:center}
.welcome{background:#11170f;min-height:100dvh;overflow:hidden}.welcome-nav{max-width:1440px;padding:30px 6%;margin:auto;display:flex;align-items:center;gap:45px;position:relative;z-index:5}.welcome-nav .brand{font-size:39px}.welcome-nav nav{display:flex;gap:27px;margin-left:auto;color:#a0b68c;font-size:12px}.welcome-nav .btn{margin-left:12px}.welcome-hero{max-width:1440px;margin:auto;min-height:660px;padding:82px 6% 85px;position:relative;display:grid;grid-template-columns:1.05fr 1fr;gap:30px;align-items:center}.welcome-copy{position:relative;z-index:3}.welcome-copy .eyebrow{letter-spacing:2.7px;font-size:10px;margin-bottom:30px}.welcome-copy h1{font-size:clamp(50px,6vw,84px);letter-spacing:-4px;line-height:1.03;font-weight:550;max-width:650px}.welcome-copy h1 span{color:var(--lime)}.welcome-copy>p{font-size:14px;color:#94ac80;max-width:380px;margin:25px 0 29px;line-height:1.8}.welcome-ctas{display:flex;gap:13px;flex-wrap:wrap}.hero-note{display:flex;align-items:center;gap:12px;margin-top:33px;color:#91a77d;font-size:10px}.hero-note .avatar{border-color:#344a27}.hero-map{position:relative;border:1px solid #41572f;border-radius:30px;transform:rotate(-6deg);box-shadow:0 60px 140px #040b0255;overflow:visible}.hero-map:before{content:'';position:absolute;inset:-90px;background:radial-gradient(ellipse,#8cc34d22,transparent 70%);z-index:-1}.hero-map .map-wrap{height:440px;border-radius:29px}.hero-map .map-attribution{display:none}.hero-map .map-bottom{display:none}.hero-map .map-status{transform:rotate(6deg);left:-30px;bottom:30px;padding:16px}.hero-label{position:absolute;right:-12px;top:-18px;transform:rotate(6deg);border-radius:11px;background:var(--lime);color:#253817;font-size:11px;padding:13px 18px;box-shadow:0 10px 25px #0004;z-index:3;display:flex;align-items:center;gap:9px}.hero-label svg{width:17px;height:17px}.welcome-strip{max-width:1270px;margin:0 auto;border-top:1px solid #34492a;border-bottom:1px solid #34492a;display:grid;grid-template-columns:repeat(3,1fr)}.welcome-stat{padding:32px 40px;border-right:1px solid #34492a}.welcome-stat:last-child{border:0}.welcome-stat strong{font-size:30px;color:#d9edc9;font-weight:500;letter-spacing:-1px}.welcome-stat p{font-size:11px;color:#859e71;margin-top:9px}.welcome-features{max-width:1270px;margin:75px auto;padding:0 20px}.welcome-features h2{font-size:30px;letter-spacing:-1px}.feature-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:27px}.feature{padding:28px;border:1px solid #34472a;border-radius:16px;background:#172211}.feature svg{color:var(--lime);height:27px;width:27px;margin-bottom:25px}.feature h3{font-size:17px;letter-spacing:-.5px}.feature p{color:#88a474;font-size:12px;line-height:1.9;margin:13px 0 24px}.welcome-footer{padding:35px 6%;border-top:1px solid #2a3b21;display:flex;align-items:center;justify-content:space-between;color:#688351;font-size:10px}.auth-page{min-height:100dvh;display:grid;grid-template-columns:1fr 1fr;background:#12190e}.auth-story{padding:45px 9% 45px;background:radial-gradient(ellipse at 40% 60%,#2c431c,#141f0f 65%);border-right:1px solid #354c29;display:flex;flex-direction:column;position:relative;overflow:hidden}.auth-story .brand{position:relative;z-index:2}.auth-story .auth-visual{margin:auto 0;padding:65px 0;position:relative;z-index:2}.auth-story h1{font-size:52px;line-height:1.08;letter-spacing:-2.3px}.auth-story h1 span{color:var(--lime)}.auth-story p{max-width:360px;margin-top:24px;font-size:12px;color:#8fab79;line-height:1.9}.auth-story .auth-orbit{position:absolute;border:1px solid #435e2e;border-radius:50%;width:660px;height:660px;bottom:-320px;left:-150px;opacity:.4}.auth-story .auth-orbit:before,.auth-story .auth-orbit:after{content:'';position:absolute;inset:65px;border:1px solid #47632f;border-radius:50%}.auth-story .auth-orbit:after{inset:125px}.auth-story .auth-bottom{font-size:9px;color:#6c8955;letter-spacing:.5px;position:relative}.auth-form-wrap{display:flex;align-items:center;justify-content:center;padding:45px}.auth-form{width:370px;max-width:100%}.auth-form h2{font-size:27px;letter-spacing:-1px;margin-bottom:10px}.auth-form>p{font-size:11px;color:#8aa174;margin-bottom:28px}.auth-form .field{margin-top:17px}.auth-form .btn.full{margin-top:23px}.auth-form .form-help{margin-top:18px;text-align:center}.auth-form .auth-switch{margin-top:22px;text-align:center;color:#8fa67d;font-size:11px}.auth-form .auth-switch a{color:var(--lime)}.auth-demo{border-top:1px solid #33472a;margin-top:28px;padding-top:22px}.auth-demo p{font-size:10px;color:#819b6d;margin-bottom:12px}.demo-roles{display:flex;gap:7px}.demo-roles .btn{flex:1;font-size:10px;padding:9px 6px}.auth-role-choice{display:flex;gap:8px;margin:20px 0}.auth-role-choice label{flex:1;background:#1d2b15;border:1px solid #3e542e;border-radius:8px;padding:11px;font-size:11px;display:flex;align-items:center;gap:6px;cursor:pointer;margin:0}.auth-role-choice input{width:auto;accent-color:var(--lime)}.public-track{max-width:1150px;margin:0 auto;padding:32px 25px}.public-track .brand{margin-bottom:45px;font-size:32px}.public-track .page-heading{margin-bottom:30px}.public-track .tracking-grid{grid-template-columns:minmax(0,1fr) 320px}
@keyframes route-flow{to{stroke-dashoffset:-1000}}@keyframes loading{from{transform:translateX(-100%)}to{transform:translateX(200%)}}@keyframes fade-in{from{opacity:0}to{opacity:1}}@keyframes slide-in{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}@keyframes toast-in{from{opacity:0;transform:translate(-50%,12px)}to{opacity:1;transform:translate(-50%,0)}}
@media(min-width:1550px){:root{--side:240px}.content{padding:35px 42px}.topbar{padding:0 42px}.map-wrap{height:410px}.grid-main{grid-template-columns:minmax(0,1fr) 320px}.activity{padding:18px 0}.stat{min-height:130px}.stat-value{font-size:33px}td{padding-top:17px;padding-bottom:17px}}
@media(max-width:1180px){:root{--side:190px}.sidebar{padding:28px 12px 0}.sidebar .brand{padding-left:10px;font-size:33px}.brand-mark{width:27px}.content{padding:25px 24px}.topbar{padding:0 24px}.grid-main{grid-template-columns:minmax(0,1fr) 250px;gap:15px}.map-toolbar{display:none}.stat{padding:16px 14px}.spark{width:54px}.stat-value{font-size:25px}.people-grid{grid-template-columns:repeat(2,1fr)}.profile-grid{grid-template-columns:minmax(0,1fr) 280px}.breadcrumb span:first-child{display:none}.chart-grid{grid-template-columns:minmax(0,1fr) 260px}.map-full-grid{grid-template-columns:minmax(0,1fr) 230px}.topbar-search span{display:none}.topbar-search .keycap{display:none}.welcome-copy h1{font-size:63px}.hero-map .map-wrap{height:400px}}
@media(max-width:960px){.grid-main{grid-template-columns:1fr}.activity-card{display:none}.stats{gap:10px}.stat .spark{display:none}.stat-value{font-size:26px}.stat-change{font-size:8px}.stat-foot{font-size:8px}.map-wrap{height:380px}.tracking-grid{grid-template-columns:1fr 260px}.topbar-right{gap:9px}.demo-badge{display:none}.page-heading h1{font-size:27px}.job-grid{grid-template-columns:1fr}.profile-grid{grid-template-columns:1fr}.profile-aside{display:grid;grid-template-columns:1fr 1fr;gap:16px}.chart-grid{grid-template-columns:1fr}.chart-grid .breakdown{display:none}.map-full-grid{grid-template-columns:1fr}.map-full-grid .map-side{display:none}.chat-shell{grid-template-columns:200px 1fr}.welcome-hero{padding-top:50px;padding-bottom:65px;min-height:560px}.welcome-copy h1{font-size:52px;letter-spacing:-2.8px}.welcome-copy>p{font-size:12px}.welcome-nav nav{display:none}.welcome-nav .btn{margin-left:auto}.hero-map .map-wrap{height:380px}.auth-story{padding:40px}.auth-story h1{font-size:42px}.auth-form-wrap{padding:30px}.public-track .tracking-grid{grid-template-columns:1fr 280px}}
@media(max-width:760px){:root{--side:0px}.sidebar{width:225px;transform:translateX(-100%);transition:.2s;box-shadow:15px 0 80px #0008}.sidebar.open{transform:translateX(0)}.sidebar .brand{font-size:37px}.main{margin:0}.mobile-menu{display:flex}.topbar{height:64px;padding:0 18px;gap:11px}.breadcrumb{font-size:10px}.topbar-right{gap:6px}.topbar .topbar-search,.topbar .date-chip,.topbar .divider{display:none}.role-select{width:112px;font-size:9px}.topbar-right .avatar{height:29px;width:29px;font-size:9px}.content{padding:24px 18px}.page-heading{align-items:flex-start;gap:12px;margin-bottom:22px}.page-heading h1{font-size:28px;letter-spacing:-1px}.page-heading .subtitle{font-size:10px;max-width:220px;line-height:1.8}.page-actions .btn{font-size:10px;padding:10px 12px}.page-actions .btn.ghost{display:none}.stats{grid-template-columns:repeat(2,1fr);gap:11px;margin-bottom:18px}.stat{padding:16px;min-height:110px}.stat-top{font-size:10px}.stat-value{font-size:28px}.stat-bottom{margin-top:15px}.stat-foot{font-size:9px}.stat .spark{display:block;width:60px}.grid-main{margin-bottom:18px}.map-wrap{height:350px}.card-head{padding:18px 17px}.map-toolbar{display:none}.map-bottom{gap:13px;padding:12px 15px;font-size:8px}.map-bottom .last{display:none}.card-head .right .btn{font-size:9px}.card-head h2{font-size:13px}.table-toolbar{padding:0 15px;gap:15px;flex-wrap:wrap}.tabs{gap:17px;min-height:48px}.table-search{width:100%;margin:0 0 12px}.tab{font-size:10px}td{padding:14px 15px}th{padding:12px 15px}.table-footer{padding:13px 15px}.table-footer>span{max-width:180px;line-height:1.6}.footer{font-size:7px;margin-top:25px}.footer>span:last-child{display:none}.people-grid{grid-template-columns:1fr}.person-card{padding:22px}.search-row input{width:100%;max-width:none}.search-row{flex-wrap:wrap}.search-row .btn{margin-left:0}.tracking-grid{grid-template-columns:1fr}.track-progress{order:-1}.track-progress .history-list{display:none}.track-courier{margin-top:15px}.tracking-map .map-wrap{height:380px}.job-card{padding:19px}.courier-hero{padding:22px;align-items:flex-start;gap:15px;flex-direction:column}.courier-hero h2{font-size:24px}.courier-hero p{font-size:10px}.shift-toggle{align-self:flex-start;padding:11px 14px}.courier-hero .grow{width:100%}.profile-aside{display:block}.profile-form{padding:22px}.profile-intro h2{font-size:19px}.profile-grid{gap:18px}.chart-card{padding:19px}.chart{gap:7px;padding:0 3px;height:200px}.bar-value{font-size:7px}.bar-label{font-size:8px}.modal-backdrop{padding:10px}.modal{max-height:96dvh;border-radius:17px}.modal-header{padding:19px 18px}.modal-body{padding:20px 18px}.modal-footer{padding:16px 18px}.modal-footer small{display:none}.form-grid{gap:15px}.form-grid.mobile-single{grid-template-columns:1fr}.form-grid.mobile-single .field{grid-column:auto}.detail-grid{grid-template-columns:repeat(2,1fr);gap:19px}.photo-grid{grid-template-columns:repeat(2,1fr)}.chat-shell{display:flex;flex-direction:column;height:calc(100dvh - 215px);min-height:480px}.chat-sidebar{border-right:0;border-bottom:1px solid var(--line);display:flex;max-height:78px;overflow:auto;flex-shrink:0}.chat-sidebar h3{display:none}.chat-thread{min-width:180px;padding:13px;flex-shrink:0;width:auto;border-top:0;border-right:1px solid var(--line)}.chat-thread.active{border-left:0;border-bottom:2px solid var(--lime)}.chat-thread .avatar{height:27px;width:27px;font-size:9px}.chat-thread b{font-size:10px}.chat-thread p{font-size:8px}.chat-main{flex:1}.chat-header{padding:14px 17px}.chat-messages{padding:18px}.chat-compose{padding:13px}.message{max-width:90%}.message-bubble{font-size:11px}.toast{left:50%;bottom:20px;font-size:11px;padding:13px 16px}.welcome-nav{padding:26px 23px}.welcome-nav .brand{font-size:33px}.welcome-nav .btn{font-size:11px;padding:9px 12px}.welcome-hero{display:flex;flex-direction:column;padding:40px 24px 60px;gap:60px;align-items:stretch}.welcome-copy h1{font-size:58px;letter-spacing:-3px}.welcome-copy .eyebrow{margin-bottom:25px;font-size:9px}.welcome-copy>p{max-width:320px;font-size:13px}.welcome-ctas .btn{font-size:12px}.hero-map{margin:0 12px;transform:rotate(-3deg);border-radius:23px}.hero-map .map-wrap{height:340px;border-radius:22px}.hero-map .map-status{left:8px;transform:rotate(3deg);bottom:25px;padding:12px}.hero-label{right:-8px;transform:rotate(3deg);font-size:10px}.welcome-strip{margin:0 24px;grid-template-columns:1fr}.welcome-stat{padding:24px 10px;display:flex;align-items:center;justify-content:space-between;border-right:0;border-bottom:1px solid #34492a}.welcome-stat strong{font-size:25px}.welcome-stat p{font-size:10px;max-width:180px;margin:0;text-align:right}.welcome-features{padding:0 24px;margin:55px auto}.welcome-features h2{font-size:26px}.feature-grid{grid-template-columns:1fr}.feature{padding:25px}.welcome-footer{padding:25px;gap:25px;font-size:8px}.auth-page{grid-template-columns:1fr}.auth-story{padding:30px;border:0;min-height:auto}.auth-story .auth-visual{padding:40px 0 10px}.auth-story h1{font-size:38px;max-width:360px}.auth-story p,.auth-story .auth-bottom{display:none}.auth-form-wrap{padding:35px 26px 50px}.auth-form{width:100%}.auth-form h2{font-size:24px}.public-track{padding:28px 18px}.public-track .brand{margin-bottom:33px}.public-track .tracking-grid{grid-template-columns:1fr}.public-track .page-heading h1{font-size:25px}.public-track .track-progress{order:-1}.public-track .track-progress .history-list{display:block}.map-large .map-wrap{height:calc(100dvh - 260px);min-height:420px}}
@media(max-height:850px){.sidebar .city-card{display:none}.sidebar{padding-top:25px}.sidebar .brand{padding-bottom:22px}.workspace{margin-bottom:22px}.sidebar-bottom{padding-top:12px}}
@media(max-width:760px){.topbar .breadcrumb{display:none}.topbar-right>button:last-child{padding:0}.page-heading{flex-wrap:wrap}.tabs{max-width:100%;overflow-x:auto}.sidebar{overflow-y:auto}.chips{max-width:100%;overflow-x:auto}.chip{flex-shrink:0}}
*{scrollbar-width:thin;scrollbar-color:#3d4a32 transparent}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}</style></head><body><div id="app"><div class="boot"><div class="brand">dropp<span>®</span></div><div class="boot-line"></div><p>Собираем город в одно целое</p></div></div><div id="overlay"></div><div id="toast-root" aria-live="polite"></div><script nonce="%%NONCE%%">(function clientMain() {
  'use strict';
  const $=(s,root=document)=>root.querySelector(s);
  const $$=(s,root=document)=>[...root.querySelectorAll(s)];
  const app=$('#app'),overlay=$('#overlay');
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=n=>new Intl.NumberFormat('ru-RU').format(n||0);
  const money=n=>fmt(n)+' ֏';
  const orderId=id=>'DR-'+String(id).padStart(4,'0');
  const initials=name=>String(name||'?').split(' ').filter(Boolean).slice(0,2).map(x=>x[0]).join('');
  const first=name=>String(name||'Курьер').split(' ')[0];
  const date=(iso,opts={day:'numeric',month:'short'})=>new Date(iso).toLocaleDateString('ru-RU',{...opts,timeZone:'Asia/Yerevan'});
  const time=iso=>new Date(iso).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Yerevan'});
  const dayKey=iso=>new Date(iso).toLocaleDateString('en-CA',{timeZone:'Asia/Yerevan'});
  const statusLabel={new:'Новый',assigned:'Курьер назначен',picked_up:'В пути',delivered:'Доставлен',cancelled:'Отменён'};
  const roleLabel={admin:'Администратор',courier:'Курьер',client:'Клиент'};
  const vehicles={bike:'Велосипед',scooter:'Скутер',car:'Автомобиль',walk:'Пешком'};
  const paths={
    overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    box:'m3 7 9-5 9 5v10l-9 5-9-5z M3 7l9 5 9-5 M12 12v10 M7.5 4.5l9 5',
    map:'m9 18-6 3V5l6-3 6 3 6-3v16l-6 3-6-3z M9 2v16 M15 5v16',
    bike:'M18 17a3.5 3.5 0 1 0 0 .01 M5 17a3.5 3.5 0 1 0 0 .01 M5 17l5-9h4l4 9 M10 8l5 9H5 M9 5h4 M14 3h3l2 4',
    scooter:'M4 17h12l4-10h-4 M6 17a2 2 0 1 0 0 .01 M18 17a2 2 0 1 0 0 .01 M5 14v-4h5',
    car:'m3 10 2-6h14l2 6v9h-3v-3H6v3H3z M3 10h18 M6 13h2 M16 13h2',
    walk:'M13 3a1 1 0 1 0 0 .01 M7 21l4-8-2-4 3-3 4 5 4 1 M12 6l2 8 3 7 M9 9l-4 3',
    people:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
    wallet:'M3 6h17v15H3a1 1 0 0 1-1-1V4a2 2 0 0 1 2-2h14v4 M20 10h-6v6h6 M16 13h.01',
    chat:'M21 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-4-.9L3 21l1.9-5.5A9 9 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3H13a8.5 8.5 0 0 1 8 8v.5z',
    settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3l-1 3-3 1-2 4 2 2v3l3 3 3-1 3 1 3-3v-3l2-2-2-4-3-1-1-3z',
    plus:'M12 5v14 M5 12h14',minus:'M5 12h14',arrow:'M5 12h14 M13 6l6 6-6 6',arrowUp:'m7 17 10-10 M7 7h10v10',
    chevron:'m9 5 7 7-7 7',down:'m6 9 6 6 6-6',search:'M21 21l-5-5 M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15',
    bell:'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4',
    calendar:'M4 5h16v16H4z M16 3v4 M8 3v4 M4 11h16',clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2',
    pin:'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0z M12 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
    locate:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16',
    layers:'m12 3 10 6-10 6L2 9z M2 14l10 6 10-6 M2 19l10 6 10-6',
    expand:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5',
    more:'M5 12h.01 M12 12h.01 M19 12h.01',close:'M6 6l12 12 M6 18 12 6',
    check:'m5 12 4 4L19 6',checkCircle:'M21 11v1a9 9 0 1 1-5-8 M21 3l-9 9-3-3',
    phone:'M22 17v3a2 2 0 0 1-2.2 2 20 20 0 0 1-17-17A2 2 0 0 1 5 3h3l2 5-2 2a14 14 0 0 0 6 6l2-2z',
    mail:'M3 5h18v14H3z M3 5l9 8 9-8',
    camera:'M4 7h4l2-3h4l2 3h4v14H4z M12 10a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
    upload:'M12 16V3 M7 8l5-5 5 5 M3 16v5h18v-5',download:'M12 3v13 M7 11l5 5 5-5 M3 16v5h18v-5',
    send:'m22 2-7 20-4-9-9-4z M22 2 11 13',telegram:'m22 3-4 18-7-5-4 3 1-6L22 3 2 11l6 2 M11 16l7-9',
    shield:'m12 2 9 4v6c0 6-9 10-9 10S3 18 3 12V6z M8 12l3 3 5-6',
    logout:'M9 3H3v18h6 M9 12h12 M16 7l5 5-5 5',menu:'M3 6h18 M3 12h18 M3 18h18',
    bolt:'m13 2-9 12h7l-1 8L21 9h-8z',info:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 11v6 M12 7h.01',
    weight:'M6 7h12l3 14H3z M9 7V5a3 3 0 0 1 6 0v2',link:'M10 13a5 5 0 0 0 7 0l4-4a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7 0l-4 4a5 5 0 0 0 7 7l2-2',
    leaf:'M20 3c0 13-5 17-12 16S2 5 20 3z M5 21l11-13'
  };
  const icon=(name,size=18)=>`<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.box}"/></svg>`;
  const brand=()=>`<span class="brand">${`<svg class="brand-mark" viewBox="0 0 35 40" fill="currentColor" aria-hidden="true"><path d="M12 3h14l-8 16h13L18 37H4l8-16H1z"/></svg>`}dropp<span>®</span></span>`;
  const colors=['','violet','orange','blue','pink'];
  const avatar=(name,id=0,extra='')=>`<span class="avatar ${colors[id%colors.length]} ${extra}">${esc(initials(name))}</span>`;
  const badge=status=>`<span class="pill ${esc(status)}"><span class="dot"></span>${esc(statusLabel[status]||status)}</span>`;
  const express=()=>`<span class="pill express">${icon('bolt',10)} Экспресс</span>`;
  const btn=(label,action,ic='',extra='',attrs='')=>`<button type="button" class="btn ${extra}" data-action="${action}" ${attrs}>${ic?icon(ic,15):''}${label}</button>`;
  const empty=(title,description,ic='box',button='')=>`<div class="empty">${icon(ic,32)}<b>${title}</b><p>${description}</p>${button}</div>`;
  const field=(label,name,value='',type='text',attrs='',full=false)=>`<div class="field ${full?'full':''}"><label for="f-${name}">${label}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${attrs}></div>`;
  let authEpoch=0;
  let S=null,config=null,page='overview',orderTab='all',orderSearch='',orderPage=1,peopleSearch='',peopleFilter='all',period=14;
  let selectedOrder=null,selectedCourier=null,thread=null,events=null,watchId=null,notificationEnabled=false;
  let modalCloseFocus=null,orderDraft=null,mapPick='pickup',refreshTimer=null,toastTimer=null,publicData=null,publicTimer=null;
  const mapViews=new Map();
  const person=id=>S?.people.find(p=>p.id===id);
  const myOrders=()=>S.orders;
  const active=o=>['assigned','picked_up'].includes(o.status);
  const formObject=form=>Object.fromEntries(new FormData(form));
  async function api(path,method='GET',body) {
    const response=await fetch('/api'+path,{method,headers:{...(body?{'Content-Type':'application/json'}:{}),...(S?.csrf?{'X-CSRF-Token':S.csrf}:{})},body:body?JSON.stringify(body):undefined,credentials:'same-origin'});
    const result=await response.json();
    if(!response.ok){const e=new Error(result.error||'Не удалось выполнить запрос.');e.status=response.status;throw e;}
    return result;
  }
  function toast(message,error=false) {
    clearTimeout(toastTimer);$('#toast-root').innerHTML=`<div class="toast ${error?'error':''}" role="status">${icon(error?'info':'checkCircle')}${esc(message)}</div>`;
    toastTimer=setTimeout(()=>$('#toast-root').innerHTML='',4500);
  }
  async function busy(button,fn) {if(button?.disabled)return;if(button)button.disabled=true;try{return await fn();}catch(e){toast(e.message,true);}finally{if(button)button.disabled=false;}}
  async function submitForm(form,fn) {
    const error=$('.form-error',form),button=$('[type=submit]',form)||document.querySelector(`[form="${form.id}"]`);
    if(error)error.textContent='';if(button?.disabled)return;if(button)button.disabled=true;
    try{await fn(formObject(form));}catch(e){if(error)error.textContent=e.message;else toast(e.message,true);}finally{if(button)button.disabled=false;}
  }
  async function refresh(force=false) {
    try {
      const epoch=authEpoch;const next=await api('/me');if(epoch!==authEpoch)return;S=next;
      const focused=document.activeElement;
      if(force||(!overlay.firstChild&&!focused?.matches('input,textarea,select'))){
        const scroll=window.scrollY;renderApp();window.scrollTo(0,scroll);
      }else if(page==='chat'&&thread)await loadMessages();
    }catch(e){if(e.status===401){events?.close();S=null;renderAuth();}else toast('Связь прервалась. Изменения можно повторить.',true);}
  }
  function connectEvents() {
    events?.close();events=new EventSource('/api/events');
    events.onmessage=e=>{
      let update;try{update=JSON.parse(e.data);}catch{return;}
      if(update.type==='connected')return;
      if(update.type==='order_created'&&S?.user.role==='courier'&&S.user.online){toast('Новый заказ! Успейте забрать его.');if(notificationEnabled&&'Notification'in window&&Notification.permission==='granted')new Notification('Новый заказ · DROPP',{body:'Откройте кабинет, чтобы принять заказ.'});}
      clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>refresh(),450);
    };
    events.onerror=()=>{const el=$('#connection-state');if(el)el.innerHTML='Восстанавливаем соединение…';};
    events.onopen=()=>{const el=$('#connection-state');if(el)el.innerHTML='Все системы в норме';};
  }
  const navPages={overview:'Обзор',orders:'Заказы',map:'Карта города',couriers:'Курьеры',clients:'Клиенты',finance:'Финансы',chat:'Сообщения',profile:'Профиль',settings:'Настройки',jobs:'Биржа заказов',tracking:'Отслеживание'};
  function allowedPages(){return S.user.role==='admin'?['overview','orders','map','couriers','clients','finance','chat','settings','profile']:S.user.role==='courier'?['jobs','orders','map','finance','chat','profile']:['overview','orders','tracking','chat','profile'];}
  function navItem(key,ic,count='') {return `<a href="#${key}" class="nav-item ${page===key?'active':''}" ${page===key?'aria-current="page"':''}>${icon(ic)}${navPages[key]}${count!==''?`<span class="count">${count}</span>`:''}</a>`;}
  function renderApp() {
    if(!S)return;
    page=location.hash.slice(1)||({admin:'overview',courier:'jobs',client:'overview'}[S.user.role]);
    if(!allowedPages().includes(page))page=allowedPages()[0];
    const role=S.user.role,couriers=S.people.filter(p=>p.role==='courier'),clients=S.people.filter(p=>p.role==='client');
    let nav='';
    if(role==='admin')nav=navItem('overview','overview')+navItem('orders','box',S.orders.filter(o=>o.status==='new').length)+navItem('map','map')+navItem('couriers','bike',couriers.length)+navItem('clients','people')+navItem('finance','wallet');
    else if(role==='courier')nav=navItem('jobs','bolt',S.orders.filter(o=>o.status==='new').length)+navItem('orders','box',S.orders.filter(o=>o.courier_id===S.user.id&&active(o)).length)+navItem('map','map')+navItem('finance','wallet');
    else nav=navItem('overview','overview')+navItem('orders','box')+navItem('tracking','map');
    app.innerHTML=`<aside class="sidebar" id="sidebar"><a href="/welcome" aria-label="DROPP, главная">${brand()}</a><div class="workspace"><span class="city-flag"></span><div class="grow"><b>Ереван, Армения</b><small>${role==='admin'?'Диспетчерская':role==='courier'?'Кабинет курьера':'Личный кабинет'}</small></div>${icon('down',12)}</div><div class="nav-label">${role==='admin'?'РАБОЧЕЕ ПРОСТРАНСТВО':'ВАШ ГОРОД'}</div><nav class="nav">${nav}</nav><div class="nav-separator"></div><div class="nav-label">НА СВЯЗИ</div><nav class="nav">${navItem('chat','chat',S.threads.length||'')}${navItem(role==='admin'?'settings':'profile','settings')}</nav><div class="sidebar-bottom"><div class="city-card"><p>Город не ждёт.<br><span class="lime">Двигайся с ним.</span></p><small>Сделано для Еревана ↗</small><svg width="100" height="80" viewBox="0 0 100 80" fill="none"><path d="M0 78C28 56 13 12 66 10S76 61 100 36 M4 90C38 50 25 22 60 23S86 68 112 27" stroke="#a7cb75" stroke-width="1.5" stroke-dasharray="4 5"/></svg></div><button class="user-card" data-action="profile" style="width:100%;text-align:left">${avatar(S.user.name,S.user.id,'square')}<span class="grow"><b>${esc(S.user.name)}</b><small>${roleLabel[role]}</small></span>${icon('more',17)}</button></div></aside><main class="main"><header class="topbar"><button class="icon-btn mobile-menu" data-action="menu" aria-label="Открыть меню">${icon('menu')}</button><div class="breadcrumb"><span>РАБОЧЕЕ ПРОСТРАНСТВО</span><span>/</span><b>${navPages[page]}</b></div><div class="topbar-right">${S.demo?`<span class="demo-badge">ДЕМО</span><select class="role-select" id="demo-role" aria-label="Демонстрационная роль"><option value="admin" ${role==='admin'?'selected':''}>Администратор</option><option value="courier" ${role==='courier'?'selected':''}>Курьер</option><option value="client" ${role==='client'?'selected':''}>Клиент</option></select>`:''}<button class="topbar-search" data-action="search">${icon('search',16)}<span>Поиск<kbd class="keycap">⌘ K</kbd></span></button><span class="divider"></span><button class="icon-btn" data-action="notifications" aria-label="Уведомления">${icon('bell')}${S.orders.some(o=>o.status==='new')?'<span class="notification-dot"></span>':''}</button><button data-action="profile" aria-label="Профиль">${avatar(S.user.name,S.user.id)}</button></div></header><div class="content" id="page-content">${renderPage()}</div></main>`;
    if(page==='chat')loadMessages();
    mountMaps();
  }
  function footer(){return `<footer class="footer"><span class="wordmark">dropp<span>YEREVAN DISPATCH SYSTEM</span></span><span><span class="dot lime" style="margin-right:5px"></span><span id="connection-state">Все системы в норме</span>${S.demo?' · Демонстрационные данные':''}</span><span>40.1872° N &nbsp; 44.5152° E</span></footer>`;}
  function heading(title,sub,actions=''){return `<div class="page-heading"><div><h1>${title}</h1><p class="subtitle">${sub}</p></div><div class="page-actions">${actions}</div></div>`;}
  function renderPage() {
    if(page==='overview')return S.user.role==='admin'?adminOverview():clientOverview();
    if(page==='orders')return ordersPage();
    if(page==='map')return mapPage();
    if(page==='couriers'||page==='clients')return peoplePage(page==='couriers'?'courier':'client');
    if(page==='finance')return financePage();
    if(page==='jobs')return jobsPage();
    if(page==='tracking')return trackingPage();
    if(page==='chat')return chatPage();
    if(page==='profile')return profilePage();
    if(page==='settings')return settingsPage();
    return '';
  }
  function spark(values,color='#c5f56b') {
    const max=Math.max(...values,1);const pts=values.map((v,i)=>`${i*10},${26-v/max*22}`).join(' ');
    return `<svg class="spark" viewBox="0 0 80 30" fill="none"><polyline points="${pts}" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><polygon points="0,30 ${pts} 70,30" fill="${color}" opacity=".035"/></svg>`;
  }
  function stat(label,value,unit,foot,ic='box',values=null,color='#c5f56b'){
    return `<div class="card stat"><div class="stat-top">${label}<span class="stat-icon">${icon(ic,15)}</span></div><div class="stat-bottom"><span class="stat-value mono">${value}${unit?`<small>${unit}</small>`:''}</span>${values?spark(values,color):''}</div><div class="stat-foot"><span class="dot" style="color:${color}"></span>${foot}</div></div>`;
  }
  function adminOverview() {
    const today=S.orders.filter(o=>dayKey(o.created_at)===dayKey(new Date())),moving=S.orders.filter(active),online=S.people.filter(p=>p.role==='courier'&&p.online);
    const revenue=today.filter(o=>o.status!=='cancelled').reduce((a,o)=>a+o.fee,0);
    const daily=Array.from({length:8},(_,i)=>S.orders.filter(o=>dayKey(o.created_at)===dayKey(Date.now()-(7-i)*86400000)).length);
    const route=S.orders.find(o=>o.status==='picked_up')||moving[0];
    return heading('Город в движении<span class="lime">.</span>','Всё, что происходит с доставкой. Здесь и сейчас.',btn('Новый заказ','new-order','plus','primary'))+`<div class="stats">${stat('Заказы сегодня',today.length,'',S.orders.filter(o=>o.status==='new').length+' ожидают курьера','box',daily)}${stat('На доставке',moving.length,'','Заказы под контролем','bolt',null,'#b6a3f5')}${stat('Курьеры на линии',online.length,'/ '+S.people.filter(p=>p.role==='courier').length,'Готовы к новым маршрутам','bike',null,'#c5f56b')}${stat('Оборот сегодня',fmt(revenue),'֏','Стоимость заказов, не поступления','wallet',Array.from({length:8},(_,i)=>S.orders.filter(o=>o.status!=='cancelled'&&dayKey(o.created_at)===dayKey(Date.now()-(7-i)*86400000)).reduce((a,o)=>a+o.fee,0)),'#d5ba91')}</div><div class="grid-main"><section class="card map-card"><div class="card-head"><h2>Город под контролем</h2><span class="live"><span class="dot"></span>LIVE</span><div class="map-toolbar"><button class="map-tab active" data-action="map">Курьеры</button><button class="map-tab" data-action="orders">Заказы</button></div><div class="right">${btn('На всю карту','map','expand','ghost small')}</div></div>${mapHTML('overview',route)}<div class="map-bottom"><span class="legend"><span class="dot lime"></span>Свободен</span><span class="legend"><span class="dot purple"></span>На заказе</span><span class="legend"><span class="dot" style="color:#697861"></span>Не на линии</span><span class="last">Авторская карта Еревана</span></div></section>${activityCard()}</div><section class="card orders-card"><div class="card-head"><h2>Последние заказы</h2><span class="pill neutral">${S.orders.length}</span><div class="right">${btn('Все заказы','orders','arrow','link')}</div></div>${orderTable(S.orders.slice(0,5),false)}</section>${footer()}`;
  }
  function activityCard() {
    const orders=S.orders.filter(o=>o.status!=='cancelled').slice(0,4);
    return `<section class="card activity-card"><div class="card-head"><div><h2>Пульс доставки</h2><p>Последние обновления</p></div><div class="right">${icon('bolt',16)}</div></div><div class="activity-list">${orders.map((o,i)=>`<button class="activity" style="width:100%;text-align:left" data-action="order" data-id="${o.id}"><span class="activity-icon ${['','purple','orange',''][i]}">${icon(o.status==='delivered'?'check':o.status==='new'?'box':'bike',14)}</span><span class="grow"><span class="activity-id">#${orderId(o.id)}</span><span class="activity-meta" style="display:block">${esc(statusLabel[o.status])}<br>${esc(o.description)}</span></span><span><span class="activity-amount" style="display:block">${money(o.payout)}</span><span class="activity-label" style="display:block">${time(o.updated_at)}</span></span></button>`).join('')}</div><div class="activity-foot"><span class="active-pulse"><span class="dot lime"></span>Город на связи</span>${icon('arrow',14)}</div></section>`;
  }
  function filteredOrders(){
    let rows=S.orders;
    if(S.user.role==='courier')rows=rows.filter(o=>o.courier_id===S.user.id);
    if(orderTab==='active')rows=rows.filter(active);else if(orderTab!=='all')rows=rows.filter(o=>o.status===orderTab);
    if(orderSearch){const s=orderSearch.toLowerCase();rows=rows.filter(o=>(orderId(o.id)+' '+o.pickup+' '+o.dropoff+' '+o.description+' '+(person(o.client_id)?.name||'')).toLowerCase().includes(s));}
    return rows;
  }
  function orderTable(orders,pagination=true) {
    const role=S.user.role;
    return `<div class="table-scroll"><table><thead><tr><th>Номер / заказ</th><th>Маршрут</th><th>${role==='courier'?'Клиент':'Курьер'}</th><th>Статус</th><th>Вес</th><th>${role==='courier'?'Ваш доход':'Стоимость'}</th><th></th></tr></thead><tbody>${orders.map(o=>{
      const p=person(role==='courier'?o.client_id:o.courier_id);
      return `<tr data-action="order" data-id="${o.id}" tabindex="0" role="button" aria-label="Открыть заказ ${orderId(o.id)}"><td><span class="order-id"><span style="color:#6f875e">${icon('box',12)}</span>#${orderId(o.id)}</span><span class="sub">${esc(o.description)}${o.priority==='express'?' · ⚡':''}</span></td><td><span class="route-address">${esc(o.pickup)}</span><span class="route-address">${esc(o.dropoff)}</span></td><td>${p?`<span class="courier-cell">${avatar(p.name,p.id)}<span>${esc(first(p.name))}<span class="sub">${role==='courier'?'Клиент':esc(vehicles[p.vehicle]||'Курьер')}</span></span></span>`:'<span class="muted">Ищем курьера</span>'}</td><td>${badge(o.status)}</td><td class="mono">${o.weight} кг</td><td class="mono" style="color:#d9e7cd">${money(role==='courier'?o.payout:o.fee)}</td><td>${icon('chevron',13)}</td></tr>`;
    }).join('')}</tbody></table>${!orders.length?empty('Пока ничего нет','Попробуйте другой фильтр или создайте первый заказ.'):''}</div>${pagination?tablePagination():`<div class="table-footer"><span>Показаны последние ${orders.length} заказов</span><span class="grow"></span><span>${date(new Date(),{day:'numeric',month:'long',year:'numeric'})}</span></div>`}`;
  }
  function tablePagination(){const total=filteredOrders().length,pages=Math.max(1,Math.ceil(total/8));return `<div class="table-footer"><span>${total?`Показано ${(orderPage-1)*8+1}–${Math.min(orderPage*8,total)} из ${total} заказов`:'0 заказов'}</span><div class="pages">${Array.from({length:pages},(_,i)=>`<button class="page-num ${i+1===orderPage?'active':''}" data-action="order-page" data-id="${i+1}">${i+1}</button>`).join('')}</div></div>`;}
  function ordersPage(){
    const all=S.user.role==='courier'?S.orders.filter(o=>o.courier_id===S.user.id):S.orders;
    const tabs=[['all','Все заказы',all.length],['new','Новые',all.filter(o=>o.status==='new').length],['active','Активные',all.filter(active).length],['delivered','Доставлены',all.filter(o=>o.status==='delivered').length],['cancelled','Отменены',all.filter(o=>o.status==='cancelled').length]];
    const rows=filteredOrders();orderPage=Math.min(orderPage,Math.max(1,Math.ceil(rows.length/8)));
    return heading('Каждый заказ на виду<span class="lime">.</span>','От первой точки маршрута до последнего подтверждения.',S.user.role==='courier'?'':btn('Новый заказ','new-order','plus','primary'))+`<section class="card orders-card"><div class="table-toolbar"><div class="tabs">${tabs.filter(t=>S.user.role!=='courier'||t[0]!=='new').map(([key,label,count])=>`<button class="tab ${orderTab===key?'active':''}" data-action="order-tab" data-id="${key}">${label}<span class="tab-count">${count}</span></button>`).join('')}</div><div class="table-search">${icon('search',12)}<input id="order-search" placeholder="Номер, адрес или содержимое" aria-label="Поиск заказов" value="${esc(orderSearch)}"></div></div><div id="order-table">${orderTable(rows.slice((orderPage-1)*8,orderPage*8))}</div></section>${footer()}`;
  }
  function peoplePage(role){
    let people=S.people.filter(p=>p.role===role&&(!peopleSearch||(p.name+' '+p.email+' '+p.phone).toLowerCase().includes(peopleSearch.toLowerCase())));
    if(peopleFilter==='online')people=people.filter(p=>p.online);if(peopleFilter==='pending')people=people.filter(p=>!p.approved);
    const courier=role==='courier';
    return heading(courier?'Люди, которые двигают город<span class="lime">.</span>':'Ваши люди<span class="lime">.</span>',courier?'Команда, статусы и каждый маршрут в одном месте.':'Клиенты, контакты и история доставок.',btn(courier?'Добавить курьера':'Добавить клиента','new-person','plus','primary',`data-role="${role}"`))+`<div class="search-row"><input id="people-search" placeholder="Поиск по имени, email или телефону" aria-label="Поиск пользователей" value="${esc(peopleSearch)}">${courier?`<div class="chips" style="margin:0"><button class="chip ${peopleFilter==='all'?'active':''}" data-action="people-filter" data-id="all">Вся команда</button><button class="chip ${peopleFilter==='online'?'active':''}" data-action="people-filter" data-id="online">На линии</button><button class="chip ${peopleFilter==='pending'?'active':''}" data-action="people-filter" data-id="pending">Заявки</button></div>`:''}</div><div class="people-grid" id="people-grid">${people.map(p=>personCard(p)).join('')||empty('Никого не нашли','Измените поиск или добавьте нового пользователя.','people')}</div>${footer()}`;
  }
  function personCard(p){
    const orders=S.orders.filter(o=>p.role==='courier'?o.courier_id===p.id:o.client_id===p.id),delivered=orders.filter(o=>o.status==='delivered');
    return `<article class="card person-card"><div class="person-head">${avatar(p.name,p.id)}<div class="grow"><h3>${esc(p.name)}</h3><p>${p.role==='courier'?esc(vehicles[p.vehicle]):'Клиент · с '+date(p.created_at)}</p></div>${p.role==='courier'?`<span class="pill ${!p.approved?'express':p.online?'picked_up':'neutral'}"><span class="dot"></span>${!p.approved?'Заявка':p.online?'На линии':'Офлайн'}</span>`:''}</div><div class="person-details"><div><small>${p.role==='courier'?'Доставлено':'Всего заказов'}</small><b>${p.role==='courier'?delivered.length:orders.length}</b></div><div><small>${p.role==='courier'?'Заработано':'Общая стоимость'}</small><b>${money((p.role==='courier'?delivered:orders.filter(o=>o.status!=='cancelled')).reduce((a,o)=>a+(p.role==='courier'?o.payout:o.fee),0))}</b></div></div><a class="person-contact" href="tel:${esc(p.phone.replace(/[^+\d]/g,''))}">${icon('phone',13)}${esc(p.phone)}</a><a class="person-contact" href="mailto:${esc(p.email)}">${icon('mail',13)}${esc(p.email)}</a><div class="person-bottom">${p.role==='courier'?!p.approved?btn('Подтвердить курьера','approve','check','primary',`data-id="${p.id}"`):btn('Показать на карте','courier-map','map','ghost',`data-id="${p.id}"`):btn('История заказов','client-orders','box','ghost',`data-id="${p.id}"`)}${btn('Написать','support-user','chat','ghost',`data-id="${p.id}"`)}</div></article>`;
  }

  // Every road, block, park and marker below is drawn locally. No map SDK, tiles or map API.
  const project=(lat,lng)=>[(lng-44.4915)/.0445*1000,(40.2005-lat)/.0335*650];
  const unproject=(x,y)=>({lat:40.2005-y/650*.0335,lng:44.4915+x/1000*.0445});
  let mapBaseCache='';
  function mapBase(){
    if(mapBaseCache)return mapBaseCache;
    let blocks='';
    for(let row=-3;row<13;row++)for(let col=-2;col<18;col++){
      const seed=(row+5)*37+(col+4)*19,x=col*62+10,y=row*52+7;
      if((row===4&&col>7&&col<12)||(row>5&&row<9&&col>2&&col<5)||(row<2&&col>8&&col<13))continue;
      const fill=['#20291e','#222c20','#1f281d','#243020'][Math.abs(seed)%4];
      blocks+=`<path d="M${x} ${y}h${44+seed%8}v${33+seed%5}h-${44+seed%8}z" fill="${fill}" stroke="#2c3826" stroke-width=".6"/><path d="M${x+6} ${y+6}h${28+seed%8}v${20+seed%5}h-${28+seed%8}z" fill="none" stroke="#2e3b27" stroke-width=".5"/>`;
    }
    let streets='';
    for(let i=-2;i<19;i++)streets+=`<path d="M${i*62+3} -200V900"/>`;
    for(let i=-3;i<16;i++)streets+=`<path d="M-300 ${i*52+1}H1300"/>`;
    const primary=[
      ['M-80 590 240 437 509 276 733 128 1010 -70',7],
      ['M110 760 370 569 630 394 896 208 1130 60',7],
      ['M101 86 348 231 580 371 826 520 1032 646',6],
      ['M-30 299 174 392 457 550 760 728',6],
      ['M90 -70 213 80 376 183 526 212 669 208 816 275 868 386 857 515 746 652',8],
      ['M-70 107 158 154 278 180 400 218 500 256',9],
      ['M-25 702 88 527 170 371 207 264 240 186 320 55 378 -80',8]
    ];
    const roadPaths=primary.map(([d,w])=>`<path d="${d}" fill="none" stroke="#111c0f" stroke-width="${w+4}"/><path d="${d}" fill="none" stroke="#46533a" stroke-width="${w}"/><path d="${d}" fill="none" stroke="#263720" stroke-width="${Math.max(1,w-3)}"/>`).join('');
    const labels=[['МАШТОЦА',441,301,-32],['АБОВЯНА',744,315,-34],['ТУМАНЯНА',593,391,31],['АМИРЯНА',360,513,29],['БАГРАМЯНА',240,165,16],['ИСААКЯНА',751,225,23],['МОСКОВЯН',604,207,1]];
    mapBaseCache=`<rect width="1000" height="650" fill="#192218"/><g transform="rotate(-32 500 325)">${blocks}<g fill="none" stroke="#33422c" stroke-width="3.5">${streets}</g></g><path d="M0 0H170L178 93 124 198 71 293 26 405 0 439Z" fill="#1e2b1c"/><path d="M90 -20C115 60 128 96 98 159S24 215 38 299 86 375 54 440 6 539-21 599" fill="none" stroke="#36472f" stroke-width="30"/><path d="M90 -20C115 60 128 96 98 159S24 215 38 299 86 375 54 440 6 539-21 599" fill="none" stroke="#253c36" stroke-width="12"/><path d="M486 7 636 37 668 100 591 149 518 117 468 58Z" fill="#2b3d22" stroke="#3b522d"/><path d="M786 262C891 302 896 435 840 501L810 477C856 385 834 336 772 309Z" fill="#2b3b23" stroke="#3b4c2e"/><path d="M376 311 445 268 488 299 422 344Z" fill="#2a3c22" stroke="#3a4d2c"/><path d="M289 509 345 479 373 517 318 552Z" fill="#2b3d23" stroke="#3e522f"/><g opacity=".35" fill="#547244">${Array.from({length:35},(_,i)=>`<circle cx="${495+(i*37)%147}" cy="${30+(i*13)%80}" r="${3+i%4}"/>`).join('')}</g>${roadPaths}<path d="M493 265 627 395" fill="none" stroke="#4a593c" stroke-width="6"/><ellipse cx="641" cy="541" rx="38" ry="28" transform="rotate(-30 641 541)" fill="#25341e" stroke="#5a6748" stroke-width="5"/><circle cx="552" cy="251" r="24" fill="#293b20" stroke="#536a3e" stroke-width="4"/><circle cx="552" cy="251" r="10" fill="#3e5430"/><path d="M565 142l36-20 9 14-37 20z M572 157l36-20 9 14-37 20z M579 172l36-20 9 14-37 20z" fill="#405334" stroke="#60754a"/><g>${labels.map(([t,x,y,r])=>`<text x="${x}" y="${y}" class="street-name" text-anchor="middle" transform="rotate(${r} ${x} ${y})">${t}</text>`).join('')}</g><text x="360" y="100" class="area-name" transform="rotate(-15 360 100)">АРАБКИР</text><text x="475" y="438" class="area-name" transform="rotate(-32 475 438)">КЕНТРОН</text><text x="910" y="510" class="area-name" transform="rotate(-32 910 510)">НОРК</text><text x="550" y="283" class="landmark" text-anchor="middle">Театр оперы</text><text x="629" y="587" class="landmark" text-anchor="middle">Площадь Республики</text><text x="615" y="108" class="landmark">Каскад</text><text x="905" y="347" class="landmark" transform="rotate(63 905 347)">Кольцевой парк</text><text x="50" y="356" class="landmark" transform="rotate(-72 50 356)" opacity=".6">Река Раздан</text>`;
    return mapBaseCache;
  }
  function routeSVG(order){
    if(!order)return '';
    const a=project(order.pickup_lat,order.pickup_lng),b=project(order.dropoff_lat,order.dropoff_lng);
    const c=person(order.courier_id);const pos=c?.lat!=null?project(c.lat,c.lng):null;
    const bend=[(a[0]+b[0])/2+28,(a[1]+b[1])/2-22];
    return `<path d="M${a.join(' ')} L${bend.join(' ')} L${b.join(' ')}" fill="none" stroke="#c5f56b" stroke-width="14" opacity=".06"/><path d="M${a.join(' ')} L${bend.join(' ')} L${b.join(' ')}" fill="none" stroke="#c5f56b" stroke-width="3" stroke-linecap="round" class="route-line"/>${pos?`<path d="M${pos.join(' ')} L${a.join(' ')}" fill="none" stroke="#c5f56b" stroke-width="1.5" opacity=".4" stroke-dasharray="3 6"/>`:''}<g transform="translate(${a.join(' ')})"><circle r="17" fill="#252533" stroke="#b6a3f5" stroke-width="1.5"/><rect x="-5" y="-5" width="10" height="10" rx="2" fill="#b6a3f5"/><text y="33" text-anchor="middle" fill="#c5b9eb" font-size="10" stroke="#192218" stroke-width="4" paint-order="stroke">Забрать</text></g><g transform="translate(${b.join(' ')})"><circle r="20" fill="#c5f56b" opacity=".08"/><path d="M0 10C-4 5-12-2-12-9a12 12 0 1 1 24 0C12-2 4 5 0 10" fill="#c5f56b" stroke="#24351a" stroke-width="2"/><circle cy="-9" r="4" fill="#2b3f1b"/><rect x="-35" y="23" width="70" height="21" rx="5" fill="#c5f56b"/><text y="37" text-anchor="middle" fill="#233817" font-size="9" font-weight="600">${order.id?'#'+orderId(order.id):'Доставить'}</text></g>`;
  }
  function courierMarker(c){
    if(c.lat==null||c.lng==null)return '';
    const [x,y]=project(c.lat,c.lng),hasJob=S?.orders.some(o=>o.courier_id===c.id&&active(o));
    const color=c.online?(hasJob?'#b6a3f5':'#c5f56b'):'#6f7e63';
    return `<g class="marker ${selectedCourier===c.id?'selected':''}" transform="translate(${x} ${y})" data-action="marker" data-id="${c.id}" role="button" tabindex="0" aria-label="${esc(c.name)}"><title>${esc(c.name)} · ${c.online?'На линии':'Офлайн'}</title><circle r="30" fill="${color}" opacity=".055"/><circle r="22" fill="${color}" opacity=".08"/><circle class="marker-ring" r="15" fill="${color}" stroke="#25361c" stroke-width="3"/><g transform="translate(-9 -9) scale(.75)" fill="none" stroke="#233419" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${paths[c.vehicle]||paths.bike}"/></g><path d="m-4 16 4 5 4-5" fill="${color}"/><text x="0" y="37" font-size="9" text-anchor="middle" fill="#bacba9" stroke="#192218" stroke-width="4" paint-order="stroke">${esc(first(c.name))}</text></g>`;
  }
  function mapHTML(id,order=null,opts={}){
    const all=opts.hero?[
      {id:201,name:'Артур',vehicle:'bike',lat:40.1865,lng:44.5136,online:true},
      {id:202,name:'Мариам',vehicle:'car',lat:40.1825,lng:44.517,online:true},
      {id:203,name:'Давид',vehicle:'scooter',lat:40.1932,lng:44.503,online:true},
      {id:204,name:'Нарек',vehicle:'bike',lat:40.1774,lng:44.5075,online:true}
    ]:opts.public?(publicData?.courier?[{...publicData.courier,id:999,online:true}]:[]):S?.people.filter(p=>p.role==='courier')||[];
    const couriers=opts.select?[]:all.filter(c=>S?.user.role==='admin'||opts.hero||opts.public||c.online||c.id===S?.user.id);
    const shown=couriers.filter(c=>c.online).length;
    const selected=selectedCourier&&couriers.find(c=>c.id===selectedCourier);
    const route=opts.hero?{id:42,pickup_lat:40.1828,pickup_lng:44.5169,dropoff_lat:40.1920,dropoff_lng:44.5047}:order;
    return `<div class="map-wrap" data-map="${id}" ${opts.select?'data-select="true"':''}><svg class="city-map" viewBox="0 0 1000 650" preserveAspectRatio="xMidYMid slice" aria-label="Авторская схема центрального Еревана" role="img"><defs><pattern id="grid-${id}" width="60" height="60" patternUnits="userSpaceOnUse"><path d="M60 0H0V60" fill="none" stroke="#435339" stroke-width=".35" opacity=".2"/></pattern></defs><g class="map-viewport">${mapBase()}<rect width="1000" height="650" fill="url(#grid-${id})"/>${routeSVG(route)}${couriers.map(c=>courierMarker(c)).join('')}</g></svg><div class="map-overlay-top"><span class="map-chip">${icon('pin',12)}Ереван, Армения</span>${S?.demo||opts.hero||publicData?.demo?'<span class="map-chip">Демо</span>':''}</div>${!opts.select?`<div class="map-controls"><button data-action="map-zoom" data-zoom="1.25" aria-label="Увеличить">${icon('plus',14)}</button><button data-action="map-zoom" data-zoom="0.8" aria-label="Уменьшить">${icon('minus',14)}</button><button data-action="map-reset" aria-label="Сбросить карту">${icon('locate',14)}</button></div><div class="map-status"><div class="avatar-stack">${couriers.filter(c=>c.online).slice(0,3).map(c=>avatar(c.name,c.id,'small')).join('')}</div><div><b>${opts.public?'Доставка на карте':shown+' '+(shown===1?'курьер на линии':'курьеров на линии')}</b><p>${opts.hero?'Город становится ближе':opts.public?'Обновляется каждые 10 секунд':'Схематичный маршрут · GPS с устройства'}</p></div></div><span class="map-attribution">DROPP MAP · АВТОРСКАЯ СХЕМА</span>`:''}${selected?`<div class="map-tooltip"><div class="top">${avatar(selected.name,selected.id)}<div class="grow"><b>${esc(selected.name)}</b><br><small>${esc(vehicles[selected.vehicle]||'Курьер')}</small></div><button class="icon-btn" data-action="marker-close" aria-label="Закрыть">${icon('close',12)}</button></div><small>${selected.located_at?'GPS: '+date(selected.located_at)+' · '+time(selected.located_at):'Геопозиция ещё не получена'}</small>${selected.phone?`<a class="btn small full" style="margin-top:12px" href="tel:${esc(selected.phone.replace(/[^+\d]/g,''))}">${icon('phone',12)}Связаться</a>`:''}</div>`:''}</div>`;
  }
  function mountMaps(){
    $$('.map-wrap').forEach(wrap=>{
      const svg=$('.city-map',wrap);if(svg.dataset.mounted)return;svg.dataset.mounted='true';const group=$('.map-viewport',svg),key=wrap.dataset.map;
      const view=mapViews.get(key)||{scale:1,x:0,y:0};mapViews.set(key,view);
      const apply=()=>group.setAttribute('transform',`translate(${view.x} ${view.y}) scale(${view.scale})`);apply();
      let start=null,moved=false;
      const point=e=>{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform(svg.getScreenCTM().inverse());};
      svg.addEventListener('pointerdown',e=>{if(e.target.closest('.marker')||wrap.dataset.select)return;const p=point(e);start={x:p.x,y:p.y,vx:view.x,vy:view.y};moved=false;svg.setPointerCapture(e.pointerId);svg.classList.add('dragging');});
      svg.addEventListener('pointermove',e=>{if(!start)return;const p=point(e);if(Math.abs(p.x-start.x)+Math.abs(p.y-start.y)>4)moved=true;view.x=start.vx+p.x-start.x;view.y=start.vy+p.y-start.y;apply();});
      svg.addEventListener('pointerup',e=>{start=null;svg.classList.remove('dragging');if(wrap.dataset.select){const p=point(e);const loc=unproject((p.x-view.x)/view.scale,(p.y-view.y)/view.scale);if(orderDraft){orderDraft[mapPick+'_lat']=+loc.lat.toFixed(6);orderDraft[mapPick+'_lng']=+loc.lng.toFixed(6);const f=$('#order-form');f.elements[mapPick+'_lat'].value=orderDraft[mapPick+'_lat'];f.elements[mapPick+'_lng'].value=orderDraft[mapPick+'_lng'];redrawDraftMap();toast('Точка '+(mapPick==='pickup'?'отправления':'доставки')+' установлена');}}});
      svg.addEventListener('pointercancel',()=>{start=null;svg.classList.remove('dragging');});
      wrap._apply=apply;
    });
  }
  function mapPage(){
    const couriers=S.people.filter(p=>p.role==='courier'),route=S.orders.find(o=>o.courier_id===selectedCourier&&active(o))||S.orders.find(active);
    return heading('Весь город. Один взгляд<span class="lime">.</span>','Авторская схема Еревана. Нажмите на маркер, чтобы увидеть курьера.',S.user.role==='courier'?btn('Передавать GPS','gps','locate','primary'):btn('Новый заказ','new-order','plus','primary'))+`<div class="map-full-grid"><section class="card map-card map-large"><div class="card-head"><h2>Ереван</h2><span class="live"><span class="dot"></span>LIVE</span><div class="right"><span class="muted" style="font-size:9px">Двигайте карту · + / − для масштаба</span></div></div>${mapHTML('full',route)}<div class="map-bottom"><span class="legend"><span class="dot lime"></span>Свободен</span><span class="legend"><span class="dot purple"></span>На заказе</span><span class="last">Схема центра · для обзора доставок</span></div></section><section class="card"><div class="card-head"><h2>${S.user.role==='admin'?'Команда на карте':'На вашем маршруте'}</h2><span class="pill neutral">${couriers.length}</span></div><div class="map-side">${couriers.map(c=>`<button class="courier-list-row" style="width:100%;text-align:left" data-action="marker" data-id="${c.id}">${avatar(c.name,c.id)}<span class="grow"><b>${esc(c.name)}</b><p>${c.located_at?'GPS · '+time(c.located_at):'Нет геопозиции'} · ${esc(vehicles[c.vehicle])}</p></span><span class="online-dot ${c.online?'':'offline'}"></span></button>`).join('')||empty('Курьер ещё не назначен','Он появится здесь после принятия заказа.','bike')}</div></section></div>${footer()}`;
  }
  function routeDetail(o){return `<div class="detail-route"><div class="route-stop"><span class="stop">A</span><div><p>${esc(o.pickup)}</p><small>Точка получения</small></div></div><div class="route-dash"></div><div class="route-stop end"><span class="stop">B</span><div><p>${esc(o.dropoff)}</p><small>Точка доставки</small></div></div></div>`;}
  function jobsPage(){
    const mine=S.orders.filter(o=>o.courier_id===S.user.id&&active(o)),available=S.orders.filter(o=>o.status==='new');
    const today=S.orders.filter(o=>o.courier_id===S.user.id&&o.status==='delivered'&&dayKey(o.delivered_at)===dayKey(new Date()));
    return heading('Твой город. Твой темп<span class="lime">.</span>','Привет, '+esc(first(S.user.name))+'. Поехали делать чей-то день лучше.',btn(watchId!==null?'GPS включён':'Передавать GPS','gps','locate','ghost'))+
      (!S.user.approved?`<div class="notice warning">${icon('shield')}Профиль на проверке. Диспетчер подтвердит вас, и появятся доступные заказы.</div>`:'')+
      `<div class="courier-hero"><div class="grow"><span class="eyebrow">DROPP COURIER</span><h2>${S.user.online?'Город ждёт тебя. Ты на линии.':'Хороший маршрут начинается здесь.'}</h2><p>${S.user.online?'Новые заказы приходят всем курьерам. Кто принял первым — тот доставляет.':'Включи смену, чтобы принимать заказы и получать уведомления.'}</p></div><button class="shift-toggle" data-action="online" ${!S.user.approved?'disabled':''}><span class="switch ${S.user.online?'on':''}"></span><b>${S.user.online?'На линии':'Выйти на линию'}</b></button></div><div class="stats">${stat('Заработано сегодня',fmt(today.reduce((a,o)=>a+o.payout,0)),'֏','По завершённым доставкам','wallet')}${stat('Доставлено сегодня',today.length,'','Ещё один повод гордиться','checkCircle')}${stat('Сейчас в работе',mine.length,'/ 3','Не больше трёх одновременно','box')}${stat('Доступно заказов',available.length,'','Успей забрать подходящий','bolt')}</div>${mine.length?`<div class="section-title"><h2>Твои активные заказы</h2><span class="pill picked_up">${mine.length}</span></div><div class="job-grid">${mine.map(o=>jobCard(o,true)).join('')}</div>`:''}<div class="section-title"><h2>Новые маршруты</h2><span class="pill neutral">${available.length}</span><span class="grow"></span><span class="live"><span class="dot"></span>ОБНОВЛЯЕТСЯ</span></div><div class="job-grid">${available.map(o=>jobCard(o,false)).join('')}</div>${!available.length?`<div class="card">${empty(S.user.approved?'Скоро будет новый маршрут':'Ждём подтверждения','Здесь появятся доступные заказы. Подключите Telegram в профиле, чтобы узнавать о них сразу.','bike')}</div>`:''}${footer()}`;
  }
  function jobCard(o,mine){return `<article class="card job-card"><div class="job-head"><b>#${orderId(o.id)}</b>${o.priority==='express'?express():badge(o.status)}</div>${routeDetail(o)}<div class="job-details"><span>${icon('box',13)} ${esc(o.description)}</span><span>${icon('weight',13)} ${o.weight} кг</span></div>${o.note?`<p class="muted" style="font-size:10px;margin-bottom:17px">${esc(o.note)}</p>`:''}<div class="job-bottom"><span class="pay mono">${money(o.payout)}<small>ТВОЙ ДОХОД</small></span>${mine?btn(o.status==='assigned'?'Подтвердить получение':'Подтвердить доставку','proof','camera','primary',`data-id="${o.id}" data-stage="${o.status==='assigned'?'picked_up':'delivered'}"`):btn('Забрать заказ','claim','arrow','primary',`data-id="${o.id}" ${!S.user.online?'disabled':''}`)}</div>${mine?`<div style="display:flex;gap:12px;margin-top:19px">${btn('Детали заказа','order','box','link',`data-id="${o.id}"`)}${btn('Чат с клиентом','order-chat','chat','link',`data-id="${o.id}"`)}</div>`:''}</article>`;}
  function clientOverview(){
    const current=S.orders.find(active)||S.orders.find(o=>o.status==='new'),done=S.orders.filter(o=>o.status==='delivered');
    return heading('Доставим то, что важно<span class="lime">.</span>','Привет, '+esc(first(S.user.name))+'. Твои доставки всегда рядом.',btn('Отправить посылку','new-order','plus','primary'))+`<div class="stats">${stat('Всего заказов',S.orders.length,'','Вся история в одном месте','box')}${stat('Активные доставки',S.orders.filter(o=>active(o)||o.status==='new').length,'','Следи за ними на карте','map')}${stat('Уже доставлено',done.length,'','С заботой до самой двери','checkCircle')}${stat('Стоимость доставок',fmt(S.orders.filter(o=>o.status!=='cancelled').reduce((a,o)=>a+o.fee,0)),'֏','По всем вашим заказам','wallet')}</div>${current?trackingBlock(current):`<section class="card">${empty('Первый маршрут начинается с тебя','Документы, подарок или кофе для друга. Укажи два адреса — и мы найдём курьера.','box',btn('Создать заказ','new-order','plus','primary'))}</section>`}<section class="card orders-card" style="margin-top:23px"><div class="card-head"><h2>Твои последние доставки</h2><div class="right">${btn('Все заказы','orders','arrow','link')}</div></div>${orderTable(S.orders.slice(0,4),false)}</section>${footer()}`;
  }
  function trackingPage(){
    if(!S.orders.length)return heading('Доставка ближе, чем кажется<span class="lime">.</span>','Карта, статус и связь с курьером — всё здесь.')+`<section class="card">${empty('Пока нет доставок','Создай первый заказ, и он появится на карте.','map',btn('Отправить посылку','new-order','plus','primary'))}</section>`+footer();
    const order=S.orders.find(o=>o.id===selectedOrder)||S.orders.find(active)||S.orders.find(o=>o.status==='new')||S.orders[0];
    return heading('Твоя доставка на карте<span class="lime">.</span>','Следи за маршрутом и оставайся на связи.',`<select class="order-selector" id="tracking-order" aria-label="Заказ для отслеживания">${S.orders.map(o=>`<option value="${o.id}" ${o.id===order.id?'selected':''}>#${orderId(o.id)} · ${statusLabel[o.status]}</option>`).join('')}</select>`)+trackingBlock(order)+footer();
  }
  function trackingBlock(o,isPublic=false){
    const courier=isPublic?publicData?.courier:person(o.courier_id);
    return `<div class="tracking-grid"><section class="card map-card tracking-map"><div class="card-head"><h2>#${orderId(o.id)}</h2>${badge(o.status)}<div class="right"><span class="live"><span class="dot"></span>НА СВЯЗИ</span></div></div>${mapHTML(isPublic?'public':'tracking',o,{public:isPublic})}<div class="map-bottom"><span>${icon('info',11)} Маршрут на схеме ориентировочный</span><span class="last">Геопозиция передаётся курьером</span></div></section><section class="card track-progress"><span class="eyebrow">ТВОЯ ДОСТАВКА</span><div class="track-eta">${o.status==='new'?'Ищем курьера':o.status==='assigned'?'Едет за посылкой':o.status==='picked_up'?'Уже в пути':o.status==='delivered'?'Доставлено':'Отменено'}</div><p>${o.status==='new'?'Курьеры уже видят твой заказ.':o.status==='delivered'?'Посылка на месте. Спасибо, что с нами.':o.status==='cancelled'?'Заказ больше не выполняется.':'Следующий шаг появится здесь.'}</p>${courier?`<div class="track-courier">${avatar(courier.name,courier.id||0)}<div class="grow"><b>${esc(courier.name)}</b><p>${esc(vehicles[courier.vehicle]||'Курьер')} · ${courier.located_at?'GPS '+time(courier.located_at):'GPS ещё не получен'}</p></div>${courier.phone?`<a href="tel:${esc(courier.phone.replace(/[^+\d]/g,''))}" aria-label="Позвонить курьеру">${icon('phone',14)}</a>`:''}</div>`:''}${routeDetail(o)}${!isPublic?`<div style="display:flex;gap:8px;flex-wrap:wrap">${courier?btn('Чат с курьером','order-chat','chat','primary',`data-id="${o.id}"`):btn('Поддержка','support','chat','ghost')}${btn('Поделиться','share','link','ghost',`data-id="${o.id}"`)}</div><div style="margin-top:17px">${btn('История и фото','order','camera','link',`data-id="${o.id}"`)}</div>`:`<div class="history-list">${(publicData.history||[]).map(h=>`<div class="history-event"><span class="dot"></span><div><b>${statusLabel[h.status]}</b></div><small>${time(h.created_at)}</small></div>`).join('')}</div>`}</section></div>`;
  }
  function financePage(){
    const courier=S.user.role==='courier';
    const rows=S.orders.filter(o=>(!courier||o.courier_id===S.user.id)&&new Date(o.created_at)>=new Date(Date.now()-period*86400000));
    const done=rows.filter(o=>o.status==='delivered'),sum=(list,key)=>list.reduce((a,o)=>a+o[key],0);
    const days=Math.min(period,14);const daily=Array.from({length:days},(_,i)=>{const d=Date.now()-(days-i-1)*86400000;const list=done.filter(o=>dayKey(o.delivered_at)===dayKey(d));return {date:d,value:sum(list,courier?'payout':'fee')};});
    const max=Math.max(...daily.map(x=>x.value),1),revenue=sum(done,'fee'),payout=sum(done,'payout'),cancelled=rows.filter(o=>o.status==='cancelled').length;
    const ratio=rows.length?Math.round(done.length/rows.length*100):0;
    return heading(courier?'Каждый маршрут считается<span class="lime">.</span>':'Цифры без лишнего шума<span class="lime">.</span>',courier?'Начисления за завершённые доставки.':'Экономика заказов и начисления курьерам.',`<select class="period" id="finance-period" aria-label="Период"><option value="7" ${period===7?'selected':''}>За 7 дней</option><option value="14" ${period===14?'selected':''}>За 14 дней</option><option value="30" ${period===30?'selected':''}>За 30 дней</option><option value="90" ${period===90?'selected':''}>За 90 дней</option></select><a class="btn ghost" href="/api/export">${icon('download',14)}Экспорт CSV</a>`)+`<div class="stats">${stat(courier?'Начислено курьеру':'Стоимость доставок',fmt(courier?payout:revenue),'֏','За завершённые заказы','wallet')}${stat(courier?'В работе, ожидается':'Начислено курьерам',fmt(courier?sum(rows.filter(active),'payout'):payout),'֏',courier?'После завершения заказов':'За завершённые заказы','bike')}${stat(courier?'Доставлено':'Разница стоимости',courier?done.length:fmt(revenue-payout),courier?'':'֏',courier?'За выбранный период':'До расходов и налогов','checkCircle')}${stat('Средний заказ',fmt(Math.round((courier?payout:revenue)/(done.length||1))),'֏',done.length+' завершённых доставок','box')}</div><div class="chart-grid"><section class="card chart-card"><div class="card-head"><div><h2>${courier?'Начисления по дням':'Стоимость завершённых доставок'}</h2><p>Армянский драм · ${days} последних дней</p></div><div class="right"><span class="pill neutral">AMD</span></div></div><div class="chart">${daily.map(d=>`<div class="bar-col"><span class="bar-value">${d.value?fmt(d.value):''}</span><div class="bar" style="height:${Math.max(1,d.value/max*80)}%" title="${date(d.date)}: ${money(d.value)}"></div><span class="bar-label">${new Date(d.date).toLocaleDateString('ru-RU',{day:'numeric',timeZone:'Asia/Yerevan'})}</span></div>`).join('')}</div><p class="form-help">Отражены суммы по заказам. Банковские переводы и приём платежей в этой версии не подключены.</p></section><section class="card chart-card breakdown"><h2>Доставка в цифрах</h2><div class="donut" style="background:conic-gradient(#c5f56b 0 ${ratio}%,#b6a3f5 ${ratio}% ${rows.length?100-cancelled/rows.length*100:100}%,#5c6b50 0)"><div>${ratio}%<small>ЗАВЕРШЕНО</small></div></div><div class="chart-legend"><span class="dot lime"></span>Доставлены<b>${done.length}</b></div><div class="chart-legend"><span class="dot purple"></span>В работе и новые<b>${rows.length-done.length-cancelled}</b></div><div class="chart-legend"><span class="dot muted"></span>Отменены<b>${cancelled}</b></div></section></div><section class="card orders-card"><div class="card-head"><h2>Завершённые доставки</h2><span class="pill neutral">${done.length}</span></div>${orderTable(done.slice(0,20),false)}</section>${footer()}`;
  }
  function profilePage(){
    const u=S.user;
    return heading('Всё начинается с тебя<span class="lime">.</span>','Твой профиль, транспорт и способы оставаться на связи.',btn('Выйти','logout','logout','ghost'))+`<div class="profile-grid"><section class="card profile-form"><div class="profile-intro">${avatar(u.name,u.id,'large')}<div><h2>${esc(u.name)}</h2><p>${roleLabel[u.role]} · ${esc(u.email)}</p></div></div><form id="profile-form"><div class="form-grid mobile-single">${field('Имя и фамилия','name',u.name,'text','required maxlength="90" autocomplete="name"')}${field('Телефон','phone',u.phone,'tel','required maxlength="24" autocomplete="tel"')}${u.role==='courier'?`<div class="field full"><label for="f-vehicle">Транспорт</label><select name="vehicle" id="f-vehicle">${Object.entries(vehicles).map(([k,v])=>`<option value="${k}" ${u.vehicle===k?'selected':''}>${v}</option>`).join('')}</select></div>`:''}</div><div class="form-error"></div><button type="submit" class="btn primary" style="margin-top:22px">${icon('check',14)}Сохранить профиль</button></form><div class="form-section">${icon('shield',16)}Безопасность</div><form id="password-form"><div class="form-grid mobile-single">${field('Текущий пароль','current','','password','required autocomplete="current-password" maxlength="128"')}${field('Новый пароль','password','','password','required minlength="10" maxlength="128" autocomplete="new-password"')}</div><p class="form-help">Минимум 10 символов. Другие активные сессии завершатся.</p><div class="form-error"></div><button type="submit" class="btn ghost" style="margin-top:17px">Изменить пароль</button></form></section><aside class="profile-aside"><section class="card settings-card"><h2>${icon('telegram')}Всегда на связи</h2><p>Подключи Telegram, чтобы получать статусы${u.role==='courier'?' и принимать новые заказы прямо из бота':''}.</p><div class="row"><span>Telegram-бот</span><span class="pill ${u.telegram_connected?'picked_up':'neutral'}">${u.telegram_connected?'Подключён':S.telegram.configured?'Готов к подключению':'Не настроен'}</span></div>${btn(u.telegram_connected?'Отключить Telegram':'Подключить Telegram',u.telegram_connected?'telegram-unlink':'telegram-link','telegram',u.telegram_connected?'ghost full':'primary full','style="margin-top:17px"')}</section><section class="card settings-card"><h2>${icon('bell')}Уведомления</h2><p>Разреши уведомления в браузере. Они работают, пока кабинет открыт.</p>${btn('Включить уведомления','enable-notifications','bell','ghost full')}</section>${u.role==='courier'?`<section class="card settings-card"><h2>${icon('locate')}Живая геопозиция</h2><p>Передавай GPS во время смены, чтобы клиент видел тебя на карте. Браузер должен оставаться открытым.</p><div class="row"><span>GPS</span><small>${watchId!==null?'Передаётся':'Выключен'}</small></div>${btn(watchId!==null?'Остановить GPS':'Включить GPS','gps','locate','ghost full','style="margin-top:14px"')}</section>`:''}<section class="card settings-card"><h2>${icon('chat')}Мы рядом</h2><p>Вопрос о доставке? Напиши диспетчеру в поддержку.</p>${btn('Написать в поддержку','support','arrow','ghost full')}</section></aside></div>${footer()}`;
  }
  function settingsPage(){return heading('Диспетчерская под тебя<span class="lime">.</span>','Контакты поддержки и состояние сервиса.')+`<div class="profile-grid"><section class="card profile-form"><h2 style="margin-bottom:25px">Контакты сервиса</h2><form id="settings-form">${field('Телефон поддержки','support',S.settings.support,'tel','required maxlength="24"')}<div class="form-error"></div><button type="submit" class="btn primary" style="margin-top:20px">${icon('check',14)}Сохранить</button></form><div class="form-section">${icon('shield',16)}Аккаунт администратора</div><p class="muted" style="font-size:12px;margin-bottom:20px">Измени личные данные и пароль в своём профиле.</p>${btn('Открыть профиль','profile','arrow','ghost')}</section><aside><section class="card settings-card"><h2>${icon('telegram')}Telegram</h2><div class="row"><span>Бот</span><span class="pill ${S.telegram.configured?'picked_up':'neutral'}">${S.telegram.configured?'Настроен':'Ожидает настройки'}</span></div><div class="row"><span>Имя бота</span><small>${S.telegram.username?'@'+esc(S.telegram.username):'—'}</small></div><div class="row"><span>Уведомлений в очереди</span><small>${S.telegram.pending||0}</small></div><p style="margin-top:17px;margin-bottom:0">Настройка токена описана в инструкции проекта. Секреты хранятся на сервере.</p></section><section class="card settings-card"><h2>${icon('map')}DROPP Map</h2><p>Авторская векторная схема центрального Еревана. Без ключей и внешних картографических сервисов.</p><div class="row"><span>Режим</span><small>Офлайн-схема</small></div><div class="row"><span>Положение курьеров</span><small>GPS с устройства</small></div></section></aside></div>${footer()}`;}
  function threadName(t){if(t.startsWith('support:')){const p=person(Number(t.slice(8)));return S.user.role==='admin'?(p?.name||'Пользователь'):'Поддержка DROPP';}return '#'+orderId(Number(t.slice(6)));}
  function threadOptions(){
    const result=new Set(S.threads.map(t=>t.thread));
    if(S.user.role==='admin')S.people.filter(p=>p.role!=='admin').forEach(p=>result.add('support:'+p.id));else result.add('support:'+S.user.id);
    S.orders.filter(o=>o.courier_id&&(S.user.role==='admin'||o.client_id===S.user.id||o.courier_id===S.user.id)).slice(0,40).forEach(o=>result.add('order:'+o.id));
    return [...result];
  }
  function chatPage(){
    const options=threadOptions();if(!thread||!options.includes(thread))thread=options[0]||null;
    return heading('Хорошая доставка — это диалог<span class="lime">.</span>','Курьеры, клиенты и поддержка. Всегда на одной волне.')+`<section class="card chat-shell"><aside class="chat-sidebar"><h3>Диалоги · ${options.length}</h3>${options.map(t=>`<button class="chat-thread ${t===thread?'active':''}" data-action="thread" data-id="${t}">${t.startsWith('support:')?avatar(threadName(t),Number(t.slice(8))):`<span class="avatar violet">${icon('box',15)}</span>`}<span class="grow"><b>${esc(threadName(t))}</b><p>${t.startsWith('support:')?'Поддержка и вопросы':'Диалог по заказу'}</p></span></button>`).join('')}</aside><div class="chat-main">${thread?`<header class="chat-header"><span class="avatar square">${icon(thread.startsWith('support:')?'chat':'box',16)}</span><div class="grow"><b>${esc(threadName(thread))}</b><p>${thread.startsWith('support:')?'Диспетчер ответит в этом чате':'Участники: клиент, курьер и диспетчер'}</p></div>${thread.startsWith('order:')?`<button class="icon-btn" data-action="order" data-id="${Number(thread.slice(6))}" aria-label="Детали заказа">${icon('info')}</button>`:''}</header><div class="chat-messages" id="chat-messages" aria-live="polite"><div class="chat-empty">Загружаем сообщения…</div></div><form id="chat-form" class="chat-compose"><input name="body" id="chat-input" placeholder="Написать сообщение…" maxlength="2000" autocomplete="off" required aria-label="Сообщение"><button type="submit" class="btn primary" aria-label="Отправить">${icon('send',16)}</button></form>`:'<div class="chat-empty">Выберите диалог</div>'}</div></section>${footer()}`;
  }
  let messageThread=null,messageRows=[],messageHasMore=false;
  async function loadMessages(before=0){
    if(!thread||!$('#chat-messages'))return;const requestedThread=thread;
    try{
      const result=await api('/messages?thread='+encodeURIComponent(thread)+(before?'&before='+before:''));
      if(thread!==requestedThread||!$('#chat-messages'))return;
      messageRows=before&&messageThread===thread?[...result.messages,...messageRows]:result.messages;messageThread=thread;messageHasMore=result.hasMore;
      const box=$('#chat-messages'),nearEnd=box.scrollHeight-box.scrollTop-box.clientHeight<80,oldHeight=box.scrollHeight;
      box.innerHTML=`${messageHasMore?btn('Более ранние сообщения','more-messages','','ghost small chat-more'):''}${messageRows.map(m=>`<article class="message ${m.user_id===S.user.id?'mine':''}"><div class="message-name">${esc(m.name)}${m.role==='admin'?' · диспетчер':''}</div><div class="message-bubble">${esc(m.body)}</div><div class="message-time">${date(m.created_at)} · ${time(m.created_at)}</div></article>`).join('')||'<div class="chat-empty">Начните разговор. Мы рядом.</div>'}`;
      if(before)box.scrollTop=box.scrollHeight-oldHeight;else if(nearEnd||!before)box.scrollTop=box.scrollHeight;
    }catch(e){const box=$('#chat-messages');if(box)box.innerHTML=`<div class="chat-empty">${esc(e.message)}</div>`;}
  }
  function modal(title,sub,body,foot='',className=''){
    closeModal();modalCloseFocus=document.activeElement;
    overlay.innerHTML=`<div class="modal-backdrop"><section class="modal ${className}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1"><header class="modal-header"><div class="grow"><h2 id="modal-title">${title}</h2>${sub?`<p>${sub}</p>`:''}</div><button class="icon-btn" data-action="close-modal" aria-label="Закрыть">${icon('close')}</button></header><div class="modal-body">${body}</div>${foot?`<footer class="modal-footer">${foot}</footer>`:''}</section></div>`;
    document.body.style.overflow='hidden';setTimeout(()=>{const el=$('input:not([type=hidden]),select,textarea,.modal',overlay);el?.focus({preventScroll:true});},60);mountMaps();
  }
  let cameraStream=null,proofPhoto='',proofOrder=null,proofStage=null;
  function closeModal(){if(cameraStream){cameraStream.getTracks().forEach(t=>t.stop());cameraStream=null;}overlay.innerHTML='';document.body.style.overflow='';modalCloseFocus?.focus?.({preventScroll:true});}
  function confirmModal(title,description,action,attrs='',danger=false){modal(title,'',`<p class="confirm-text">${description}</p>`,btn('Отмена','close-modal','','ghost')+btn('Подтвердить',action,'check',danger?'danger':'primary',attrs),'narrow');}
  function newPerson(role){modal(role==='courier'?'Новый человек в команде':'Новый клиент',role==='courier'?'Доступ к заказам, карте и Telegram-уведомлениям.':'Все доставки и контакты в одном месте.',`<form id="person-form"><input type="hidden" name="role" value="${role}"><div class="form-grid mobile-single">${field('Имя и фамилия / название','name','','text','required maxlength="90" autocomplete="name"')}${field('Email для входа','email','','email','required maxlength="180" autocomplete="email"')}${field('Телефон','phone','+374 ','tel','required maxlength="24" autocomplete="tel"')}${field('Временный пароль','password','','password','required minlength="10" maxlength="128" autocomplete="new-password"')}${role==='courier'?`<div class="field full"><label for="f-vehicle">Транспорт</label><select name="vehicle" id="f-vehicle">${Object.entries(vehicles).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div>`:''}</div><p class="form-help">Пароль — минимум 10 символов. Передайте данные человеку лично; он сможет сменить пароль в профиле.</p><div class="form-error"></div></form>`,btn('Отмена','close-modal','','ghost')+`<button class="btn primary" type="submit" form="person-form">${icon('plus',14)}Создать аккаунт</button>`);}
  function newOrder(){
    const clients=S.people.filter(p=>p.role==='client');
    if(S.user.role==='admin'&&!clients.length){toast('Сначала добавьте клиента.');newPerson('client');return;}
    orderDraft={pickup_lat:40.1828,pickup_lng:44.5169,dropoff_lat:40.1920,dropoff_lng:44.5047};mapPick='pickup';
    modal('Два адреса. Один хороший маршрут.','Новый заказ · Ереван',`<form id="order-form"><input type="hidden" name="request_key" value="${crypto.randomUUID()}">${Object.entries(orderDraft).map(([k,v])=>`<input type="hidden" name="${k}" value="${v}">`).join('')}<div class="form-section"><span>01</span>Маршрут</div><div class="form-grid mobile-single">${S.user.role==='admin'?`<div class="field full"><label for="f-client_id">Клиент</label><select name="client_id" id="f-client_id" required>${clients.map(c=>`<option value="${c.id}">${esc(c.name)} · ${esc(c.phone)}</option>`).join('')}</select></div>`:''}${field('Откуда забрать','pickup','','text','required maxlength="200" placeholder="Улица, дом, подъезд"')}${field('Куда доставить','dropoff','','text','required maxlength="200" placeholder="Улица, дом, квартира"')}</div><div class="address-choices"><button type="button" data-action="landmark" data-side="pickup" data-place="0">A · Площадь Республики</button><button type="button" data-action="landmark" data-side="pickup" data-place="1">A · Театр оперы</button><button type="button" data-action="landmark" data-side="dropoff" data-place="2">B · Каскад</button><button type="button" data-action="landmark" data-side="dropoff" data-place="3">B · пр. Баграмяна</button></div><p class="form-help">Адрес впишите вручную, точки A и B поставьте на схеме. Поиска адресов через сторонние сервисы нет.</p><div class="chips" style="margin:12px 0 0"><button class="chip active" type="button" data-action="pick-side" data-id="pickup">Поставить A · получение</button><button class="chip" type="button" data-action="pick-side" data-id="dropoff">Поставить B · доставка</button></div><div class="map-select" id="draft-map">${mapHTML('draft',{...orderDraft,id:0},{select:true})}</div><div class="form-section"><span>02</span>Посылка и получатель</div><div class="form-grid mobile-single">${field('Что доставляем','description','','text','required maxlength="400" placeholder="Например, букет цветов или документы"',true)}${field('Имя получателя','recipient','','text','required maxlength="90"')}${field('Телефон получателя','phone','+374 ','tel','required maxlength="24"')}${field('Вес, кг','weight','1','number','required min="0.01" max="50" step="0.01"')}<div class="field"><label for="f-priority">Тип доставки</label><select name="priority" id="f-priority"><option value="normal">Стандарт</option><option value="express">Экспресс · приоритет</option></select></div><div class="field full"><label for="f-note">Комментарий</label><textarea id="f-note" name="note" maxlength="1000" placeholder="Код домофона, этаж, особенности доставки"></textarea></div></div><div class="form-section"><span>03</span>Стоимость</div>${S.user.role==='admin'?`<div class="form-grid">${field('Стоимость доставки, ֏','fee','1300','number','required min="100" max="1000000" step="1"')}${field('Начисление курьеру, ֏','payout','950','number','required min="50" max="1000000" step="1"')}</div><p class="form-help">Суммы можно изменить. Начисление курьеру не может быть выше стоимости доставки.</p>`:''}<div class="order-summary"><div><small>ОПЛАТА ПРИ ПОЛУЧЕНИИ</small><b id="quote-total">1 300 ֏</b></div><span class="muted" style="font-size:10px">${S.user.role==='admin'?'Курьеру: <span id="quote-payout">950 ֏</span>':'Итоговая стоимость доставки'}</span></div><div class="form-error"></div></form>`,`<small>Заказ увидят все курьеры на линии</small>`+btn('Отмена','close-modal','','ghost')+`<button type="submit" form="order-form" class="btn primary">Создать заказ ${icon('arrow',14)}</button>`,'wide');
    updateQuote(true);
  }
  const landmarks=[['Площадь Республики',40.1778,44.5125],['Театр оперы, ул. Туманяна',40.1855,44.5150],['Каскад, ул. Таманяна',40.1918,44.5156],['пр. Баграмяна, 15',40.1920,44.5047]];
  function redrawDraftMap(){const el=$('#draft-map');if(el){el.innerHTML=mapHTML('draft',{...orderDraft,id:0},{select:true});mountMaps();updateQuote(true);}}
  function localQuote(b){const rad=Math.PI/180,a=Math.sin((b.dropoff_lat-b.pickup_lat)*rad/2)**2+Math.cos(b.pickup_lat*rad)*Math.cos(b.dropoff_lat*rad)*Math.sin((b.dropoff_lng-b.pickup_lng)*rad/2)**2,km=6371*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a))*1.35;const fee=Math.ceil((800+km*160+Number(b.weight||1)*50)*(b.priority==='express'?1.4:1)/50)*50;return {fee,payout:Math.round(fee*.72/50)*50};}
  function updateQuote(recalculate=false){const f=$('#order-form');if(!f)return;const b=formObject(f);const quote=localQuote(b);if(S.user.role==='admin'&&recalculate){f.elements.fee.value=quote.fee;f.elements.payout.value=quote.payout;}$('#quote-total').textContent=money(S.user.role==='admin'?Number(f.elements.fee.value):quote.fee);if($('#quote-payout'))$('#quote-payout').textContent=money(Number(f.elements.payout.value));}
  async function openOrder(id){
    const data=await api('/orders/'+id),o=data.order,courier=person(o.courier_id),client=person(o.client_id),mine=S.user.role==='courier'&&o.courier_id===S.user.id;
    let actions=btn('Закрыть','close-modal','','ghost');
    if(S.user.role==='admin'&&o.status==='new')actions+=btn('Назначить курьера','assign','bike','primary',`data-id="${o.id}"`);
    if(mine&&active(o))actions+=btn(o.status==='assigned'?'Получение':'Доставка','proof','camera','primary',`data-id="${o.id}" data-stage="${o.status==='assigned'?'picked_up':'delivered'}"`);
    if((S.user.role==='admin'&&['new','assigned','picked_up'].includes(o.status))||(S.user.role==='client'&&o.status==='new'))actions=btn('Отменить','cancel-order','close','danger',`data-id="${o.id}"`)+actions;
    modal('#'+orderId(o.id),'Создан '+date(o.created_at)+' в '+time(o.created_at),`<div style="display:flex;align-items:center;gap:8px">${badge(o.status)}${o.priority==='express'?express():''}<span class="grow"></span>${btn('Ссылка на доставку','share','link','link',`data-id="${o.id}"`)}</div>${routeDetail(o)}<div class="detail-grid"><div><small>Содержимое</small><b>${esc(o.description)}</b></div><div><small>Вес посылки</small><b>${o.weight} кг</b></div><div><small>Стоимость</small><b>${money(o.fee)}</b></div><div><small>Курьеру</small><b class="lime">${money(o.payout)}</b></div><div><small>Получатель</small><b>${esc(o.recipient)}</b><p style="margin-top:5px;font-size:10px"><a class="lime" href="tel:${esc(o.phone.replace(/[^+\d]/g,''))}">${esc(o.phone)}</a></p></div><div><small>Курьер</small><b>${esc(courier?.name||'Ещё не назначен')}</b></div></div>${o.note?`<div class="notice">${icon('chat',15)}${esc(o.note)}</div>`:''}${o.courier_id?btn('Открыть чат по заказу','order-chat','chat','ghost full',`data-id="${o.id}"`):''}<div class="form-section">История маршрута</div><div class="history-list">${data.history.map(h=>`<div class="history-event"><span class="dot"></span><div><b>${statusLabel[h.status]}</b><p>${esc(h.comment)}${h.name?' · '+esc(first(h.name)):''}</p></div><small>${date(h.created_at)}<br>${time(h.created_at)}</small></div>`).join('')}</div>${data.photos.length?`<div class="form-section">${icon('camera',16)}Подтверждения</div><div class="photo-grid">${data.photos.map(p=>`<a class="proof-photo" href="/api/photos/${p.id}" target="_blank" rel="noopener"><img src="/api/photos/${p.id}" alt="Фото: ${p.stage==='picked_up'?'получение':'доставка'} заказа" loading="lazy"><p>${p.stage==='picked_up'?'Получение':'Доставка'} · ${esc(p.comment)}</p></a>`).join('')}</div>`:''}`,actions,'wide');
  }
  function proofModal(id,stage){
    proofOrder=id;proofStage=stage;proofPhoto='';
    modal(stage==='picked_up'?'Посылка у тебя?':'Ещё одна хорошая доставка.','Фото и комментарий подтвердят '+(stage==='picked_up'?'получение':'доставку')+' заказа #'+orderId(id),`<form id="proof-form"><div id="proof-preview" class="proof-uploader">${icon('camera',32)}<b>Покажи, что всё в порядке</b><small>Сфотографируй посылку и место передачи</small><div style="display:flex;gap:8px">${btn('Открыть камеру','camera-start','camera','primary small')}<label class="btn ghost small" style="margin:0;cursor:pointer" for="proof-file">Выбрать фото</label><input id="proof-file" type="file" accept="image/jpeg,image/png,image/webp" capture="environment" style="display:none"></div></div><div id="camera-panel" style="display:none;margin-top:16px"><video id="camera-video" autoplay playsinline muted style="width:100%;max-height:300px;border-radius:10px"></video>${btn('Сделать снимок','camera-capture','camera','primary full','style="margin-top:12px"')}</div><div class="field" style="margin-top:20px"><label for="proof-comment">Комментарий к подтверждению</label><textarea name="comment" id="proof-comment" required maxlength="800" placeholder="Например: передал получателю лично, упаковка целая"></textarea></div><p class="form-help">Фото до 4 МБ. Камера доступна через HTTPS или на localhost.</p><div class="form-error"></div></form>`,btn('Отмена','close-modal','','ghost')+`<button class="btn primary" type="submit" form="proof-form">${icon('check',14)}Подтвердить ${stage==='picked_up'?'получение':'доставку'}</button>`);
  }
  async function compressPhoto(file){if(file.size>16*1024*1024)throw new Error('Выберите фото меньше 16 МБ.');if(!/^image\/(jpeg|png|webp)$/.test(file.type))throw new Error('Нужна фотография JPEG, PNG или WebP.');const bitmap=await createImageBitmap(file);const ratio=Math.min(1,1600/Math.max(bitmap.width,bitmap.height));const canvas=document.createElement('canvas');canvas.width=Math.round(bitmap.width*ratio);canvas.height=Math.round(bitmap.height*ratio);const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();return canvas.toDataURL('image/jpeg',.82);}
  function showProofPhoto(){const preview=$('#proof-preview');if(preview)preview.innerHTML=`<img src="${proofPhoto}" alt="Фото подтверждения"><span style="font-size:10px">${icon('check',13)} Фото готово к отправке</span>${btn('Переснять','proof-reset','camera','ghost small')}`;if($('#camera-panel'))$('#camera-panel').style.display='none';if(cameraStream){cameraStream.getTracks().forEach(t=>t.stop());cameraStream=null;}}
  function renderWelcome(){
    app.innerHTML=`<main class="welcome"><header class="welcome-nav"><a href="/welcome" aria-label="DROPP, главная">${brand()}</a><nav><a href="#how">Как это работает</a><a href="/login">Для бизнеса</a><a href="/register?role=courier">Стать курьером</a></nav><a href="/login" class="btn ghost">Войти ${icon('arrow',13)}</a></header><section class="welcome-hero"><div class="welcome-copy"><div class="eyebrow"><span class="dot"></span>СДЕЛАНО ДЛЯ ЕРЕВАНА</div><h1>Маленький город.<br>Большое<br><span>движение.</span></h1><p>Кофе для друга. Важные документы. Подарок без повода. Мы соединяем людей, а не просто адреса.</p><div class="welcome-ctas">${config.demo?btn('Открыть диспетчерскую','enter-demo','arrow','primary large','data-role="admin"'):'<a href="/register" class="btn primary large">Отправить посылку '+icon('arrow',16)+'</a>'}<a href="/register?role=courier" class="btn ghost large">Я курьер ${icon('bike',16)}</a></div><div class="hero-note"><div class="avatar-stack">${avatar('Артур Мкртчян',2,'small')}${avatar('Мариам Геворгян',3,'small')}${avatar('Давид Акопян',4,'small')}</div><span>Местные люди. Знакомые улицы. Твоя доставка.</span></div></div><div class="hero-map"><div class="hero-label">${icon('checkCircle',17)}Каждый маршрут под контролем</div>${mapHTML('hero',null,{hero:true})}</div></section><section class="welcome-strip" id="how"><div class="welcome-stat"><strong>01 → 02</strong><p>Два адреса — и доставка уже начинается</p></div><div class="welcome-stat"><strong>На одной карте</strong><p>Заказ, курьер и твой спокойный день</p></div><div class="welcome-stat"><strong>До самой двери</strong><p>Фото подтверждает каждую доставку</p></div></section><section class="welcome-features"><span class="eyebrow">ОДИН ГОРОД · ТРИ ВОЗМОЖНОСТИ</span><h2>Каждому — свой хороший маршрут.</h2><div class="feature-grid"><article class="feature">${icon('overview',27)}<h3>Держи город в руках.</h3><p>Диспетчерская с картой, командой и заказами. Всё, что нужно, чтобы доставка работала в твоём ритме.</p>${config.demo?btn('Зайти как администратор','enter-demo','arrow','link','data-role="admin"'):'<a class="btn link" href="/login">Войти в диспетчерскую →</a>'}</article><article class="feature">${icon('bike',27)}<h3>Двигайся. Доставляй.</h3><p>Выбирай заказы, зарабатывай на маршрутах и оставайся на связи через сайт или Telegram.</p>${config.demo?btn('Посмотреть кабинет курьера','enter-demo','arrow','link','data-role="courier"'):'<a class="btn link" href="/register?role=courier">Присоединиться к команде →</a>'}</article><article class="feature">${icon('box',27)}<h3>Просто отправляй.</h3><p>Создай заказ за минуту, следи за курьером и напиши ему, если хочется что-то уточнить.</p>${config.demo?btn('Попробовать как клиент','enter-demo','arrow','link','data-role="client"'):'<a class="btn link" href="/register">Создать аккаунт →</a>'}</article></div></section><footer class="welcome-footer"><span>© ${new Date().getFullYear()} DROPP. Ереван в движении.</span><span>40.1872° N · 44.5152° E</span><span>С заботой. До двери. ${icon('leaf',12)}</span></footer></main>`;
    mountMaps();
  }
  function renderAuth(register=location.pathname==='/register'){
    const courier=new URLSearchParams(location.search).get('role')==='courier';
    app.innerHTML=`<main class="auth-page"><section class="auth-story"><a href="/welcome">${brand()}</a><div class="auth-visual"><span class="eyebrow">YEREVAN, IN MOTION.</span><h1>Хорошая доставка<br>начинается<br><span>с хороших людей.</span></h1><p>Один город. Тысячи историй. И маленькие посылки, которые делают нас ближе.</p></div><div class="auth-orbit"></div><div class="auth-bottom">40.1872° N · 44.5152° E &nbsp; СДЕЛАНО ДЛЯ ЕРЕВАНА</div></section><section class="auth-form-wrap"><div class="auth-form"><h2>${register?'Начнём движение.':'С возвращением.'}</h2><p>${register?'Пара деталей — и город станет ближе.':'Войди, чтобы продолжить свой маршрут.'}</p><form id="auth-form" data-register="${register}">${register?`<div class="auth-role-choice"><label><input type="radio" name="role" value="client" ${!courier?'checked':''}>Я клиент</label><label><input type="radio" name="role" value="courier" ${courier?'checked':''}>Я курьер</label></div>${field('Имя и фамилия','name','','text','required maxlength="90" autocomplete="name"')}${field('Телефон','phone','','tel','required maxlength="24" placeholder="+374 91 123 456" autocomplete="tel"')}`:''}${field('Email','email','','email','required maxlength="180" placeholder="you@example.com" autocomplete="email"')}${field('Пароль','password','','password',`required ${register?'minlength="10"':''} maxlength="128" autocomplete="${register?'new-password':'current-password'}" placeholder="${register?'Минимум 10 символов':'Твой пароль'}"`)}<div class="form-error"></div><button type="submit" class="btn primary full">${register?'Создать аккаунт':'Войти в DROPP'} ${icon('arrow',15)}</button></form>${register?'<p class="form-help">Аккаунт курьера подтверждает диспетчер.<br>Администраторов создают только при настройке сервера.</p>':''}<p class="auth-switch">${register?'Уже с нами? <a href="/login">Войти</a>':'Пока нет аккаунта? <a href="/register">Присоединиться</a>'}</p>${config?.demo?`<div class="auth-demo"><p>ЗАГЛЯНУТЬ ВНУТРЬ БЕЗ РЕГИСТРАЦИИ</p><div class="demo-roles">${btn('Администратор','enter-demo','','ghost','data-role="admin"')}${btn('Курьер','enter-demo','','ghost','data-role="courier"')}${btn('Клиент','enter-demo','','ghost','data-role="client"')}</div></div>`:''}</div></section></main>`;
  }
  async function enterApp(){
    authEpoch++;stopGPS();closeModal();orderTab='all';orderSearch='';orderPage=1;peopleSearch='';peopleFilter='all';selectedOrder=null;selectedCourier=null;thread=null;mapViews.clear();S=await api('/me');history.replaceState(null,'','/'+(S.user.role==='courier'?'#jobs':''));renderApp();connectEvents();
  }
  async function demoLogin(role){const result=await api('/auth/demo','POST',{role});S={csrf:result.csrf};await enterApp();}
  async function publicTracking(){
    const token=location.pathname.slice(7);
    try{
      publicData=await api('/track/'+token);
      app.innerHTML=`<main class="public-track"><a href="/welcome" aria-label="Главная DROPP">${brand()}</a>${heading('Посылка уже ближе<span class="lime">.</span>','Доставка #'+orderId(publicData.order.id)+' · '+date(publicData.order.created_at))}${publicData.demo?'<div class="notice">Демонстрационная доставка. Геопозиция иллюстративная.</div>':''}${trackingBlock(publicData.order,true)}<div class="footer"><span>С заботой. До двери.</span><a href="/welcome">DROPP · YEREVAN</a></div></main>`;mountMaps();
    }catch(e){app.innerHTML=`<main class="public-track"><a href="/welcome">${brand()}</a><section class="card">${empty('Не нашли эту доставку','Проверьте ссылку или попросите отправителя поделиться ей ещё раз.','map','<a class="btn primary" href="/welcome">На главную</a>')}</section></main>`;}
  }
  async function shareOrder(id){
    const o=S.orders.find(o=>o.id===id);if(!o?.track_token)throw new Error('Ссылка пока недоступна.');
    const url=location.origin+'/track/'+o.track_token;
    try{await navigator.clipboard.writeText(url);toast('Ссылка на доставку скопирована');}
    catch{modal('Доставка по одной ссылке','Отправь её получателю, чтобы он видел статус и курьера.',`<input id="share-url" value="${esc(url)}" readonly aria-label="Ссылка на отслеживание"><p class="form-help">Ссылка открывается без входа. Поделитесь ей только с получателем.</p>`,btn('Выделить ссылку','select-share','link','primary'),'narrow');}
  }
  function navigate(key){closeModal();if(location.hash==='#'+key)renderApp();else location.hash=key;}
  function stopGPS(){if(watchId!==null){navigator.geolocation.clearWatch(watchId);watchId=null;}}
  async function toggleGPS(){
    if(watchId!==null){stopGPS();toast('Передача геопозиции остановлена');renderApp();return;}
    if(!S.user.online)throw new Error('Сначала выйдите на линию.');
    if(!navigator.geolocation)throw new Error('Браузер не поддерживает геопозицию.');
    if(!window.isSecureContext)throw new Error('Для GPS откройте сайт через HTTPS.');
    let last=0;
    watchId=navigator.geolocation.watchPosition(async position=>{
      if(Date.now()-last<6000)return;last=Date.now();
      try{await api('/courier/location','POST',{lat:position.coords.latitude,lng:position.coords.longitude});}
      catch(e){stopGPS();toast(e.message,true);}
    },error=>{stopGPS();toast(error.code===1?'Доступ к геопозиции не разрешён. Измените разрешение сайта в браузере.':'Не удалось получить GPS. Попробуйте у окна или на улице.',true);renderApp();},{enableHighAccuracy:true,maximumAge:5000,timeout:20000});
    toast('GPS включён. Оставьте кабинет открытым во время доставки.');renderApp();
  }
  async function handleAction(el){
    const action=el.dataset.action,id=Number(el.dataset.id);
    if(['overview','orders','map','profile','settings','jobs','tracking','chat','finance'].includes(action)){navigate(action);return;}
    if(action==='close-modal'){closeModal();return;}
    if(action==='menu'){$('#sidebar')?.classList.toggle('open');return;}
    if(action==='enter-demo'){await busy(el,()=>demoLogin(el.dataset.role));return;}
    if(action==='new-order'){newOrder();return;}
    if(action==='new-person'){newPerson(el.dataset.role);return;}
    if(action==='order'){await busy(el,()=>openOrder(id));return;}
    if(action==='order-tab'){orderTab=el.dataset.id;orderPage=1;renderApp();return;}
    if(action==='order-page'){orderPage=id;renderApp();return;}
    if(action==='people-filter'){peopleFilter=el.dataset.id;renderApp();return;}
    if(action==='courier-map'){selectedCourier=id;navigate('map');return;}
    if(action==='client-orders'){orderSearch=person(id)?.name||'';orderTab='all';orderPage=1;navigate('orders');return;}
    if(action==='marker'){
      if(!S&&!publicData)return;selectedCourier=id;
      const c=person(id);if(c?.lat!=null){const view=mapViews.get(page==='map'?'full':'overview')||{scale:1,x:0,y:0};const [x,y]=project(c.lat,c.lng);view.scale=1.35;view.x=500-x*view.scale;view.y=325-y*view.scale;mapViews.set(page==='map'?'full':'overview',view);}
      if(S)renderApp();return;
    }
    if(action==='marker-close'){selectedCourier=null;renderApp();return;}
    if(action==='map-zoom'||action==='map-reset'){
      const wrap=el.closest('.map-wrap'),view=mapViews.get(wrap.dataset.map);if(!view)return;
      if(action==='map-reset'){view.x=0;view.y=0;view.scale=1;}
      else{const next=Math.max(.8,Math.min(4,view.scale*Number(el.dataset.zoom))),ratio=next/view.scale;view.x=500-(500-view.x)*ratio;view.y=325-(325-view.y)*ratio;view.scale=next;}wrap._apply();return;
    }
    if(action==='pick-side'){mapPick=el.dataset.id;$$('[data-action="pick-side"]',overlay).forEach(b=>b.classList.toggle('active',b===el));return;}
    if(action==='landmark'){
      const place=landmarks[Number(el.dataset.place)],side=el.dataset.side,f=$('#order-form');orderDraft[side+'_lat']=place[1];orderDraft[side+'_lng']=place[2];f.elements[side].value=place[0];f.elements[side+'_lat'].value=place[1];f.elements[side+'_lng'].value=place[2];redrawDraftMap();return;
    }
    if(action==='claim'){await busy(el,async()=>{await api('/orders/'+id+'/claim','POST',{});toast('Заказ твой. Хорошего маршрута!');await refresh(true);});return;}
    if(action==='approve'){await busy(el,async()=>{await api('/users/'+id+'/approve','POST',{});toast('Курьер подтверждён');await refresh(true);});return;}
    if(action==='online'){await busy(el,async()=>{const online=!S.user.online;await api('/courier/online','POST',{online});if(!online)stopGPS();toast(online?'Ты на линии. Можно забирать заказы.':'Смена завершена. До новых маршрутов.');await refresh(true);});return;}
    if(action==='gps'){await busy(el,toggleGPS);return;}
    if(action==='proof'){proofModal(id,el.dataset.stage);return;}
    if(action==='proof-reset'){proofModal(proofOrder,proofStage);return;}
    if(action==='camera-start'){
      await busy(el,async()=>{if(!navigator.mediaDevices?.getUserMedia)throw new Error('Камера недоступна. Используйте выбор фото или откройте сайт через HTTPS.');try{cameraStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1600}},audio:false});}catch{throw new Error('Разрешите доступ к камере или выберите готовое фото.');}$('#camera-panel').style.display='block';$('#camera-video').srcObject=cameraStream;await $('#camera-video').play();});return;
    }
    if(action==='camera-capture'){const video=$('#camera-video');if(!video?.videoWidth)throw new Error('Камера ещё запускается. Попробуйте через секунду.');const canvas=document.createElement('canvas'),ratio=Math.min(1,1600/video.videoWidth);canvas.width=Math.round(video.videoWidth*ratio);canvas.height=Math.round(video.videoHeight*ratio);canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);proofPhoto=canvas.toDataURL('image/jpeg',.82);showProofPhoto();return;}
    if(action==='share'){await busy(el,()=>shareOrder(id));return;}
    if(action==='select-share'){$('#share-url').focus();$('#share-url').select();return;}
    if(action==='order-chat'){thread='order:'+id;navigate('chat');return;}
    if(action==='support-user'){thread='support:'+id;navigate('chat');return;}
    if(action==='support'){thread='support:'+S.user.id;navigate('chat');return;}
    if(action==='thread'){thread=el.dataset.id;messageThread=null;renderApp();return;}
    if(action==='more-messages'){await busy(el,()=>loadMessages(messageRows[0]?.id||0));return;}
    if(action==='assign'){
      const couriers=S.people.filter(p=>p.role==='courier'&&p.approved);
      modal('Выбрать курьера','Заказ #'+orderId(id),`<form id="assign-form" data-id="${id}"><label for="assign-courier">Кто отправится в путь?</label><select id="assign-courier" name="courier_id" required>${couriers.map(c=>`<option value="${c.id}">${esc(c.name)} · ${c.online?'На линии':'Офлайн'}</option>`).join('')}</select><p class="form-help">Назначение возможно, если у курьера меньше трёх активных заказов.</p><div class="form-error"></div></form>`,btn('Отмена','close-modal','','ghost')+`<button type="submit" form="assign-form" class="btn primary">Назначить ${icon('arrow',14)}</button>`,'narrow');return;
    }
    if(action==='cancel-order'){modal('Отменить заказ #'+orderId(id)+'?','Укажите причину — её увидят участники доставки.',`<form id="cancel-form" data-id="${id}"><label for="cancel-reason">Причина отмены</label><textarea name="reason" id="cancel-reason" required maxlength="400" placeholder="Почему заказ больше не нужен?"></textarea><div class="form-error"></div></form>`,btn('Вернуться','close-modal','','ghost')+`<button type="submit" form="cancel-form" class="btn danger">Отменить заказ</button>`,'narrow');return;}
    if(action==='notifications'){
      const orders=S.orders.filter(o=>o.status==='new').slice(0,10),pending=S.user.role==='admin'?S.people.filter(p=>p.role==='courier'&&!p.approved):[];
      modal('Что нового в городе','Заказы и заявки, которым нужно внимание.',`${pending.map(p=>`<div class="activity">${avatar(p.name,p.id)}<div class="grow"><b>${esc(p.name)}</b><p class="muted" style="font-size:10px">Заявка в команду курьеров</p></div>${btn('Подтвердить','approve','check','primary small',`data-id="${p.id}"`)}</div>`).join('')}${orders.map(o=>`<button class="activity" style="width:100%;text-align:left" data-action="${S.user.role==='courier'?'close-modal':'order'}" data-id="${o.id}"><span class="activity-icon">${icon('box',14)}</span><span class="grow"><b style="font-size:12px">#${orderId(o.id)}</b><p class="muted" style="font-size:10px">${esc(o.description)} · ${esc(o.pickup)}</p></span>${badge(o.status)}</button>`).join('')}${!orders.length&&!pending.length?empty('Всё спокойно','Новые события появятся здесь.','bell'):''}`,btn('Понятно','close-modal','','primary'),'narrow');return;
    }
    if(action==='search'){modal('Найдём за секунду','Номер заказа, адрес, содержимое или имя.',`<input id="quick-search" aria-label="Поиск" placeholder="Например: DR-0042, Абовяна…" autocomplete="off"><div id="quick-results" style="margin-top:18px"></div>`,'','narrow');return;}
    if(action==='telegram-link'){await busy(el,async()=>{const result=await api('/telegram/link','POST',{});modal('Подключи Telegram','Ссылка действительна 10 минут.',`<p class="confirm-text">Открой бота и нажми «Старт». После этого ${S.user.role==='courier'?'заказы и уведомления':'статусы доставки'} будут приходить в Telegram.</p><a href="${esc(result.url)}" target="_blank" rel="noopener" class="btn primary full" style="margin-top:22px">${icon('telegram',17)}Открыть бота</a>`,btn('Готово','close-modal','','ghost'),'narrow');});return;}
    if(action==='telegram-unlink'){await busy(el,async()=>{await api('/telegram/unlink','POST',{});toast('Telegram отключён');await refresh(true);});return;}
    if(action==='enable-notifications'){if(!('Notification'in window))throw new Error('Этот браузер не поддерживает уведомления.');const permission=await Notification.requestPermission();notificationEnabled=permission==='granted';toast(notificationEnabled?'Уведомления включены':'Разрешите уведомления в настройках браузера.',!notificationEnabled);return;}
    if(action==='logout'){await busy(el,async()=>{await api('/auth/logout','POST',{});authEpoch++;events?.close();stopGPS();S=null;history.replaceState(null,'','/login');renderAuth(false);});return;}
  }
  document.addEventListener('click',async e=>{
    const el=e.target.closest('[data-action]');
    if(el){e.preventDefault();try{await handleAction(el);}catch(error){toast(error.message,true);}}
    else if(e.target.classList.contains('modal-backdrop'))closeModal();
    else if(S&&e.target.closest('.main'))$('#sidebar')?.classList.remove('open');
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'){$('#sidebar')?.classList.remove('open');closeModal();}
    if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&S){e.preventDefault();handleAction({dataset:{action:'search'}});}
    if(e.key==='Enter'&&e.target.matches('tr[data-action],g[data-action]')){e.preventDefault();handleAction(e.target).catch(e=>toast(e.message,true));}
    if(e.key==='Tab'&&overlay.firstChild){const focusable=$$('button:not([disabled]),a[href],input:not([type=hidden]),select,textarea,[tabindex="0"]',overlay).filter(el=>el.offsetParent!==null);const firstEl=focusable[0],lastEl=focusable.at(-1);if(e.shiftKey&&document.activeElement===firstEl){e.preventDefault();lastEl?.focus();}else if(!e.shiftKey&&document.activeElement===lastEl){e.preventDefault();firstEl?.focus();}}
  });
  document.addEventListener('input',e=>{
    const el=e.target;
    if(el.id==='order-search'){orderSearch=el.value;orderPage=1;const rows=filteredOrders();$('#order-table').innerHTML=orderTable(rows.slice(0,8));}
    if(el.id==='people-search'){peopleSearch=el.value;let people=S.people.filter(p=>p.role===(page==='couriers'?'courier':'client')&&(p.name+' '+p.email+' '+p.phone).toLowerCase().includes(peopleSearch.toLowerCase()));if(peopleFilter==='online')people=people.filter(p=>p.online);if(peopleFilter==='pending')people=people.filter(p=>!p.approved);$('#people-grid').innerHTML=people.map(personCard).join('')||empty('Никого не нашли','Измените условия поиска.','people');}
    if(el.closest('#order-form')&&['weight','priority','fee','payout'].includes(el.name))updateQuote(['weight','priority'].includes(el.name));
    if(el.id==='quick-search'){const query=el.value.trim().toLowerCase();const results=query?S.orders.filter(o=>(S.user.role!=='courier'||o.courier_id===S.user.id)&&(orderId(o.id)+' '+o.pickup+' '+o.dropoff+' '+o.description+' '+(person(o.client_id)?.name||'')+' '+(person(o.courier_id)?.name||'')).toLowerCase().includes(query)).slice(0,7):[];$('#quick-results').innerHTML=results.map(o=>`<button class="activity" style="width:100%;text-align:left" data-action="order" data-id="${o.id}"><span class="activity-icon">${icon('box',14)}</span><span class="grow"><b>#${orderId(o.id)}</b><p class="muted" style="font-size:10px">${esc(o.description)} · ${esc(o.pickup)}</p></span>${icon('arrow',14)}</button>`).join('')||(query?'<p class="muted" style="font-size:11px">Ничего не нашли. Попробуйте другой запрос.</p>':'');}
  });
  document.addEventListener('change',async e=>{
    const el=e.target;
    if(el.id==='demo-role'){try{stopGPS();await demoLogin(el.value);}catch(error){toast(error.message,true);}}
    if(el.id==='finance-period'){period=Number(el.value);renderApp();}
    if(el.id==='tracking-order'){selectedOrder=Number(el.value);renderApp();}
    if(el.id==='proof-file'&&el.files?.[0]){try{proofPhoto=await compressPhoto(el.files[0]);showProofPhoto();}catch(error){toast(error.message,true);}}
  });
  document.addEventListener('submit',async e=>{
    e.preventDefault();const form=e.target;
    if(form.id==='auth-form')await submitForm(form,async b=>{const result=await api(form.dataset.register==='true'?'/auth/register':'/auth/login','POST',b);S={csrf:result.csrf};await enterApp();});
    if(form.id==='person-form')await submitForm(form,async b=>{await api('/users','POST',b);closeModal();toast('Аккаунт создан. Человек уже в команде.');await refresh(true);});
    if(form.id==='order-form')await submitForm(form,async b=>{const result=await api('/orders','POST',b);closeModal();toast('Заказ #'+orderId(result.id)+' создан');orderTab='all';orderSearch='';orderPage=1;await refresh(true);await openOrder(result.id);});
    if(form.id==='profile-form')await submitForm(form,async b=>{await api('/profile','PATCH',b);toast('Профиль обновлён');await refresh(true);});
    if(form.id==='password-form')await submitForm(form,async b=>{await api('/password','POST',b);form.reset();toast('Пароль изменён. Другие сессии завершены.');});
    if(form.id==='settings-form')await submitForm(form,async b=>{await api('/settings','PATCH',b);toast('Настройки сохранены');await refresh(true);});
    if(form.id==='chat-form')await submitForm(form,async b=>{const requested=thread;await api('/messages','POST',{thread:requested,body:b.body});if(thread===requested){form.reset();await loadMessages();$('#chat-input')?.focus();}});
    if(form.id==='assign-form')await submitForm(form,async b=>{await api('/orders/'+form.dataset.id+'/assign','POST',b);closeModal();toast('Курьер назначен');await refresh(true);});
    if(form.id==='cancel-form')await submitForm(form,async b=>{await api('/orders/'+form.dataset.id+'/cancel','POST',b);closeModal();toast('Заказ отменён');await refresh(true);});
    if(form.id==='proof-form')await submitForm(form,async b=>{if(!proofPhoto)throw new Error('Сначала сделайте или выберите фотографию.');await api('/orders/'+proofOrder+'/proof','POST',{stage:proofStage,comment:b.comment,photo:proofPhoto});closeModal();toast(proofStage==='delivered'?'Доставлено! Ещё один хороший маршрут позади.':'Получение подтверждено. Можно ехать к получателю.');await refresh(true);});
  });
  window.addEventListener('hashchange',()=>{if(S){closeModal();renderApp();window.scrollTo(0,0);}});
  window.addEventListener('online',()=>{if(S)refresh();});
  window.addEventListener('pagehide',()=>{cameraStream?.getTracks().forEach(t=>t.stop());});
  setInterval(()=>{if(S&&document.visibilityState==='visible')refresh();},30000);
  async function init(){
    try{
      config=await api('/config');
      if(location.pathname.startsWith('/track/')){await publicTracking();publicTimer=setInterval(publicTracking,10000);return;}
      if(location.pathname==='/welcome'){renderWelcome();return;}
      if(location.pathname==='/login'||location.pathname==='/register'){renderAuth();return;}
      if(config.telegram&&(new URLSearchParams(location.search).has('app')||location.hash.includes('tgWebAppData'))){
        await new Promise(resolve=>{const script=document.createElement('script');script.src='https://telegram.org/js/telegram-web-app.js';script.onload=resolve;script.onerror=resolve;document.head.appendChild(script);setTimeout(resolve,4000);});
        const tg=window.Telegram?.WebApp;
        if(tg?.initData){tg.ready();tg.expand();tg.setHeaderColor('#111312');tg.setBackgroundColor('#111312');const result=await api('/auth/telegram','POST',{initData:tg.initData});S={csrf:result.csrf};}
      }
      try{S=await api('/me');renderApp();connectEvents();}
      catch(e){if(e.status!==401)throw e;if(config.demo){await demoLogin(new URLSearchParams(location.search).get('app')==='courier'?'courier':'admin');}else renderAuth(false);}
    }catch(e){app.innerHTML=`<div class="boot">${brand()}<p style="max-width:400px;padding:0 25px;text-align:center">${esc(e.message)}</p><button class="btn primary" data-action="reload">Повторить</button></div>`;const reload=$('[data-action="reload"]');if(reload)reload.addEventListener('click',()=>location.reload());}
  }
  init();
})();</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DROPP/2.0"
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format_string, *args):
        # Do not log public tracking tokens or request bodies.
        pass

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
        if not self.app.cfg.token:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        if self.app.cfg.production:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for key, value in self.extra_headers.items():
            self.send_header(key, value)

    def send_bytes(self, body, content_type, status=200, headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        response_headers = {"Cache-Control": "no-store", **(headers or {})}
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.sent_headers = True
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", status)

    def body(self):
        if self.cached_body is not None:
            return self.cached_body
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            fail(415, "Используйте application/json.")
        if self.headers.get("Transfer-Encoding"):
            fail(400, "Передайте JSON с Content-Length.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            fail(411, "Укажите единственный Content-Length.")
        try:
            length = int(lengths[0])
        except ValueError:
            fail(400, "Некорректная длина запроса.")
        if length < 0:
            fail(400, "Некорректная длина запроса.")
        if length > MAX_BODY:
            fail(413, "Файл слишком большой (фото до 4 МБ).")
        raw = self.rfile.read(length)
        if len(raw) != length:
            fail(400, "Запрос передан не полностью.")
        try:
            def invalid_constant(value):
                raise ValueError(value)
            data = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeError):
            fail(400, "Некорректный JSON.")
        self.cached_body = data
        return data

    def send_events(self, user):
        app = self.app
        with app.condition:
            if app.stream_counts[user["id"]] >= 5:
                fail(429, "Слишком много открытых вкладок.")
            app.stream_counts[user["id"]] += 1
            last = app.sequence
        try:
            self.send_response(200)
            self.common_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.sent_headers = True
            self.close_connection = True
            self.wfile.write(b'retry: 3000\ndata: {"type":"connected"}\n\n')
            self.wfile.flush()
            while not app.stop_event.is_set():
                with app.condition:
                    app.condition.wait_for(lambda: app.sequence > last or app.stop_event.is_set(), timeout=25)
                    outgoing = [(seq, value) for seq, value in app.events if seq > last]
                    if outgoing:
                        last = outgoing[-1][0]
                if app.stop_event.is_set() or not app.get_session(self.headers):
                    break
                for seq, payload in outgoing:
                    self.wfile.write(("data: " + payload + "\n\n").encode())
                if not outgoing:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        finally:
            with app.condition:
                app.stream_counts[user["id"]] -= 1
                if not app.stream_counts[user["id"]]:
                    del app.stream_counts[user["id"]]

    def dispatch(self):
        self.extra_headers = {}
        self.sent_headers = False
        self.cached_body = None
        try:
            url = urllib.parse.urlsplit(self.path)
            if url.path.startswith("/api/"):
                return self.app.api(self, url.path, urllib.parse.parse_qs(url.query, max_num_fields=50))
            if self.command not in ("GET", "HEAD"):
                fail(405, "Method not allowed")
            if url.path not in ("/", "/welcome", "/login", "/register") and not re.fullmatch(r"/track/[A-Za-z0-9_-]{32}", url.path):
                fail(404, "Страница не найдена.")
            nonce = secrets.token_urlsafe(24)
            telegram = " https://telegram.org" if self.app.cfg.token else ""
            frames = " https://web.telegram.org https://*.telegram.org" if self.app.cfg.token else ""
            policy = (f"default-src 'self'; script-src 'nonce-{nonce}'{telegram}; style-src 'self' 'unsafe-inline'; "
                      "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; base-uri 'none'; object-src 'none'; "
                      f"form-action 'self'; frame-ancestors 'self'{frames}")
            self.send_bytes(CLIENT_HTML.replace("%%NONCE%%", nonce), "text/html; charset=utf-8", headers={"Content-Security-Policy": policy})
        except APIError as error:
            if not self.sent_headers:
                self.send_json({"error": str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            logging.exception("Request failed")
            if not self.sent_headers:
                self.send_json({"error": "Ошибка сервера. Попробуйте ещё раз."}, 500)

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, app, address=None):
        self.app = app
        super().__init__(address or (app.cfg.host, app.cfg.port), Handler)

def run_self_tests():
    """Real local HTTP + temporary SQLite. Telegram is mocked; no messages sent."""
    import concurrent.futures
    import tempfile
    import unittest
    from unittest import mock

    class Client:
        def __init__(self, owner):
            self.owner, self.cookie, self.csrf, self.user = owner, "", "", None

        def call(self, path, method="GET", data=None, headers=None):
            sent = {"Cookie": self.cookie} if self.cookie else {}
            if self.csrf:
                sent["X-CSRF-Token"] = self.csrf
            if method not in ("GET", "HEAD"):
                data = json.dumps(data if data is not None else {}, ensure_ascii=False).encode()
                sent["Content-Type"] = "application/json"
            sent.update(headers or {})
            request = urllib.request.Request(self.owner.base + "/api" + path, data=data, headers=sent, method=method)
            try:
                response = urllib.request.urlopen(request, timeout=8)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                content = response.read()
                if "Set-Cookie" in response.headers:
                    self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                result = json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content
                if isinstance(result, dict):
                    self.csrf = result.get("csrf", self.csrf)
                    self.user = result.get("user", self.user)
                return response.status, result, response.headers

    photo = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZCkAAAAASUVORK5CYII="
    password = "TestCourier!2026"

    class DeliveryTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.temp = tempfile.TemporaryDirectory(prefix=".dropp-python-tests-", dir=ROOT)
            cls.settings = Settings(db_path=str(Path(cls.temp.name) / "test.sqlite"))
            cls.start()
            cls.admin, cls.alice, cls.bob, cls.c1, cls.c2, cls.pending = [Client(cls) for _ in range(6)]
            cls.body = {"pickup": "ул. Абовяна, 12", "dropoff": "Каскад", "pickup_lat": 40.1828, "pickup_lng": 44.5169,
                        "dropoff_lat": 40.1918, "dropoff_lng": 44.5156, "recipient": "Получатель Тест", "phone": "+374 91 900 001",
                        "description": "Документы <b>важные</b>", "weight": 1.2, "priority": "express", "note": "Код домофона 4321",
                        "fee": 1, "payout": 1, "request_key": "python-test-order-0001"}

        @classmethod
        def start(cls):
            cls.application = Application(cls.settings)
            cls.httpd = Server(cls.application, ("127.0.0.1", 0))
            cls.base = "http://127.0.0.1:" + str(cls.httpd.server_port)
            cls.application.cfg.public_url = cls.base
            cls.worker = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            cls.worker.start()

        @classmethod
        def stop(cls):
            cls.application.stop_event.set()
            with cls.application.condition:
                cls.application.condition.notify_all()
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.worker.join(timeout=2)
            cls.application.close()

        @classmethod
        def tearDownClass(cls):
            cls.stop()
            cls.temp.cleanup()

        def test_01_health_and_private_data(self):
            self.assertEqual(self.alice.call("/health")[0], 200)
            self.assertEqual(self.alice.call("/me")[0], 401)
            self.assertEqual(self.alice.call("/config")[1]["demo"], True)

        def test_02_admin_demo_and_snapshot(self):
            self.assertEqual(self.admin.call("/auth/demo", "POST", {"role": "admin"})[0], 200)
            status, data, headers = self.admin.call("/me")
            self.assertEqual(status, 200)
            self.assertEqual(data["user"]["role"], "admin")
            self.assertEqual(len(data["orders"]), 42)
            self.assertTrue(all("password" not in p and "telegram_id" not in p for p in data["people"]))

        def test_03_registration_and_approval(self):
            for client, name, email, role in [(self.alice, "Алиса <script>alert(1)</script>", "alice@test.am", "admin"),
                                              (self.bob, "Борис Тест", "bob@test.am", "client"),
                                              (self.pending, "Курьер Заявка", "pending@test.am", "courier")]:
                status, data, headers = client.call("/auth/register", "POST", {"name": name, "email": email, "phone": "+374 91 200 001", "password": password, "role": role})
                self.assertEqual(status, 200)
                self.assertNotEqual(data["user"]["role"], "admin")
            self.assertEqual(self.pending.call("/courier/online", "POST", {"online": True})[0], 403)
            self.assertEqual(len(self.pending.call("/me")[1]["orders"]), 0)
            self.assertEqual(self.alice.call("/users", "POST", {})[0], 403)

        def test_04_csrf_origin_and_validation(self):
            value = {"name": "Не менять", "phone": "+374 91 200 001"}
            self.assertEqual(self.alice.call("/profile", "PATCH", value, {"X-CSRF-Token": "bad"})[0], 403)
            self.assertEqual(self.alice.call("/profile", "PATCH", value, {"Origin": "https://malicious.example"})[0], 403)
            self.assertEqual(self.alice.call("/settings", "PATCH", {"support": "+374 91 111 222"})[0], 403)

        def test_05_admin_creates_couriers(self):
            for index, client in enumerate((self.c1, self.c2), 1):
                data = {"role": "courier", "name": f"Тест Курьер {index}", "email": f"courier{index}@test.am", "phone": "+374 91 210 001", "password": password, "vehicle": "bike"}
                self.assertEqual(self.admin.call("/users", "POST", data)[0], 201)
                self.assertEqual(client.call("/auth/login", "POST", {"email": data["email"], "password": "wrong-password"})[0], 401)
                self.assertEqual(client.call("/auth/login", "POST", {"email": data["email"], "password": password})[0], 200)
                self.assertEqual(client.call("/courier/online", "POST", {"online": True})[0], 200)

        def test_06_atomic_creation_and_server_price(self):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: self.alice.call("/orders", "POST", self.body), range(2)))
            self.assertEqual(sorted(r[0] for r in responses), [200, 201])
            self.assertEqual(responses[0][1]["id"], responses[1][1]["id"])
            type(self).order_id = responses[0][1]["id"]
            order = self.alice.call(f"/orders/{self.order_id}")[1]["order"]
            self.assertGreaterEqual(order["fee"], 1000)
            self.assertGreater(order["payout"], 1)
            self.assertEqual(self.bob.call(f"/orders/{self.order_id}")[0], 403)
            self.assertEqual(self.c1.call(f"/orders/{self.order_id}")[0], 403)
            listed = next(o for o in self.c1.call("/me")[1]["orders"] if o["id"] == self.order_id)
            self.assertEqual(listed["phone"], "")
            self.assertEqual(listed["note"], "")
            self.assertNotIn("track_token", listed)

        def test_07_only_one_courier_wins(self):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda c: c.call(f"/orders/{self.order_id}/claim", "POST", {}), (self.c1, self.c2)))
            self.assertEqual(sorted(r[0] for r in responses), [200, 409])
            type(self).winner = self.c1 if responses[0][0] == 200 else self.c2
            type(self).loser = self.c2 if self.winner is self.c1 else self.c1
            details = self.admin.call(f"/orders/{self.order_id}")[1]
            self.assertEqual(len([h for h in details["history"] if h["status"] == "assigned"]), 1)
            self.assertEqual(self.alice.call(f"/orders/{self.order_id}/cancel", "POST", {"reason": "Не нужно"})[0], 403)

        def test_08_live_location_scope(self):
            self.assertEqual(self.winner.call("/courier/location", "POST", {"lat": 40.186, "lng": 44.515})[0], 200)
            self.assertEqual(self.winner.call("/courier/location", "POST", {"lat": 50, "lng": 10})[0], 400)
            visible = next(p for p in self.alice.call("/me")[1]["people"] if p["id"] == self.winner.user["id"])
            self.assertEqual(visible["lat"], 40.186)

        def test_09_chat_membership(self):
            thread = f"order:{self.order_id}"
            self.assertEqual(self.alice.call("/messages", "POST", {"thread": thread, "body": "Передайте лично."})[0], 201)
            self.assertEqual(self.winner.call("/messages", "POST", {"thread": thread, "body": "Конечно, еду."})[0], 201)
            self.assertEqual(len(self.admin.call("/messages?thread=" + thread)[1]["messages"]), 2)
            self.assertEqual(self.bob.call("/messages?thread=" + thread)[0], 403)
            self.assertEqual(self.loser.call("/messages?thread=" + thread)[0], 403)
            self.assertEqual(self.alice.call("/messages", "POST", {"thread": f"support:{self.bob.user['id']}", "body": "Чужой диалог"})[0], 403)

        def test_10_photo_and_comment_mandatory(self):
            path = f"/orders/{self.order_id}/proof"
            self.assertEqual(self.winner.call(path, "POST", {"stage": "delivered", "comment": "Рано", "photo": photo})[0], 409)
            self.assertEqual(self.winner.call(path, "POST", {"stage": "picked_up", "comment": "", "photo": photo})[0], 400)
            fake = "data:image/png;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="
            self.assertEqual(self.winner.call(path, "POST", {"stage": "picked_up", "comment": "Fake", "photo": fake})[0], 400)
            self.assertEqual(self.alice.call(path, "POST", {"stage": "picked_up", "comment": "Нет", "photo": photo})[0], 403)
            for stage in ("picked_up", "delivered"):
                self.assertEqual(self.winner.call(path, "POST", {"stage": stage, "comment": "Передано лично, упаковка целая", "photo": photo})[0], 200)
            self.assertEqual(self.winner.call(path, "POST", {"stage": "delivered", "comment": "Повтор", "photo": photo})[0], 409)

        def test_11_private_photos_public_tracking(self):
            details = self.alice.call(f"/orders/{self.order_id}")[1]
            self.assertEqual(details["order"]["status"], "delivered")
            self.assertEqual(len(details["photos"]), 2)
            type(self).photo_id = details["photos"][0]["id"]
            self.assertEqual(self.alice.call("/photos/" + self.photo_id)[0], 200)
            self.assertEqual(self.bob.call("/photos/" + self.photo_id)[0], 403)
            status, track, headers = self.bob.call("/track/" + details["order"]["track_token"])
            self.assertEqual(status, 200)
            self.assertIsNone(track["courier"]["lat"])
            self.assertNotIn("phone", track["order"])
            self.assertNotIn("client_id", track["order"])
            self.assertEqual(self.bob.call("/track/invalid-token")[0], 404)

        def test_12_approval_assignment_cancellation(self):
            self.assertEqual(self.admin.call(f"/users/{self.pending.user['id']}/approve", "POST", {})[0], 200)
            self.assertEqual(self.pending.call("/courier/online", "POST", {"online": True})[0], 200)
            created = self.alice.call("/orders", "POST", {**self.body, "request_key": "python-test-order-0002"})[1]["id"]
            self.assertEqual(self.admin.call(f"/orders/{created}/assign", "POST", {"courier_id": self.pending.user["id"]})[0], 200)
            self.assertEqual(self.pending.call(f"/orders/{created}")[0], 200)
            self.assertEqual(self.admin.call(f"/orders/{created}/cancel", "POST", {"reason": "Проверка отмены"})[0], 200)
            created = self.alice.call("/orders", "POST", {**self.body, "request_key": "python-test-order-0003"})[1]["id"]
            self.assertEqual(self.alice.call(f"/orders/{created}/cancel", "POST", {"reason": "Изменились планы"})[0], 200)

        def test_13_three_order_limit_is_atomic(self):
            ids = [self.alice.call("/orders", "POST", {**self.body, "request_key": f"python-quota-order-{i:04d}"})[1]["id"] for i in range(4)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(lambda oid: self.winner.call(f"/orders/{oid}/claim", "POST", {}), ids))
            self.assertEqual(sorted(r[0] for r in responses), [200, 200, 200, 409])

        def test_14_validation_quote_csv(self):
            self.assertEqual(self.alice.call("/orders", "POST", {**self.body, "weight": -1, "request_key": "bad-weight-0001"})[0], 400)
            self.assertGreater(self.alice.call("/quote", "POST", self.body)[1]["fee"], 1)
            self.assertEqual(self.alice.call("/export")[0], 403)
            self.assertIn("ул. Абовяна".encode(), self.winner.call("/export")[1])
            self.assertEqual(self.admin.call("/export")[0], 200)

        def test_15_sse_refresh(self):
            request = urllib.request.Request(self.base + "/api/events", headers={"Cookie": self.alice.cookie})
            with urllib.request.urlopen(request, timeout=4) as stream:
                self.assertEqual(stream.headers["Content-Type"], "text/event-stream")
                self.assertIn(b"retry", stream.readline())
                self.assertIn(b"connected", stream.readline())
                stream.readline()
                self.application.broadcast("message")
                self.assertIn(b"message", stream.readline())

        def test_16_embedded_html_and_csp(self):
            for path in ("/", "/welcome", "/login", "/register"):
                with urllib.request.urlopen(self.base + path) as response:
                    html = response.read().decode()
                    self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])
                    self.assertIn("function clientMain()", html)
                    self.assertIn(".sidebar", html)
                    self.assertIn("<style nonce=", html)
                    self.assertNotIn("%%NONCE%%", html)
                    self.assertNotRegex(html, r"fonts\.googleapis|tile\.openstreetmap|mapbox")
                    self.assertNotIn("__FRONTEND_PLACEHOLDER__", html)

        def test_17_database_survives_restart(self):
            type(self).stop()
            type(self).start()
            status, data, headers = self.alice.call(f"/orders/{self.order_id}")
            self.assertEqual(status, 200)
            self.assertEqual(data["order"]["status"], "delivered")
            self.assertEqual(len(data["photos"]), 2)
            self.assertEqual(len(self.winner.call(f"/messages?thread=order:{self.order_id}")[1]["messages"]), 2)
            self.assertEqual(self.alice.call("/photos/" + self.photo_id)[0], 200)

        def test_18_password_rotation_and_logout(self):
            second = Client(type(self))
            self.assertEqual(second.call("/auth/login", "POST", {"email": "alice@test.am", "password": password})[0], 200)
            self.assertEqual(self.alice.call("/password", "POST", {"current": password, "password": "NewPassword!2026"})[0], 200)
            self.assertEqual(second.call("/me")[0], 401)
            self.assertEqual(self.alice.call("/auth/logout", "POST", {})[0], 200)
            self.assertEqual(self.alice.call("/me")[0], 401)

        def test_19_production_has_no_demo_and_rejects_mixed_database(self):
            with mock.patch.dict(os.environ, {**{k: str(v) for k, v in CONFIG.items()}, "NODE_ENV": "production", "DEMO_MODE": "false", "PUBLIC_URL": "https://dropp.example", "ADMIN_EMAIL": "owner@example.com", "ADMIN_PASSWORD": "ProductionFixture!2026", "DB_PATH": str(Path(self.temp.name) / "production.sqlite"), "TELEGRAM_BOT_TOKEN": ""}, clear=True):
                cfg = Settings.from_env()
                app = Application(cfg)
                try:
                    self.assertEqual(len(app.query("SELECT * FROM users")), 1)
                    self.assertEqual(len(app.query("SELECT * FROM orders")), 0)
                    self.assertTrue(check_password("ProductionFixture!2026", app.one("SELECT * FROM users")["password"]))
                finally:
                    app.close()
                with mock.patch.dict(os.environ, {"DEMO_MODE": "true"}):
                    self.assertRaises(ValueError, Settings.from_env)
                with mock.patch.dict(os.environ, {"PUBLIC_URL": "http://localhost:3000"}):
                    self.assertRaises(ValueError, Settings.from_env)
            mixed = dataclasses.replace(self.settings, demo=False, admin_password="ProductionFixture!2026")
            self.assertRaises(ValueError, Application, mixed)

    class ConfigurationTests(unittest.TestCase):
        def environment(self, **overrides):
            return {**{k: str(v) for k, v in CONFIG.items()}, "NODE_ENV": "production", "DEMO_MODE": "false",
                    "ADMIN_PASSWORD": "ProductionFixture!2026", "TELEGRAM_BOT_TOKEN": "", "PUBLIC_URL": "",
                    "RENDER": "true", "RENDER_EXTERNAL_URL": "https://dropp-fixture.onrender.com", "PORT": "10000",
                    "DB_PATH": "./data/production.sqlite", **overrides}

        def test_render_url_and_port_are_detected(self):
            with mock.patch.dict(os.environ, self.environment(), clear=True):
                cfg = Settings.from_env()
            self.assertEqual(cfg.public_url, "https://dropp-fixture.onrender.com")
            self.assertEqual(cfg.port, 10000)
            self.assertEqual(cfg.host, "0.0.0.0")
            self.assertFalse(cfg.demo)

        def test_custom_domain_is_preserved(self):
            with mock.patch.dict(os.environ, self.environment(PUBLIC_URL=" https://delivery.example.com/ "), clear=True):
                cfg = Settings.from_env()
            self.assertEqual(cfg.public_url, "https://delivery.example.com")

        def test_missing_password_names_only_the_invalid_field(self):
            with mock.patch.dict(os.environ, self.environment(ADMIN_PASSWORD=""), clear=True):
                with self.assertRaisesRegex(ValueError, "ADMIN_PASSWORD: не задан") as error:
                    Settings.from_env()
            self.assertNotIn("PUBLIC_URL:", str(error.exception))
            self.assertNotIn("DEMO_MODE:", str(error.exception))
            self.assertNotIn("DB_PATH:", str(error.exception))

        def test_short_password_is_identified_without_revealing_it(self):
            with mock.patch.dict(os.environ, self.environment(ADMIN_PASSWORD="tiny-secret"), clear=True):
                with self.assertRaisesRegex(ValueError, "ADMIN_PASSWORD: слишком короткий") as error:
                    Settings.from_env()
            self.assertNotIn("tiny-secret", str(error.exception))

        def test_all_invalid_production_fields_are_listed(self):
            with mock.patch.dict(os.environ, self.environment(DEMO_MODE="true", ADMIN_PASSWORD="", PUBLIC_URL="http://localhost:3000", DB_PATH=":memory:"), clear=True):
                with self.assertRaises(ValueError) as error:
                    Settings.from_env()
            for name in ("DEMO_MODE:", "ADMIN_PASSWORD:", "PUBLIC_URL:", "DB_PATH:"):
                self.assertIn(name, str(error.exception))

        def test_empty_render_port_and_bad_ports(self):
            with mock.patch.dict(os.environ, self.environment(PORT=""), clear=True):
                self.assertEqual(Settings.from_env().port, 10000)
            for value in ("invalid", "65536", "-1"):
                with mock.patch.dict(os.environ, self.environment(PORT=value), clear=True):
                    self.assertRaises(ValueError, Settings.from_env)

        def test_config_command_checks_without_creating_database(self):
            import subprocess
            with tempfile.TemporaryDirectory(prefix=".dropp-config-tests-", dir=ROOT) as directory:
                database = str(Path(directory) / "not-created.sqlite")
                child_env = {**os.environ, **self.environment(DB_PATH=database)}
                success = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--check-config"], env=child_env, capture_output=True, text=True, timeout=10)
                self.assertEqual(success.returncode, 0, success.stderr)
                self.assertIn("ENV OK", success.stdout)
                self.assertNotIn("ProductionFixture!2026", success.stdout)
                self.assertFalse(Path(database).exists())
                child_env["ADMIN_PASSWORD"] = ""
                failure = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--check-config"], env=child_env, capture_output=True, text=True, timeout=10)
                self.assertEqual(failure.returncode, 1)
                self.assertIn("ADMIN_PASSWORD: не задан", failure.stderr)
                self.assertFalse(Path(database).exists())

    class TelegramTests(unittest.TestCase):
        def setUp(self):
            self.app = Application(Settings(token="123:offline-fixture", bot_name="dropp_test_bot", public_url="https://dropp.example", telegram_mode="webhook", webhook_secret="fixture-secret-with-at-least-32-characters"))
            self.requests, self.fail_next = [], False
            def fake_api(method, payload):
                self.requests.append((method, payload))
                if self.fail_next:
                    self.fail_next = False
                    raise APIError(502, "temporary mocked failure")
                return {"username": "dropp_test_bot"} if method == "getMe" else True
            self.app.telegram_api = fake_api

        def tearDown(self):
            self.app.close()

        def test_bot_link_callbacks_dedup_and_retry(self):
            app = self.app
            courier = app.one("SELECT * FROM users WHERE role='courier' ORDER BY id DESC LIMIT 1")
            app.execute("UPDATE users SET online=1 WHERE id=?", courier["id"])
            code = secrets.token_urlsafe(24)
            app.execute("INSERT INTO telegram_links(hash,user_id,expires) VALUES (?,?,?)", token_hash(code), courier["id"], millis() + 600000)
            app.telegram_update({"update_id": 100, "message": {"chat": {"id": 777, "type": "private"}, "from": {"id": 777}, "text": "/start " + code}})
            self.assertEqual(app.one("SELECT telegram_id FROM users WHERE id=?", courier["id"])["telegram_id"], "777")
            self.assertEqual(app.one("SELECT COUNT(*) c FROM telegram_links")["c"], 0)
            order = app.one("SELECT * FROM orders WHERE status='new' ORDER BY id DESC LIMIT 1")
            app.announce_order(order)
            update = {"update_id": 101, "callback_query": {"id": "callback-fixture", "from": {"id": 777}, "data": f"claim:{order['id']}"}}
            app.telegram_update(update)
            app.telegram_update(update)
            self.assertEqual(app.one("SELECT courier_id FROM orders WHERE id=?", order["id"])["courier_id"], courier["id"])
            self.assertEqual(app.one("SELECT COUNT(*) c FROM history WHERE order_id=? AND status='assigned'", order["id"])["c"], 1)
            self.assertTrue(any(method == "answerCallbackQuery" and "Заказ ваш" in payload["text"] for method, payload in self.requests))
            self.fail_next = True
            app.flush_outbox()
            failed = app.one("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts>0")
            self.assertIsNotNone(failed)
            self.assertGreater(failed["next_attempt"], millis())
            app.execute("UPDATE outbox SET next_attempt=0 WHERE id=?", failed["id"])
            app.flush_outbox()
            self.assertIsNotNone(app.one("SELECT sent_at FROM outbox WHERE id=?", failed["id"])["sent_at"])
            app.telegram_update({"update_id": 102, "edited_message": {"chat": {"id": 777, "type": "private"}, "from": {"id": 777}, "location": {"latitude": 40.188, "longitude": 44.514}}})
            self.assertEqual(app.one("SELECT lat FROM users WHERE id=?", courier["id"])["lat"], 40.188)
            with contextlib.redirect_stdout(io.StringIO()):
                app.setup_telegram()
            self.assertTrue(any(method == "setWebhook" for method, payload in self.requests))

        def test_mini_app_hmac_tampering_and_expiration(self):
            app = self.app
            app.execute("UPDATE users SET telegram_id='777' WHERE id=(SELECT id FROM users WHERE role='courier' ORDER BY id DESC LIMIT 1)")
            def signed(age=0):
                pairs = {"auth_date": str(int(time.time()) - age), "query_id": "fake-query", "user": json.dumps({"id": 777}), "signature": "fixture-signature"}
                data = "\n".join(k + "=" + v for k, v in sorted(pairs.items()))
                key = hmac.new(b"WebAppData", app.cfg.token.encode(), hashlib.sha256).digest()
                pairs["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
                return urllib.parse.urlencode(pairs)
            self.assertEqual(app.telegram_user(signed())["role"], "courier")
            self.assertRaises(APIError, app.telegram_user, signed().replace("fake-query", "tampered"))
            self.assertRaises(APIError, app.telegram_user, signed(7200))
            self.assertRaises(APIError, app.telegram_user, signed(-120))

    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(DeliveryTests), unittest.defaultTestLoader.loadTestsFromTestCase(ConfigurationTests), unittest.defaultTestLoader.loadTestsFromTestCase(TelegramTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

def main():
    parser = argparse.ArgumentParser(description="DROPP / Yerevan — сайт, база и Telegram в одном bot.py.",
                                     epilog="Python 3.10+, без pip/npm. Настройки — CONFIG в начале файла или ENV. Build: python -m py_compile bot.py")
    parser.add_argument("--setup-telegram", action="store_true", help="проверить токен, настроить меню бота и polling/webhook")
    parser.add_argument("--print-env", action="store_true", help="вывести шаблон всех ENV, без секретных значений")
    parser.add_argument("--check-config", action="store_true", help="проверить ENV без создания базы и отправки сообщений")
    parser.add_argument("--self-test", action="store_true", help="проверить API, доставку, гонки, сохранение и Telegram офлайн")
    args = parser.parse_args()
    if args.print_env:
        for key, value in CONFIG.items():
            if key in ("ADMIN_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"):
                value = ""
            print(f"{key}={value}")
        return 0
    if args.self_test:
        return run_self_tests()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = None
    server = None
    try:
        settings = Settings.from_env()
        if args.check_config:
            print("ENV OK\nРежим: " + ("production" if settings.production else "development"))
            print("Публичный адрес: " + settings.public_url)
            print("Telegram: " + (settings.telegram_mode if settings.token else "не подключён"))
            return 0
        app = Application(settings)
        if args.setup_telegram:
            app.setup_telegram()
            return 0
        server = Server(app)
        app.start_workers()
        def shutdown(signum, frame):
            if not app.stop_event.is_set():
                app.stop_event.set()
                with app.condition:
                    app.condition.notify_all()
                threading.Thread(target=server.shutdown, daemon=True).start()
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), shutdown)
        print(f"DROPP ready: {app.cfg.public_url}\nPython only · SQLite embedded · mode: {'demo' if app.cfg.demo else 'production'}", flush=True)
        server.serve_forever(poll_interval=.3)
        return 0
    except (ValueError, OSError, APIError) as error:
        print("Ошибка запуска: " + str(error), file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
