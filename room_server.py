"""Small in-memory WebSocket lobby and co-op game server for Temple Echo."""
from __future__ import annotations

import asyncio
import hashlib
import os
import math
import random
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

ROOMS: dict[str, "Room"] = {}
CAPACITY_LABELS = {2: "ЛЁГКИЙ", 4: "СРЕДНИЙ", 6: "ХАРД"}
WORLD_W, WORLD_H = 1280, 810
HEROES = {
    "scout": {"name": "Следопыт", "health": 3, "speed": 205, "fire_rate": 0.17, "revive": 1.0},
    "guardian": {"name": "Страж", "health": 4, "speed": 172, "fire_rate": 0.23, "revive": 1.0},
    "healer": {"name": "Хранитель", "health": 3, "speed": 185, "fire_rate": 0.22, "revive": 1.8},
}
SKINS = {"ember": "#a94f2d", "moss": "#4d8a54", "moon": "#758bbc", "royal": "#a77a43", "jade": "#78b28a"}
RELICS = {
    "fang": {"name": "Клык титана", "description": "+1 урон выстрела"},
    "feather": {"name": "Перо ветра", "description": "+12% скорость движения"},
    "hourglass": {"name": "Песочные часы", "description": "+18% скорострельность"},
    "heart": {"name": "Сердце храма", "description": "+1 здоровье и лечение"},
    "sun": {"name": "Солнечный знак", "description": "+0,4 сек защиты после удара"},
    "rune": {"name": "Руна помощи", "description": "+30% скорость возрождения"},
}
BIOMES = ["Затонувший храм", "Пепельные залы", "Лунный сад", "Зал корней"]
DAILY_MODIFIERS = [
    {"id": "swarm", "title": "Живой камень", "description": "Стражи быстрее, но дают больше очков."},
    {"id": "embers", "title": "Пепельный ветер", "description": "Волны приходят чаще, выстрелы сильнее."},
    {"id": "fragile", "title": "Хрупкая печать", "description": "Меньше здоровья, зато реликвии чаще."},
    {"id": "guardian", "title": "Пробуждение стража", "description": "Каждый пятый зал охраняет усиленный босс."},
]
DB_PATH = Path(os.getenv("TEMPLE_DB_PATH", "/tmp/temple-echo.sqlite3"))


def daily_challenge(day: str | None = None) -> dict:
    day = day or datetime.now(timezone.utc).date().isoformat()
    digest = hashlib.sha256(day.encode("ascii")).digest()
    modifier = DAILY_MODIFIERS[digest[0] % len(DAILY_MODIFIERS)]
    return {
        "day": day,
        "seed": int.from_bytes(digest[:4], "big"),
        "biome": BIOMES[digest[1] % len(BIOMES)],
        **modifier,
    }


