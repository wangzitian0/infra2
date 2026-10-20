"""Shared test fakes for secrets and Dokploy client doubles (#713 Batch B-6)."""

from __future__ import annotations

from typing import Any


class FakeSecrets:
    """In-memory SecretsResolver double shared across CLI and deployer tests."""

    def __init__(
        self, values: dict[str, Any] | None = None, set_result: bool = True
    ) -> None:
        self.values: dict[str, Any] = dict(values or {})
        self.set_result = set_result
        self.set_calls: list[tuple[str, Any]] = []

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any) -> bool:
        self.values[key] = value
        self.set_calls.append((key, value))
        return self.set_result

    def get_all(self) -> dict[str, Any]:
        return self.values


class FakeDokployLookup:
    """Records find_compose_by_name calls and returns a scripted composeId."""

    def __init__(
        self, compose_ids: dict[tuple[str, str, str], str | None]
    ) -> None:
        self._compose_ids = compose_ids
        self.calls: list[tuple[str, str, str]] = []

    def find_compose_by_name(
        self,
        name: str,
        project_name: str | None = None,
        env_name: str | None = None,
    ) -> dict[str, str] | None:
        key = (name, project_name or "", env_name or "")
        self.calls.append(key)
        if key not in self._compose_ids:
            raise AssertionError(f"unscripted find_compose_by_name call: {key}")
        live_id = self._compose_ids[key]
        if live_id is None:
            return None
        return {"composeId": live_id, "name": name}


class FakeDokployDeployments:
    """Scripted deployment snapshot double for compose rollout polling tests."""

    def __init__(
        self,
        deployment_snapshots: list[list[dict[str, Any]]],
        *,
        compose_id: str = "compose-1",
    ) -> None:
        self.compose_id = compose_id
        self.deployment_snapshots = list(deployment_snapshots)
        self.deploy_calls = 0
        self.redeploy_calls = 0

    def deploy_compose(self, compose_id: str) -> dict[str, Any]:
        assert compose_id == self.compose_id
        self.deploy_calls += 1
        return {"ok": True}

    def redeploy_compose(self, compose_id: str) -> dict[str, Any]:
        assert compose_id == self.compose_id
        self.redeploy_calls += 1
        return {"ok": True}

    def get_compose(self, compose_id: str) -> dict[str, Any]:
        assert compose_id == self.compose_id
        if self.deployment_snapshots:
            return {"deployments": self.deployment_snapshots.pop(0)}
        return {"deployments": []}
