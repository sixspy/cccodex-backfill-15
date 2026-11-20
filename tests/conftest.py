import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import init_db
from app.main import create_app


@pytest_asyncio.fixture
async def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest_asyncio.fixture
async def client(db_path):
    os.environ["DATABASE_PATH"] = str(db_path)
    init_db(str(db_path))
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


@pytest_asyncio.fixture
async def authed(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return client
