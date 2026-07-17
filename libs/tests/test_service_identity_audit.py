from tools.service_identity_audit import audit


def test_cross_plane_service_identity_contract_is_complete() -> None:
    assert audit() == []
