"""GHCR retention keeps every release, every recent build and every manifest a kept index needs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.deploy import ghcr_retention as r

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=90)


def v(
    id_: int, digest: str, *, tags: tuple[str, ...] = (), created: datetime = OLD
) -> r.Version:
    return r.Version(id=id_, digest=digest, created_at=created, tags=tags)


def ids(versions: list[r.Version]) -> set[int]:
    return {x.id for x in versions}


def test_a_release_index_keeps_its_untagged_children_and_an_old_sha_image_goes() -> (
    None
):
    versions = [
        v(1, "idx-release", tags=("v0.2.140", "sha-aaaaaaa")),
        v(2, "plat-release"),
        v(3, "att-release"),
        v(4, "idx-old", tags=("sha-bbbbbbb",)),
        v(5, "plat-old"),
    ]
    children = {"idx-release": ["plat-release", "att-release"], "idx-old": ["plat-old"]}
    plan = r.plan_package("p", versions, lambda d: children.get(d, []), NOW)
    assert ids(plan.keep) == {1, 2, 3}
    assert ids(plan.delete) == {4, 5}


def test_a_recent_build_and_latest_are_kept() -> None:
    versions = [
        v(1, "recent", tags=("sha-ccccccc",), created=NOW - timedelta(days=29)),
        v(2, "latest-idx", tags=("latest",)),
        v(3, "stale", tags=("sha-ddddddd",), created=NOW - timedelta(days=31)),
    ]
    plan = r.plan_package("p", versions, lambda d: [], NOW)
    assert ids(plan.keep) == {1, 2}
    assert ids(plan.delete) == {3}


@pytest.mark.parametrize("tag", ["v1.2", "v1.2.3-rc1", "release-1.2.3", "1.2.3"])
def test_a_tag_that_is_not_a_release_tag_does_not_protect(tag: str) -> None:
    plan = r.plan_package("p", [v(1, "d", tags=(tag,))], lambda d: [], NOW)
    assert ids(plan.delete) == {1}


def test_a_child_shared_with_a_deleted_index_stays_while_a_kept_index_names_it() -> (
    None
):
    versions = [v(1, "kept", tags=("v1.0.0",)), v(2, "gone-idx"), v(3, "shared")]
    children = {"kept": ["shared"], "gone-idx": ["shared"]}
    plan = r.plan_package("p", versions, lambda d: children.get(d, []), NOW)
    assert ids(plan.keep) == {1, 3}


def test_children_in_reads_index_entries_and_none_from_an_image_manifest() -> None:
    index = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{"digest": "a"}, {"digest": "b"}],
    }
    image = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [{"digest": "x"}],
    }
    assert r.children_in(index) == ["a", "b"]
    assert r.children_in(image) == []


class FakeGhcr:
    def __init__(
        self,
        listings: list[list[r.Version]],
        children: dict[str, list[str]],
        missing: set[str],
    ):
        self.listings = listings
        self.children_map = children
        self.missing = missing

    def versions(self, package: str) -> list[r.Version]:
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]

    def children(self, package: str, digest: str) -> list[str]:
        if digest in self.missing:
            raise r.ManifestGone(digest)
        return self.children_map.get(digest, [])


def test_a_manifest_that_vanished_from_the_list_is_skipped() -> None:
    first = [v(1, "release", tags=("v1.0.0",)), v(2, "replaced", created=NOW)]
    fake = FakeGhcr([first, [first[0]]], {}, {"replaced"})
    plan = r.build_plan(fake, "p", NOW)  # type: ignore[arg-type]
    assert ids(plan.keep) == {1}
    assert plan.delete == []


def test_a_listed_version_without_a_manifest_stops_the_plan() -> None:
    listed = [v(1, "release-idx", tags=("v1.0.0",)), v(2, "child")]
    fake = FakeGhcr([listed], {}, {"release-idx"})
    with pytest.raises(RuntimeError, match="have no manifest"):
        r.build_plan(fake, "p", NOW)  # type: ignore[arg-type]


def test_apply_deletes_oldest_first_and_stops_at_the_call_reserve() -> None:
    plan = r.Plan(
        "p", delete=[v(i, f"d{i}", created=OLD + timedelta(days=i)) for i in (3, 1, 2)]
    )
    calls: list[int] = []
    left = iter([2500, 1999, 1000])

    def delete(package: str, version_id: int) -> int:
        calls.append(version_id)
        return next(left)

    assert r.apply_plan(plan, delete, reserved_calls=2000) == 2
    assert calls == [1, 2]
