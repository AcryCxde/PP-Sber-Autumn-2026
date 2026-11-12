"""Rollout switches never remove enforcement or resurrect legacy management."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import main
from backend.models import User
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import make_app
from backend.tests.test_product_api import FakeClaude


@pytest.mark.parametrize("management_enabled", [False, True])
def test_management_switch_never_disables_frozen_user_enforcement(tmp_path, monkeypatch, management_enabled):
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = management_enabled
    with TestClient(app) as client:
        sign_in(client)
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        with client.websocket_connect("/ws") as socket:
            socket.send_text("synthetic blocked start")
            assert socket.receive_json()["code"] == "ai_access_frozen"
        assert fake.prompts == []
        assert client.get("/api/auth/me").status_code == 200


def test_legacy_read_contracts_remain_protected_during_migration(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        paths = ["/api/admin/overview?days=7", "/api/admin/users"]
        assert [client.get(path).status_code for path in paths] == [401, 401]
        account = sign_in(client)
        assert [client.get(path).status_code for path in paths] == [403, 403]
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, account["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        overview = client.get(paths[0])
        users = client.get(paths[1])
        assert overview.status_code == users.status_code == 200
        assert "totals" in overview.json()
        assert isinstance(users.json(), list)
