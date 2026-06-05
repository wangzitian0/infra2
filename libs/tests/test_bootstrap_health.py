"""Bootstrap service health contract tests."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]

CRITICAL_BOOTSTRAP_SERVICES = {
    "bootstrap/04.1password/compose.yaml": {
        "op-connect-api": {
            "healthcheck_contains": [
                "/usr/local/bin/op-connect-healthcheck",
                "sqlite:ACTIVE",
                "sync:ACTIVE",
            ],
        },
        "op-connect-sync": {
            "healthcheck_contains": [
                "/usr/local/bin/op-connect-healthcheck",
                "sync:ACTIVE",
            ],
        },
    },
    "bootstrap/05.vault/compose.yaml": {
        "vault": {
            "healthcheck_contains": [
                "vault",
                "status",
                "-address=http://127.0.0.1:8200",
            ],
        },
        "unsealer": {
            "healthcheck_contains": ["python", "unsealer.py", "health"],
        },
    },
}


def _compose(path: str) -> dict:
    with (ROOT / path).open() as handle:
        return yaml.safe_load(handle)


def _healthcheck_test(service: dict) -> str:
    test = service.get("healthcheck", {}).get("test", [])
    if isinstance(test, str):
        return test
    return " ".join(str(part) for part in test)


def _assert_pinned_image(service_name: str, service: dict):
    image = service.get("image")
    build = service.get("build")

    assert image or build, f"{service_name} must declare an image or build"

    if image:
        assert ":latest" not in image, f"{service_name} must not use latest image tags"
        assert "${" not in image, (
            f"{service_name} image must be pinned, not env-substituted"
        )
        assert ":" in image, f"{service_name} image must include an explicit tag"

    if build:
        args = build.get("args", {}) if isinstance(build, dict) else {}
        if "1password" in service_name:
            assert args.get("OP_CONNECT_VERSION") == "1.8.2"


def test_critical_bootstrap_services_have_health_restart_logging_and_pinned_images():
    for compose_path, services in CRITICAL_BOOTSTRAP_SERVICES.items():
        compose = _compose(compose_path)
        compose_services = compose["services"]

        for service_name, expected in services.items():
            service = compose_services[service_name]

            assert service.get("restart") in {"always", "unless-stopped"}
            assert "healthcheck" in service
            assert service.get("logging", {}).get("driver") == "json-file"
            assert service.get("logging", {}).get("options", {}).get("max-size")
            assert service.get("logging", {}).get("options", {}).get("max-file")

            _assert_pinned_image(service_name, service)

            healthcheck = _healthcheck_test(service)
            for expected_part in expected["healthcheck_contains"]:
                assert expected_part in healthcheck
