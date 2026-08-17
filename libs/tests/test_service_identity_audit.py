from tools.service_identity_audit import ROOT, _owned_alert_catalogs, audit


def test_cross_plane_service_identity_contract_is_complete() -> None:
    assert audit() == []


def test_alert_catalog_discovery_excludes_tool_worktrees_and_nested_repos() -> None:
    relatives = [path.relative_to(ROOT) for path in _owned_alert_catalogs()]
    assert relatives
    assert all(
        relative.parts[0] not in {".claude", "repos", "oh-my-code-agent"}
        for relative in relatives
    )
