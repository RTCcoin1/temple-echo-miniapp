"""Small in-memory WebSocket lobby and co-op game server for Temple Echo."""
from __future__ import annotations

import asyncio
import math
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

ROOMS: dict[str, "Room"] = {}
CAPACITY_LABELS = {2: "ЛЁГКИЙ", 4: "СРЕДНИЙ", 6: "ХАРД"}
WORLD_W, WORLD_H = 1280, 810


@dataclass
class Player:
    id: str
    name: str
    x: float
    y: float
    connected: bool = False
    ws: WebSocket | None = None
    dx: float = 0
    dy: float = 0
    shooting: bool = False
    face: int = 1
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

    @property
    def difficulty(self) -> str:
        return CAPACITY_LABELS[self.capacity]


class CreateRoom(BaseModel):
    capacity: Literal[2, 4, 6]
    name: str = Field(min_length=1, max_length=20)


class JoinRoom(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    player_id: str | None = None


class RoomAction(BaseModel):
    player_id: str


def public_player(player: Player, room: Room) -> dict:
    return {
        "id": player.id,
        "name": player.name,
        "connected": player.connected,
        "host": player.id == room.host_id,
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
        "players": [
            {
                "id": p.id,
                "name": p.name,
                "x": round(p.x, 1),
                "y": round(p.y, 1),
                "face": p.face,
                "connected": p.connected,
            }
            for p in room.players.values()
        ],
        "enemies": [
            {k: round(v, 1) if isinstance(v, float) else v for k, v in e.items()}
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


def new_player(name: str, player_id: str | None = None, index: int = 0) -> Player:
    return Player(
        id=player_id or secrets.token_urlsafe(12),
        name=name.strip()[:20],
        x=500 + (index % 3) * 140,
        y=520 + (index // 3) * 75,
    )


async def game_loop(room: Room) -> None:
    last = time.monotonic()
    last_broadcast = 0.0
    while room.status == "playing":
        now = time.monotonic()
        dt = min(0.1, max(0.01, now - last))
        last = now
        active = [p for p in room.players.values() if p.connected]
        if not active:
            await asyncio.sleep(0.1)
            continue

        for player in active:
            mag = math.hypot(player.dx, player.dy) or 1
            player.x = max(25, min(WORLD_W - 25, player.x + player.dx / mag * 190 * dt))
            player.y = max(190, min(WORLD_H - 25, player.y + player.dy / mag * 190 * dt))
            if player.dx:
                player.face = 1 if player.dx > 0 else -1
            if player.shooting and now >= player.next_shot:
                target = min(room.enemies, key=lambda e: math.hypot(e["x"] - player.x, e["y"] - player.y), default=None)
                dx, dy = float(player.face), 0.0
                if target:
                    length = math.hypot(target["x"] - player.x, target["y"] - player.y) or 1
                    dx, dy = (target["x"] - player.x) / length, (target["y"] - player.y) / length
                room.shots.append({"id": secrets.token_hex(4), "x": player.x, "y": player.y, "vx": dx * 440, "vy": dy * 440, "enemy": False, "life": 1.6})
                player.next_shot = now + 0.22

        spawn_interval = max(0.38, 1.5 - room.capacity * 0.105 - room.level * 0.035)
        enemy_limit = room.capacity * (2 + min(room.level // 3, 3))
        if now >= room.next_spawn and len(room.enemies) < enemy_limit:
            side = random.randrange(4)
            x = -12 if side == 0 else WORLD_W + 12 if side == 1 else random.uniform(40, WORLD_W - 40)
            y = 120 if side == 2 else WORLD_H + 12 if side == 3 else random.uniform(145, WORLD_H - 55)
            room.enemies.append({
                "id": secrets.token_hex(4), "x": x, "y": y, "r": 12,
                "speed": 62 + room.capacity * 4 + room.level * 3,
                "hp": 2 if room.capacity == 6 or room.level >= 5 else 1,
                "type": random.randrange(2), "shot": now + random.uniform(2.4, 4.5),
                "phase": random.random() * 6.28,
            })
            room.next_spawn = now + spawn_interval

        for enemy in list(room.enemies):
            target = min(active, key=lambda p: math.hypot(p.x - enemy["x"], p.y - enemy["y"]))
            distance = math.hypot(target.x - enemy["x"], target.y - enemy["y"]) or 1
            enemy["x"] += (target.x - enemy["x"]) / distance * enemy["speed"] * dt
            enemy["y"] += (target.y - enemy["y"]) / distance * enemy["speed"] * dt
            if now >= enemy["shot"] and distance < 390:
                enemy["shot"] = now + max(1.2, 3.1 - room.level * 0.08)
                room.shots.append({"id": secrets.token_hex(4), "x": enemy["x"], "y": enemy["y"], "vx": (target.x - enemy["x"]) / distance * 160, "vy": (target.y - enemy["y"]) / distance * 160, "enemy": True, "life": 3.0})
            if distance < 24 and now >= target.invincible_until:
                room.lives -= 1
                target.invincible_until = now + 1.0

        for shot in list(room.shots):
            shot["x"] += shot["vx"] * dt
            shot["y"] += shot["vy"] * dt
            shot["life"] -= dt
            if shot["enemy"]:
                for target in active:
                    if math.hypot(shot["x"] - target.x, shot["y"] - target.y) < 15 and now >= target.invincible_until:
                        room.lives -= 1
                        target.invincible_until = now + 1.0
                        shot["life"] = 0
                        break
            else:
                for enemy in list(room.enemies):
                    if math.hypot(shot["x"] - enemy["x"], shot["y"] - enemy["y"]) < enemy["r"] + 4:
                        enemy["hp"] -= 1
                        shot["life"] = 0
                        if enemy["hp"] <= 0:
                            room.enemies.remove(enemy)
                            room.kills += 1
                            room.score += 100
                            if room.kills % 8 == 0:
                                room.level += 1
                        break
        room.shots = [s for s in room.shots if s["life"] > 0 and -40 < s["x"] < WORLD_W + 40 and 70 < s["y"] < WORLD_H + 40]

        if room.lives <= 0:
            room.lives = 0
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
    host = new_player(request.name, index=0)
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
    player = new_player(request.name, index=len(room.players))
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
    room.lives = {2: 4, 4: 5, 6: 6}[room.capacity]
    room.enemies.clear()
    room.shots.clear()
    room.next_spawn = time.monotonic() + 0.7
    room.task = asyncio.create_task(game_loop(room))
    await send_state(room)
    return {"room": lobby_payload(room)}


@app.post("/api/lobbies/{code}/leave")
async def leave_lobby(code: str, request: RoomAction) -> dict:
    room = room_or_404(code)
    player = room.players.pop(request.player_id, None)
    if not player:
        return {"ok": True}
    player.connected = False
    player.ws = None
    if player.id == room.host_id and room.players:
        room.host_id = next(iter(room.players))
    if not room.players:
        ROOMS.pop(room.code, None)
        room.status = "finished"
        if room.task and not room.task.done():
            room.task.cancel()
    else:
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
        player.shooting = False
        if room.code in ROOMS:
            await send_room(room)
