"""Tests for atomic service onboarding and offboarding saga."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from invoke import Context
import tools.service_onboard as sob
from tools.service_onboard import (
    _clean_ident,
    check_op_signed_in,
    get_or_create_db_credentials,
    onboard_service,
    provision_vault_approle,
    sync_vault_secrets,
)
from tools.service_offboard import offboard_service


def test_clean_ident():
    assert _clean_ident("demo-app") == "demo_app"
    assert _clean_ident("My-Cool-Service-1") == "my_cool_service_1"
    assert _clean_ident("already_clean") == "already_clean"


def test_check_op_signed_in():
    mock_runner_ok = MagicMock(return_value=MagicMock(returncode=0))
    assert check_op_signed_in(mock_runner_ok) is True

    mock_runner_fail = MagicMock(return_value=MagicMock(returncode=1))
    assert check_op_signed_in(mock_runner_fail) is False

    mock_runner_err = MagicMock(side_effect=OSError("not found"))
    assert check_op_signed_in(mock_runner_err) is False


def test_onboard_preflight_fails_closed_without_op():
    mock_runner_fail = MagicMock(return_value=MagicMock(returncode=1))
    with pytest.raises(RuntimeError, match="1Password CLI not signed in"):
        onboard_service(
            service="test-service",
            op_runner=mock_runner_fail,
        )


def test_get_or_create_db_credentials_reuse_existing():
    fake_backend = MagicMock()
    fake_backend.read.return_value = {
        "username": "existing_user",
        "password": "existing_secret_password_123",
        "database": "existing_db",
    }
    password, created = get_or_create_db_credentials(
        "apps",
        "production",
        "demo",
        "apps_demo_db",
        "apps_demo_user",
        op_backend=fake_backend,
    )
    assert password == "existing_secret_password_123"
    assert created is False
    fake_backend.write.assert_not_called()


def test_get_or_create_db_credentials_generate_new():
    fake_backend = MagicMock()
    fake_backend.read.return_value = {}
    password, created = get_or_create_db_credentials(
        "apps",
        "production",
        "demo",
        "apps_demo_db",
        "apps_demo_user",
        op_backend=fake_backend,
    )
    assert len(password) == 32
    assert created is True
    fake_backend.write.assert_called_once()
    saved = fake_backend.write.call_args[0][1]
    assert saved["password"] == password
    assert saved["username"] == "apps_demo_user"
    assert saved["database"] == "apps_demo_db"


def test_provision_vault_approle():
    mock_c = MagicMock(spec=Context)

    # Responses for: 1. policy write, 2. role write, 3. read role-id, 4. write secret-id
    res_ok = MagicMock(ok=True)
    res_role_id = MagicMock(
        ok=True, stdout=json.dumps({"data": {"role_id": "mock-role-uuid"}})
    )
    res_secret_id = MagicMock(
        ok=True, stdout=json.dumps({"data": {"secret_id": "mock-secret-uuid"}})
    )

    mock_c.run.side_effect = [res_ok, res_ok, res_role_id, res_secret_id]

    role_name, role_id, secret_id = provision_vault_approle(
        mock_c,
        "apps",
        "production",
        "my_app",
        "https://vault.test",
        "root-token-xyz",
    )

    assert role_name == "apps-production-my_app"
    assert role_id == "mock-role-uuid"
    assert secret_id == "mock-secret-uuid"
    assert mock_c.run.call_count == 4


def test_sync_vault_secrets():
    mock_vault_backend = MagicMock()
    sync_vault_secrets(
        "apps",
        "production",
        "demo",
        "https://vault.test",
        "mock-token",
        {"KEY": "VAL"},
        vault_backend=mock_vault_backend,
    )
    mock_vault_backend.write.assert_called_once_with(
        "apps/production/demo", {"KEY": "VAL"}
    )


def test_onboard_service_end_to_end():
    mock_c = MagicMock(spec=Context)
    mock_runner_ok = MagicMock(return_value=MagicMock(returncode=0))
    mock_op_backend = MagicMock()
    mock_op_backend.read.return_value = {}
    mock_vault_backend = MagicMock()

    mock_dokploy = MagicMock()
    mock_dokploy.find_compose_by_name.return_value = {
        "composeId": "comp-123",
        "name": "demo_app",
    }

    res_ok = MagicMock(ok=True)
    res_role_id = MagicMock(ok=True, stdout=json.dumps({"data": {"role_id": "r-123"}}))
    res_secret_id = MagicMock(
        ok=True, stdout=json.dumps({"data": {"secret_id": "s-456"}})
    )
    mock_c.run.side_effect = [res_ok, res_ok, res_role_id, res_secret_id]

    with patch.object(sob, "_load_postgres_tasks") as mock_load_pg:
        mock_pg = MagicMock()
        mock_load_pg.return_value = mock_pg

        summary = onboard_service(
            c=mock_c,
            service="demo-app",
            project="apps",
            env="production",
            db="postgres",
            db_connection_limit=8,
            op_runner=mock_runner_ok,
            op_backend=mock_op_backend,
            vault_backend=mock_vault_backend,
            vault_token="test-vault-token",
            dokploy_client=mock_dokploy,
        )

        # Assertions
        assert summary["service"] == "demo_app"
        assert summary["project"] == "apps"
        assert summary["env"] == "production"
        assert summary["db"]["database"] == "apps_demo_app_db"
        assert summary["db"]["user"] == "apps_demo_app_user"
        assert summary["db"]["connection_limit"] == 8
        assert "postgresql://apps_demo_app_user:" in summary["db"]["database_url"]
        assert summary["vault_approle"]["role_id"] == "r-123"
        assert summary["dokploy_env_updated"] is True

        # Check postgres ensure_user and ensure_database were called
        mock_pg.ensure_user.assert_called_once()
        _, kwargs = mock_pg.ensure_user.call_args
        assert kwargs["username"] == "apps_demo_app_user"
        assert kwargs["database"] == "apps_demo_app_db"
        assert kwargs["connection_limit"] == 8
        mock_pg.ensure_database.assert_called_once_with(
            mock_c, name="apps_demo_app_db", owner="apps_demo_app_user"
        )
        mock_pg.grant_database.assert_called_once_with(
            mock_c, username="apps_demo_app_user", database="apps_demo_app_db"
        )

        # Check dokploy env update was invoked
        mock_dokploy.update_compose_env.assert_called_once_with(
            "comp-123",
            env_vars={
                "VAULT_ROLE_ID": "r-123",
                "VAULT_SECRET_ID": "s-456",
                "VAULT_ADDR": "https://vault.zitian.party",
                "ENV": "production",
            },
        )


def test_offboard_service_default_locks_db_and_removes_approle():
    mock_c = MagicMock()
    mock_runner_ok = MagicMock(return_value=MagicMock(returncode=0))
    mock_dokploy = MagicMock()
    mock_dokploy.find_compose_by_name.return_value = {
        "composeId": "c-999",
        "name": "demo_app",
    }

    summary = offboard_service(
        c=mock_c,
        service="demo-app",
        project="apps",
        env="production",
        purge_data=False,
        op_runner=mock_runner_ok,
        vault_token="test-vault-token",
        dokploy_client=mock_dokploy,
    )

    assert summary["dokploy_removed"] is True
    assert summary["vault_approle_removed"] is True
    assert summary["db_locked"] is True
    assert summary["db_purged"] is False

    mock_dokploy.delete_compose.assert_called_once_with("c-999", delete_volumes=False)
    # Check that NOLOGIN SQL was run on postgres
    assert any("NOLOGIN" in str(call_args) for call_args in mock_c.run.call_args_list)


def test_offboard_service_purge_data():
    mock_c = MagicMock()
    mock_runner_ok = MagicMock(return_value=MagicMock(returncode=0))
    mock_dokploy = MagicMock()
    mock_dokploy.find_compose_by_name.return_value = {
        "composeId": "c-999",
        "name": "demo_app",
    }

    summary = offboard_service(
        c=mock_c,
        service="demo-app",
        project="apps",
        env="production",
        purge_data=True,
        op_runner=mock_runner_ok,
        vault_token="test-vault-token",
        dokploy_client=mock_dokploy,
    )

    assert summary["db_purged"] is True
    mock_dokploy.delete_compose.assert_called_once_with("c-999", delete_volumes=True)
    # Check that DROP DATABASE was run
    assert any(
        "DROP DATABASE" in str(call_args) for call_args in mock_c.run.call_args_list
    )
