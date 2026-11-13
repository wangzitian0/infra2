"""Falsifiable coverage for the shared-layer size telemetry probe (#193)."""

from __future__ import annotations

import json

from tools.shared_layer_size import collect, main


def _build_workspace(root) -> None:  # type: ignore[no-untyped-def]
    def put(rel: str, text: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    body = "import os\n\n" + "".join(f"line_{i} = {i}\n" for i in range(300))
    # one comment line differs: near-duplicate above the bar, not an exact copy
    body_variant = body + "# truealpha note\n"
    other = "import os\n\n" + "".join(f"other_{i} = {i}\n" for i in range(300))
    dup = "MARKER = 'dup'\n" + "x = 2\n" * 20
    put(
        "repos/infra2-sdk/src/infra2_sdk/alpha.py",
        "VALUE = 1\n" + "pad_a = 'x' * 40\n" * 30,
    )
    put("common/sub/shared_impl.py", body)
    put("repos/truealpha/common/shared_impl.py", body_variant)
    # a divergent same-name pair: near-zero line overlap, below the bar
    put("common/sub/divergent.py", other)
    put("repos/truealpha/common/divergent.py", other.replace("other_", "variant_"))
    # exact duplicate pair across scopes
    put("common/sub/dup_one.py", dup)
    put("repos/truealpha/common/dup_two.py", dup)
    # excluded content: tests and pycache must not enter the probe
    put("common/tests/test_x.py", "assert True\n")
    put("common/sub/__pycache__/junk.py", "JUNK = 1\n")


def test_collect_excludes_tests_and_pycache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _build_workspace(tmp_path)
    names = {p.name for p in collect(tmp_path, ["common"])}
    assert names == {"shared_impl.py", "divergent.py", "dup_one.py"}


def test_main_reports_duplicates_and_near_duplicates(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    _build_workspace(tmp_path)
    code = main(["--root", str(tmp_path), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)

    sdk = report["scopes"]["sdk"]
    assert sdk["files"] == 1 and sdk["raw_bytes"] > 0

    dups = report["exact_duplicate_clusters"]
    assert len(dups) == 1, dups
    assert dups[0]["waste_bytes"] == len("MARKER = 'dup'\n" + "x = 2\n" * 20)
    assert any(p.endswith("dup_one.py") for p in dups[0]["paths"])

    assert report["near_duplicate_count"] == 1, report["near_duplicates_top"]
    pair = report["near_duplicates_top"][0]
    assert pair["similarity"] >= 0.9
    assert pair["a"] == "common/sub/shared_impl.py"
    assert pair["b"] == "repos/truealpha/common/shared_impl.py"


def test_main_survives_missing_scopes(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["--root", str(tmp_path), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert all(s["files"] == 0 for s in report["scopes"].values())
    assert report["exact_duplicate_clusters"] == []
