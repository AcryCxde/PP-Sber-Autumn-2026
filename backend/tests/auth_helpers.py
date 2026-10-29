from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.db import Database
from backend.models import User


PASSWORD = "correct horse battery"


def sign_in(client: TestClient, email: str = "alpha@example.com") -> dict:
    """Регистрирует пользователя; cookie сессии остаётся в клиенте."""
    response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


async def make_user(database: Database, email: str = "alpha@example.com") -> str:
    async with database.session() as session:
        user = User(email=email, password_hash=hash_password(PASSWORD))
        session.add(user)
        await session.commit()
        return user.id
