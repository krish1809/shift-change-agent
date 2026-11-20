from __future__ import annotations

import httpx

from app.main import build_app
from app.settings import Settings


async def test_health_endpoint_responds_ok() -> None:
    app = build_app(Settings(env="test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-correlation-id"]
