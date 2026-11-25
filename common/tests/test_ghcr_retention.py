"""GHCR retention keeps every release, every recent build and every manifest a kept index needs."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import httpx

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
    assert ids(plan.delete) == {2}


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


@pytest.mark.parametrize("tag", ["sha-abc1234", "abc1234"])
def test_an_old_version_retagged_by_a_recent_commit_is_kept(tag: str) -> None:
    versions = [
        v(1, "cached", tags=("sha-0000000", tag)),
        v(2, "stale", tags=("sha-1111111",)),
    ]
    plan = r.plan_package(
        "p", versions, lambda d: [], NOW, recent_commits=frozenset({"abc1234"})
    )
    assert ids(plan.keep) == {1}
    assert ids(plan.delete) == {2}


def test_a_version_exactly_keep_days_old_is_kept() -> None:
    edge = v(1, "edge", created=NOW - timedelta(days=r.KEEP_DAYS))
    assert r.is_root(edge, NOW)
    assert not r.is_root(
        v(2, "older", created=NOW - timedelta(days=r.KEEP_DAYS, seconds=1)), NOW
    )


def test_children_in_reads_an_index_without_a_media_type() -> None:
    assert r.children_in({"schemaVersion": 2, "manifests": [{"digest": "a"}]}) == ["a"]


def row(id_: int) -> dict:
    return {
        "id": id_,
        "name": f"sha256:{id_:064x}",
        "created_at": "2026-08-01T00:00:00Z",
        "metadata": {"container": {"tags": []}},
    }


def ghcr_with(handler) -> r.Ghcr:
    return r.Ghcr("t", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_list_reads_every_page() -> None:
    rows = [row(i) for i in range(1, 251)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=rows[(page - 1) * 100 : page * 100])

    assert {x.id for x in ghcr_with(handler).versions("p")} == set(range(1, 251))


def test_two_lists_that_differ_stop_the_plan() -> None:
    calls = iter(range(100))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row(next(calls) + 1)])

    with pytest.raises(RuntimeError, match="two version lists differ"):
        ghcr_with(handler).versions("p")


def test_a_registry_error_on_a_kept_index_stops_the_plan() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ghcr.io" and request.url.path == "/token":
            return httpx.Response(200, json={"token": "x"})
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        ghcr_with(handler).children("p", "sha256:abc")


def test_recent_commits_reads_short_shas_and_refuses_an_empty_history() -> None:
    def runner(argv, **kwargs):
        out = "abc1234deadbeef\n0011223aabbcc\n" if argv[1] == "-C" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    assert r.recent_commits("truealpha", NOW, runner=runner) == frozenset(
        {"abc1234", "0011223"}
    )

    def empty(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="no main commit"):
        r.recent_commits("truealpha", NOW, runner=empty)


def test_apply_checks_the_reserve_before_the_first_delete() -> None:
    plan = r.Plan("p", delete=[v(1, "d1")])
    deleted, _ = r.apply_plan(
        plan,
        lambda p, i: pytest.fail("deleted below the reserve"),
        remaining=1999,
        reserved_calls=2000,
    )
    assert deleted == 0


def test_apply_deletes_oldest_first_within_one_batch_and_stops_at_the_reserve() -> None:
    plan = r.Plan(
        "p",
        delete=[v(i, f"d{i}", created=OLD + timedelta(days=i)) for i in (3, 1, 2, 4)],
    )
    calls: list[int] = []
    left = iter([2500, 1999])

    def delete(package: str, version_id: int) -> int:
        calls.append(version_id)
        return next(left)

    assert r.apply_plan(plan, delete, remaining=3000, reserved_calls=2000, batch=3) == (
        2,
        1999,
    )
    assert calls == [1, 2]


def test_each_batch_is_planned_again_from_a_fresh_list() -> None:
    plans = iter(
        [
            r.Plan("p", delete=[v(1, "a"), v(2, "b"), v(3, "c")]),
            # Between batches the release train tagged version 3; the fresh plan keeps it.
            r.Plan("p", delete=[]),
        ]
    )
    calls: list[int] = []

    def delete(package: str, version_id: int) -> int:
        calls.append(version_id)
        return 4000

    assert r.apply_package(lambda: next(plans), delete, remaining=4000, batch=2) == (
        2,
        True,
    )
    assert calls == [1, 2]