def init_score_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS daily_scores (
            day TEXT NOT NULL, player_id TEXT NOT NULL, name TEXT NOT NULL,
            score INTEGER NOT NULL, level INTEGER NOT NULL, kills INTEGER NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(day, player_id))""")


init_score_db()


@dataclass
class Player:
    id: str
    name: str
    x: float
    y: float
    connected: bool = False
    ws: WebSocket | None = None
    hero_class: str = "scout"
    skin: str = "ember"
    max_health: int = 3
    health: int = 3
    speed: float = 205
    fire_rate: float = 0.17
    damage: int = 1
    shield_bonus: float = 0
    revive_bonus: float = 1.0
    downed: bool = False
    revive: bool = False
    revive_progress: float = 0
    relics: list[str] = field(default_factory=list)
    synergies: list[str] = field(default_factory=list)
    dx: float = 0
    dy: float = 0
    vx: float = 0
    vy: float = 0
    shooting: bool = False
    face: int = 1
    aim_x: float = 1
    aim_y: float = 0
    manual_aim: bool = False
    invincible_until: float = 0
    next_shot: float = 0


@dataclass
class Room:
    code: str
    capacity: int
    host_id: str
    players: dict[str, Player] = field(default_factory=dict)
    status: str = "waiting"
    created_at: float = field(default_factory=time.time)
    level: int = 1
    score: int = 0
    kills: int = 0
    lives: int = 0
    enemies: list[dict] = field(default_factory=list)
    shots: list[dict] = field(default_factory=list)
    next_spawn: float = 0
    task: asyncio.Task | None = None
    upgrade_options: list[str] = field(default_factory=list)
    pending_upgrades: set[str] = field(default_factory=set)
    boss_spawned: bool = False
    cache: dict | None = None
    ticks: int = 0
    hazard_phase: int = 0

    @property
    def difficulty(self) -> str:
        return CAPACITY_LABELS[self.capacity]


class CreateRoom(BaseModel):
    capacity: Literal[2, 4, 6]
    name: str = Field(min_length=1, max_length=20)
    hero_class: str = "scout"
    skin: str = "ember"


class JoinRoom(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    player_id: str | None = None
    hero_class: str = "scout"
    skin: str = "ember"


class RoomAction(BaseModel):
    player_id: str


class RelicAction(BaseModel):
    player_id: str
    relic: str


class DailyScore(BaseModel):
    day: str = Field(min_length=10, max_length=10)
    player_id: str = Field(min_length=8, max_length=80)
    name: str = Field(min_length=1, max_length=20)
    score: int = Field(ge=0, le=2_000_000_000)
    level: int = Field(ge=1, le=1000)
    kills: int = Field(ge=0, le=100_000)


def public_player(player: Player, room: Room) -> dict:
    return {
        "id": player.id,
        "name": player.name,
        "connected": player.connected,
        "host": player.id == room.host_id,
        "hero_class": player.hero_class,
        "class_name": HEROES[player.hero_class]["name"],
        "skin": player.skin,
        "health": player.health,
        "max_health": player.max_health,
        "downed": player.downed,
        "relics": player.relics,
    }


def lobby_payload(room: Room) -> dict:
    return {
        "code": room.code,
        "capacity": room.capacity,
        "player_count": len(room.players),
        "difficulty": room.difficulty,
        "status": room.status,
        "players": [public_player(p, room) for p in room.players.values()],
    }


def game_payload(room: Room) -> dict:
    return {
        "code": room.code,
        "status": room.status,
        "capacity": room.capacity,
        "difficulty": room.difficulty,
        "level": room.level,
        "score": room.score,
        "kills": room.kills,
        "lives": room.lives,
        "biome": BIOMES[((room.level - 1) // 4) % len(BIOMES)],
        "upgrade_options": room.upgrade_options,
        "pending_upgrades": list(room.pending_upgrades),
        "cache": room.cache,
        "hazard_phase": room.hazard_phase,
        "players": [
            {
                "id": p.id,
                "name": p.name,
                "x": round(p.x, 1),
                "y": round(p.y, 1),
                "face": p.face,
                "aimX": round(p.aim_x, 2),
                "aimY": round(p.aim_y, 2),
                "manualAim": p.manual_aim,
                "connected": p.connected,
                "hero_class": p.hero_class,
                "skin": SKINS[p.skin],
                "relics": p.relics,
                "synergies": p.synergies,
                "health": p.health,
                "max_health": p.max_health,
                "downed": p.downed,
                "revive_progress": round(p.revive_progress, 2),
            }
            for p in room.players.values()
        ],
        "enemies": [
            {
                **{k: round(v, 1) if isinstance(v, float) else v for k, v in e.items() if k not in {"windup_until", "lunge_until", "aim_x", "aim_y"}},
                "windup": round(
                    max(0.0, e.get("windup_until", 0.0) - time.monotonic())
                    / max(0.01, e.get("windup_duration", 0.65)),
                    2,
                ),
                "aimX": round(e.get("aim_x", 0.0), 2),
                "aimY": round(e.get("aim_y", 0.0), 2),
            }
            for e in room.enemies
        ],
        "shots": [
            {k: round(v, 1) if isinstance(v, float) else v for k, v in s.items()}
            for s in room.shots
        ],
    }


async def send_room(room: Room) -> None:
    payload = {"type": "room", "room": lobby_payload(room)}
    await send_to_players(room, payload)


async def send_state(room: Room) -> None:
    await send_to_players(room, {"type": "state", "state": game_payload(room)})


async def send_to_players(room: Room, payload: dict) -> None:
    stale: list[str] = []
    for player_id, player in room.players.items():
        if not player.ws:
            continue
        try:
            await player.ws.send_json(payload)
        except Exception:
            stale.append(player_id)
    for player_id in stale:
        player = room.players.get(player_id)
        if player:
            player.connected = False
            player.ws = None


def make_code() -> str:
    alphabet = string.ascii_uppercase.replace("I", "").replace("O", "") + "23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(5))
        if code not in ROOMS:
            return code


def room_or_404(code: str) -> Room:
    room = ROOMS.get(code.upper())
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена")
    return room


def new_player(
    name: str,
    player_id: str | None = None,
    index: int = 0,
    hero_class: str = "scout",
    skin: str = "ember",
) -> Player:
    hero_class = hero_class if hero_class in HEROES else "scout"
    skin = skin if skin in SKINS else "ember"
    hero = HEROES[hero_class]
    return Player(
        id=player_id or secrets.token_urlsafe(12),
        name=name.strip()[:20],
        x=500 + (index % 3) * 140,
        y=520 + (index // 3) * 75,
        hero_class=hero_class,
        skin=skin,
        max_health=hero["health"],
        health=hero["health"],
        speed=hero["speed"],
        fire_rate=hero["fire_rate"],
        revive_bonus=hero["revive"],
    )


def spawn_boss(room: Room) -> None:
    if room.boss_spawned:
        return
    room.boss_spawned = True
    hp = 18 + room.capacity * 4 + room.level * 2
    room.enemies.append({
        "id": secrets.token_hex(4), "x": WORLD_W / 2, "y": 190, "r": 30,
        "speed": 34 + room.level * 2, "hp": hp, "maxHp": hp,
        "type": 1, "shot": time.monotonic() + 1.5, "phase": 0, "boss": True,
        "windup_until": 0.0, "windup_duration": 0.72,
        "aim_x": 0.0, "aim_y": 0.0, "lunge_until": 0.0,
    })


def update_team_lives(room: Room) -> None:
    room.lives = sum(max(0, player.health) for player in room.players.values())


def harm_player(room: Room, player: Player, now: float) -> None:
    if player.downed or now < player.invincible_until:
        return
    player.health = max(0, player.health - 1)
    player.invincible_until = now + 1.0 + player.shield_bonus
    if player.health == 0:
        player.downed = True
        player.dx = player.dy = 0
        player.shooting = False
    update_team_lives(room)


def apply_relic(player: Player, relic: str) -> None:
    if relic == "fang":
        player.damage += 1
    elif relic == "feather":
        player.speed *= 1.12
    elif relic == "hourglass":
        player.fire_rate = max(0.08, player.fire_rate * 0.82)
    elif relic == "heart":
        player.max_health = min(8, player.max_health + 1)
        player.health = min(player.max_health, player.health + 1)
        player.downed = False
    elif relic == "sun":
        player.shield_bonus = min(1.5, player.shield_bonus + 0.4)
    elif relic == "rune":
        player.revive_bonus = min(3.0, player.revive_bonus + 0.3)
    player.relics.append(relic)
    combos = (
        ("storm", {"fang", "hourglass"}),
        ("windguard", {"feather", "sun"}),
        ("keeper", {"heart", "rune"}),
    )
    for name, parts in combos:
        if name in player.synergies or not parts.issubset(player.relics):
            continue
        player.synergies.append(name)
        if name == "windguard":
            player.speed *= 1.08
            player.shield_bonus = min(2.0, player.shield_bonus + 0.4)
        elif name == "keeper":
            player.max_health = min(8, player.max_health + 1)
            player.health = min(player.max_health, player.health + 1)
            player.revive_bonus = min(3.0, player.revive_bonus + 0.6)


async def game_loop(room: Room) -> None:
    last = time.monotonic()
    last_broadcast = 0.0
    while room.status in {"playing", "upgrade"}:
        room.ticks += 1
        now = time.monotonic()
        dt = min(0.1, max(0.01, now - last))
        last = now
        connected = [p for p in room.players.values() if p.connected]
        active = [p for p in connected if not p.downed]
        if room.status == "upgrade":
            if now - last_broadcast >= 0.08:
                await send_state(room)
                last_broadcast = now
            await asyncio.sleep(0.05)
            continue
        if not active:
            if connected and all(p.downed for p in connected):
                room.status = "finished"
                room.lives = 0
                await send_state(room)
                break
            await asyncio.sleep(0.1)
            continue

        for player in active:
            mag = math.hypot(player.dx, player.dy) or 1
            response = 1 - math.exp(-14 * dt)
            target_vx = player.dx / mag * player.speed
            target_vy = player.dy / mag * player.speed
            player.vx += (target_vx - player.vx) * response
            player.vy += (target_vy - player.vy) * response
            next_x = player.x + player.vx * dt
            next_y = player.y + player.vy * dt
            player.x = max(25, min(WORLD_W - 25, next_x))
            player.y = max(190, min(WORLD_H - 25, next_y))
            if player.x != next_x:
                player.vx = 0
            if player.y != next_y:
                player.vy = 0
            if (player.dx or player.dy) and not player.manual_aim:
                aim_length = math.hypot(player.dx, player.dy) or 1
                player.aim_x = player.dx / aim_length
                player.aim_y = player.dy / aim_length
                if player.dx:
                    player.face = 1 if player.dx > 0 else -1
            if player.shooting and now >= player.next_shot:
                dx, dy = player.aim_x, player.aim_y
                spread = (-0.16, 0.0, 0.16) if "storm" in player.synergies else (0.0,)
                for angle in spread:
                    cs, sn = math.cos(angle), math.sin(angle)
                    room.shots.append({
                        "id": secrets.token_hex(4), "x": player.x, "y": player.y,
                        "vx": (dx * cs - dy * sn) * 440,
                        "vy": (dx * sn + dy * cs) * 440,
                        "enemy": False, "life": 1.6, "damage": player.damage,
                    })
                player.next_shot = now + player.fire_rate

        if room.level % 5 == 0 and not room.boss_spawned:
            spawn_boss(room)

        spawn_interval = max(0.38, 1.5 - room.capacity * 0.105 - room.level * 0.035)
        enemy_limit = room.capacity * (2 + min(room.level // 3, 3))
        boss_alive = any(enemy.get("boss") for enemy in room.enemies)
        if not boss_alive and now >= room.next_spawn and len(room.enemies) < enemy_limit:
            side = random.randrange(4)
            x = -12 if side == 0 else WORLD_W + 12 if side == 1 else random.uniform(40, WORLD_W - 40)
            y = 120 if side == 2 else WORLD_H + 12 if side == 3 else random.uniform(145, WORLD_H - 55)
            room.enemies.append({
                "id": secrets.token_hex(4), "x": x, "y": y, "r": 12,
                "speed": 62 + room.capacity * 4 + room.level * 3,
                "hp": 2 if room.capacity == 6 or room.level >= 5 else 1,
                "type": random.randrange(2), "shot": now + random.uniform(2.4, 4.5),
                "phase": random.random() * 6.28,
                "windup_until": 0.0, "windup_duration": 0.62,
                "aim_x": 0.0, "aim_y": 0.0, "lunge_until": 0.0,
            })
            room.enemies[-1]["maxHp"] = room.enemies[-1]["hp"]
            room.next_spawn = now + spawn_interval

        for enemy in list(room.enemies):
            target = min(active, key=lambda p: math.hypot(p.x - enemy["x"], p.y - enemy["y"]))
            distance = math.hypot(target.x - enemy["x"], target.y - enemy["y"]) or 1
            melee = enemy.get("type") == 0 and not enemy.get("boss")
            winding = enemy.get("windup_until", 0.0)
            if melee:
                if winding > now:
                    pass
                elif enemy.get("lunge_until", 0.0) > now:
                    enemy["x"] += enemy.get("aim_x", 0.0) * enemy["speed"] * 3.8 * dt
                    enemy["y"] += enemy.get("aim_y", 0.0) * enemy["speed"] * 3.8 * dt
                else:
                    enemy["x"] += (target.x - enemy["x"]) / distance * enemy["speed"] * dt
                    enemy["y"] += (target.y - enemy["y"]) / distance * enemy["speed"] * dt
                if winding and now >= winding:
                    enemy["windup_until"] = 0.0
                    enemy["lunge_until"] = now + 0.22
                elif not winding and enemy.get("lunge_until", 0.0) <= now and now >= enemy["shot"] and distance < 112:
                    enemy["aim_x"] = (target.x - enemy["x"]) / distance
                    enemy["aim_y"] = (target.y - enemy["y"]) / distance
                    enemy["windup_duration"] = 0.56
                    enemy["windup_until"] = now + enemy["windup_duration"]
                    enemy["shot"] = now + 2.0
            else:
                if winding <= now:
                    enemy["x"] += (target.x - enemy["x"]) / distance * enemy["speed"] * dt
                    enemy["y"] += (target.y - enemy["y"]) / distance * enemy["speed"] * dt
                if winding and now >= winding:
                    vx, vy = enemy.get("aim_x", 0.0), enemy.get("aim_y", 0.0)
                    if enemy.get("boss"):
                        for spread in (-0.16, 0, 0.16):
                            cs, sn = math.cos(spread), math.sin(spread)
                            room.shots.append({"id": secrets.token_hex(4), "x": enemy["x"], "y": enemy["y"], "vx": (vx * cs - vy * sn) * 175, "vy": (vx * sn + vy * cs) * 175, "enemy": True, "life": 3.0})
                    else:
                        room.shots.append({"id": secrets.token_hex(4), "x": enemy["x"], "y": enemy["y"], "vx": vx * 160, "vy": vy * 160, "enemy": True, "life": 3.0})
                    enemy["windup_until"] = 0.0
                elif not winding and now >= enemy["shot"] and distance < 390:
                    enemy["aim_x"] = (target.x - enemy["x"]) / distance
                    enemy["aim_y"] = (target.y - enemy["y"]) / distance
                    enemy["windup_duration"] = 0.72 if enemy.get("boss") else 0.62
                    enemy["windup_until"] = now + enemy["windup_duration"]
                    enemy["shot"] = now + (1.7 if enemy.get("boss") else max(1.8, 3.1 - room.level * 0.08))
            distance = math.hypot(target.x - enemy["x"], target.y - enemy["y"]) or 1
            if distance < enemy["r"] + 12:
                harm_player(room, target, now)

        active = [p for p in connected if p.connected and not p.downed]
        for player in active:
            if player.revive:
                target = min((p for p in connected if p.downed and p.id != player.id), key=lambda p: math.hypot(p.x - player.x, p.y - player.y), default=None)
                if target and math.hypot(target.x - player.x, target.y - player.y) < 58:
                    target.revive_progress += dt * player.revive_bonus
                    if target.revive_progress >= 2.2:
                        target.downed = False
                        target.health = max(1, math.ceil(target.max_health / 2))
                        target.revive_progress = 0
                        target.invincible_until = now + 1.5
                        room.score += 150
                else:
                    for downed in connected:
                        downed.revive_progress = max(0, downed.revive_progress - dt * 0.4)

        active = [p for p in connected if p.connected and not p.downed]

        room.hazard_phase = room.ticks % 220
        if room.level >= 3 and 193 < room.hazard_phase < 204:
            for target in active:
                if 610 < target.y < 637 and 110 < target.x < WORLD_W - 110:
                    harm_player(room, target, now)

        for shot in list(room.shots):
            shot["x"] += shot["vx"] * dt
            shot["y"] += shot["vy"] * dt
            shot["life"] -= dt
            if shot["enemy"]:
                for target in active:
                    if math.hypot(shot["x"] - target.x, shot["y"] - target.y) < 15 and now >= target.invincible_until:
                        harm_player(room, target, now)
                        shot["life"] = 0
                        break
            else:
                for enemy in list(room.enemies):
                    if math.hypot(shot["x"] - enemy["x"], shot["y"] - enemy["y"]) < enemy["r"] + 4:
                        enemy["hp"] -= shot.get("damage", 1)
                        shot["life"] = 0
                        if enemy["hp"] <= 0:
                            room.enemies.remove(enemy)
                            room.kills += 1
                            boss = enemy.get("boss", False)
                            room.score += 1200 if boss else 100
                            if boss:
                                room.cache = {"x": enemy["x"], "y": enemy["y"], "kind": "boss"}
                            if room.kills % 8 == 0:
                                room.level += 1
                                room.boss_spawned = False
                                room.upgrade_options = random.sample(list(RELICS), 3)
                                room.pending_upgrades = {p.id for p in connected}
                                room.status = "upgrade"
                        break
        room.shots = [s for s in room.shots if s["life"] > 0 and -40 < s["x"] < WORLD_W + 40 and 70 < s["y"] < WORLD_H + 40]

        for player in active:
            if room.cache and math.hypot(player.x - room.cache["x"], player.y - room.cache["y"]) < 28:
                room.cache = None
                room.score += 300
                wounded = min((p for p in connected if p.health < p.max_health), key=lambda p: p.health, default=None)
                if wounded:
                    wounded.health += 1
                    wounded.downed = False
                    wounded.invincible_until = now + 1.5
                else:
                    player.damage += 1
        update_team_lives(room)
        if connected and all(p.downed for p in connected):
            room.status = "finished"
        if now - last_broadcast >= 0.08 or room.status == "finished":
            await send_state(room)
            last_broadcast = now
        await asyncio.sleep(0.05)


app = FastAPI(title="Temple Echo Rooms", version="1.0")


@app.get("/health")
async def health_check() -> dict:
    return {"ok": True, "rooms": len(ROOMS)}


@app.get("/api/lobbies")
async def list_lobbies() -> dict:
    now = time.time()
    for code, room in list(ROOMS.items()):
        if room.status != "playing" and now - room.created_at > 60 * 60:
            del ROOMS[code]
    return {"rooms": [lobby_payload(r) for r in ROOMS.values() if r.status == "waiting"]}


@app.post("/api/lobbies")
async def create_lobby(request: CreateRoom) -> dict:
    code = make_code()
    host = new_player(request.name, index=0, hero_class=request.hero_class, skin=request.skin)
    room = Room(code=code, capacity=request.capacity, host_id=host.id)
    room.players[host.id] = host
    ROOMS[code] = room
    return {"room": lobby_payload(room), "player_id": host.id}


@app.post("/api/lobbies/{code}/join")
async def join_lobby(code: str, request: JoinRoom) -> dict:
    room = room_or_404(code)
    if room.status != "waiting":
        raise HTTPException(status_code=409, detail="Игра уже началась")
    if request.player_id and request.player_id in room.players:
        player = room.players[request.player_id]
        return {"room": lobby_payload(room), "player_id": player.id}
    if len(room.players) >= room.capacity:
        raise HTTPException(status_code=409, detail="Комната заполнена")
    player = new_player(request.name, index=len(room.players), hero_class=request.hero_class, skin=request.skin)
    room.players[player.id] = player
    await send_room(room)
    return {"room": lobby_payload(room), "player_id": player.id}


@app.post("/api/lobbies/{code}/start")
async def start_lobby(code: str, request: RoomAction) -> dict:
    room = room_or_404(code)
    if request.player_id != room.host_id:
        raise HTTPException(status_code=403, detail="Начать игру может создатель комнаты")
    if room.status != "waiting":
        raise HTTPException(status_code=409, detail="Игра уже запущена")
    if len(room.players) < room.capacity or sum(p.connected for p in room.players.values()) < room.capacity:
        raise HTTPException(status_code=409, detail=f"Нужно собрать всех игроков: {len(room.players)}/{room.capacity}")
    room.status = "playing"
    room.level = 1
    room.score = 0
    room.kills = 0
    room.lives = 0
    room.boss_spawned = False
    room.cache = None
    room.upgrade_options.clear()
    room.pending_upgrades.clear()
    for player in room.players.values():
        player.dx = player.dy = player.vx = player.vy = 0
        player.manual_aim = False
        player.shooting = False
        player.health = player.max_health
        player.downed = False
        player.revive_progress = 0
        player.damage = 1
        player.relics.clear()
        player.synergies.clear()
    update_team_lives(room)
    room.enemies.clear()
    room.shots.clear()
    room.next_spawn = time.monotonic() + 0.7
    room.task = asyncio.create_task(game_loop(room))
    await send_state(room)
    return {"room": lobby_payload(room)}


@app.post("/api/lobbies/{code}/upgrade")
async def choose_relic(code: str, request: RelicAction) -> dict:
    room = room_or_404(code)
    if room.status != "upgrade":
        raise HTTPException(status_code=409, detail="Сейчас реликвию выбрать нельзя")
    player = room.players.get(request.player_id)
    if not player or request.player_id not in room.pending_upgrades:
        raise HTTPException(status_code=403, detail="Игрок уже выбрал реликвию")
    if request.relic not in room.upgrade_options or request.relic not in RELICS:
        raise HTTPException(status_code=400, detail="Неизвестная реликвия")
    apply_relic(player, request.relic)
    update_team_lives(room)
    room.pending_upgrades.discard(player.id)
    if not room.pending_upgrades:
        room.status = "playing"
        room.upgrade_options.clear()
        room.next_spawn = time.monotonic() + 0.8
    await send_state(room)
    return {"ok": True, "waiting": len(room.pending_upgrades)}


@app.post("/api/lobbies/{code}/leave")
async def leave_lobby(code: str, request: RoomAction) -> dict:
    room = room_or_404(code)
    player = room.players.pop(request.player_id, None)
    if not player:
        return {"ok": True}
    player.connected = False
    player.ws = None
    room.pending_upgrades.discard(player.id)
    update_team_lives(room)
    if player.id == room.host_id and room.players:
        room.host_id = next(iter(room.players))
    if not room.players:
        ROOMS.pop(room.code, None)
        room.status = "finished"
        if room.task and not room.task.done():
            room.task.cancel()
    else:
        if room.status == "upgrade" and not room.pending_upgrades:
            room.status = "playing"
            room.upgrade_options.clear()
        await send_room(room)
    return {"ok": True}


@app.websocket("/ws")
async def room_socket(websocket: WebSocket) -> None:
    room = ROOMS.get(websocket.query_params.get("code", "").upper())
    player_id = websocket.query_params.get("player_id", "")
    player = room.players.get(player_id) if room else None
    if not room or not player:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    player.ws = websocket
    player.connected = True
    player.vx = player.vy = 0
    await send_room(room)
    if room.status == "playing":
        await send_state(room)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "input" and room.status == "playing":
                player.dx = max(-1, min(1, float(message.get("dx", 0))))
                player.dy = max(-1, min(1, float(message.get("dy", 0))))
                player.shooting = bool(message.get("shoot", False))
                player.revive = bool(message.get("revive", False))
                player.manual_aim = bool(message.get("aimManual", False))
                if player.manual_aim:
                    aim_x = float(message.get("aimX", player.aim_x))
                    aim_y = float(message.get("aimY", player.aim_y))
                    aim_length = math.hypot(aim_x, aim_y)
                    if aim_length > 0.1:
                        player.aim_x = aim_x / aim_length
                        player.aim_y = aim_y / aim_length
            elif message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        player.connected = False
        player.ws = None
        player.dx = player.dy = 0
        player.vx = player.vy = 0
        player.shooting = False
        player.revive = False
        player.manual_aim = False
        room.pending_upgrades.discard(player.id)
        if room.status == "upgrade" and not room.pending_upgrades:
            room.status = "playing"
            room.upgrade_options.clear()
        if room.code in ROOMS:
            await send_room(room)


def leaderboard(day: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT name, score, level, kills FROM daily_scores WHERE day=? ORDER BY score DESC, level DESC, kills DESC LIMIT 10",
            (day,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/daily/challenge")
async def get_daily_challenge() -> dict:
    challenge = daily_challenge()
    return {**challenge, "leaderboard": leaderboard(challenge["day"])}


@app.get("/api/daily/leaderboard")
async def get_daily_leaderboard(day: str | None = None) -> dict:
    current_day = datetime.now(timezone.utc).date().isoformat()
    day = day or current_day
    return {"day": day, "leaderboard": leaderboard(day)}


@app.post("/api/daily/score")
async def submit_daily_score(request: DailyScore) -> dict:
    current_day = datetime.now(timezone.utc).date().isoformat()
    if request.day != current_day:
        raise HTTPException(status_code=409, detail="Испытание дня уже сменилось")
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO daily_scores(day, player_id, name, score, level, kills, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(day, player_id) DO UPDATE SET
                 name=excluded.name,
                 level=CASE WHEN excluded.score > daily_scores.score THEN excluded.level ELSE daily_scores.level END,
                 kills=CASE WHEN excluded.score > daily_scores.score THEN excluded.kills ELSE daily_scores.kills END,
                 score=MAX(daily_scores.score, excluded.score),
                 updated_at=excluded.updated_at""",
            (request.day, request.player_id, request.name.strip()[:20], request.score, request.level, request.kills, datetime.now(timezone.utc).isoformat()),
        )
    return {"day": request.day, "leaderboard": leaderboard(request.day)}
