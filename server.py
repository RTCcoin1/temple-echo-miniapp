"""Railway entry point: FastAPI room server plus the existing Telegram bot."""
import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from bot import TOKEN, run_bot
from room_server import app

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@asynccontextmanager
async def lifespan(_app):
    bot_task = asyncio.create_task(run_bot()) if TOKEN else None
    try:
        yield
    finally:
        if bot_task:
            bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bot_task


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
