"""The production lock: the facts the merge gate reads, and how it reads them (#1138)."""

from __future__ import annotations

import json

import pytest

from libs.gate import client, production_contract
from libs.gate.production_lock import (
    REQUIRED_POLICIES,
    LockFacts,
    lock_failures,
    read_lock_facts,
)
from libs.gate.types import WORKFLOW_PREFIX

REPO = "wangzitian0/infra2"
HEAD = "feedfacefeedface"
ENVIRONMENT_PATH = f"repos/{REPO}/environments/production"
POLICIES_PATH = f"{ENVIRONMENT_PATH}/deployment-branch-policies?per_page=100"
CI_WORKFLOW = f"{WORKFLOW_PREFIX}infra-ci.yml"


def _content(name: str) -> str:
    """The contents API path of a workflow file at the head."""
    return f"repos/{REPO}/contents/{WORKFLOW_PREFIX}{name}?ref={HEAD}"


# The shape GitHub returned live on 2026-10-08, without the fields the gate does not read.
LIVE_ENVIRONMENT = {
    "name": "production",
    "can_admins_bypass": False,
    "protection_rules": [
        {
            "type": "required_reviewers",
            "prevent_self_review": False,
            "reviewers": [{"type": "User", "reviewer": {"login": "wangzitian0"}}],
        },
        {"type": "branch_policy"},
    ],
    "deployment_branch_policy": {
        "protected_branches": False,
        "custom_branch_policies": True,
    },
}
LIVE_POLICIES = {
    "total_count": 2,
    "branch_policies": [
        {"name": "main", "type": "branch"},
        {"name": "v*", "type": "tag"},
    ],
}

HOLDS = LockFacts(
    reviewers=1,
    can_admins_bypass=False,
    custom_policies=True,
    policies=REQUIRED_POLICIES,
)


class _Api:
    """Canned `gh api` answers by path; records every path it was asked for."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, argv):
        assert argv[0] == "api", argv
        path = argv[-1]
        self.paths.append(path)
        if path not in self.answers:  # gh reports a missing path as a failed call
            raise RuntimeError(f"gh api {path}: HTTP 404")
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


def _live(**environment_overrides) -> _Api:
    environment = {**LIVE_ENVIRONMENT, **environment_overrides}
    return _Api({ENVIRONMENT_PATH: environment, POLICIES_PATH: LIVE_POLICIES})


# --- lock_failures: one reason per false fact --------------------------------------


def test_the_lock_holds_when_every_fact_is_true():
    assert lock_failures(HOLDS) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {"reviewers": 0},
            "the production environment has no required reviewer",
        ),
        (
            {"can_admins_bypass": True},
            "administrators can bypass the production environment "
            "(can_admins_bypass=True)",
        ),
        (
            {"can_admins_bypass": None},
            "administrators can bypass the production environment "
            "(can_admins_bypass=None)",
        ),
        (
            {"custom_policies": False},
            "the production environment does not limit deployments to custom "
            "branch and tag policies",
        ),
        (
            {"policies": frozenset({("v*", "tag")})},
            "the production environment has no deployment policy for branch 'main'",
        ),
        (
            {"policies": frozenset({("main", "branch")})},
            "the production environment has no deployment policy for tag 'v*'",
        ),
        (
            {"policies": REQUIRED_POLICIES | {("*", "branch")}},
            "the production environment also accepts deployments from branch '*'",
        ),
    ],
    ids=[
        "no-reviewer",
        "admins-bypass",
        "bypass-unknown",
        "no-custom-policies",
        "no-main",
        "no-tag",
        "extra-branch",
    ],
)
def test_each_false_fact_is_reported_alone(change, expected):
    facts = LockFacts(**{**HOLDS.__dict__, **change})
    assert lock_failures(facts) == [expected]


def test_an_unreadable_environment_is_one_failure_that_names_the_cause():
    facts = LockFacts(error="RuntimeError: HTTP 502")
    assert lock_failures(facts) == [
        "the production environment could not be read (RuntimeError: HTTP 502)"
    ]


def test_an_error_fails_closed_even_beside_facts_that_look_true():
    assert lock_failures(LockFacts(**{**HOLDS.__dict__, "error": "x"})) != []


# --- read_lock_facts: GitHub's shape, and every way it can be wrong ----------------


def test_the_live_shape_reads_as_a_lock_that_holds():
    api = _live()
    facts = read_lock_facts(REPO, gh=api)
    assert facts == HOLDS
    assert lock_failures(facts) == []
    assert api.paths == [ENVIRONMENT_PATH, POLICIES_PATH]


def test_teams_and_users_count_and_other_entries_do_not():
    rules = [
        {
            "type": "required_reviewers",
            "reviewers": [
                {"type": "User"},
                {"type": "Team"},
                {"type": "Bot"},
                "not-an-object",
            ],
        }
    ]
    assert read_lock_facts(REPO, gh=_live(protection_rules=rules)).reviewers == 2


def test_a_reviewer_rule_with_no_reviewers_is_no_reviewer():
    rules = [{"type": "required_reviewers", "reviewers": []}]
    facts = read_lock_facts(REPO, gh=_live(protection_rules=rules))
    assert facts.error == "" and facts.reviewers == 0
    assert "the production environment has no required reviewer" in lock_failures(facts)


def test_an_environment_with_no_branch_limit_skips_the_policy_read():
    api = _live(deployment_branch_policy=None)
    facts = read_lock_facts(REPO, gh=api)
    assert facts.error == ""
    assert not facts.custom_policies and facts.policies == frozenset()
    assert api.paths == [ENVIRONMENT_PATH]
    assert len(lock_failures(facts)) == 3  # no custom policies, no main, no v*


@pytest.mark.parametrize(
    "answers",
    [
        {ENVIRONMENT_PATH: "not json"},
        {ENVIRONMENT_PATH: "[]"},
        {ENVIRONMENT_PATH: RuntimeError("gh api: HTTP 404")},
        {ENVIRONMENT_PATH: LIVE_ENVIRONMENT, POLICIES_PATH: "not json"},
        {ENVIRONMENT_PATH: LIVE_ENVIRONMENT, POLICIES_PATH: RuntimeError("HTTP 502")},
        {ENVIRONMENT_PATH: LIVE_ENVIRONMENT, POLICIES_PATH: {"total_count": 2}},
        {
            ENVIRONMENT_PATH: LIVE_ENVIRONMENT,
            POLICIES_PATH: {**LIVE_POLICIES, "total_count": 3},
        },
        {
            ENVIRONMENT_PATH: LIVE_ENVIRONMENT,
            POLICIES_PATH: {"total_count": 1, "branch_policies": [{"name": "main"}]},
        },
        {
            ENVIRONMENT_PATH: {
                **LIVE_ENVIRONMENT,
                "protection_rules": [{"type": "required_reviewers"}],
            }
        },
        {ENVIRONMENT_PATH: {**LIVE_ENVIRONMENT, "protection_rules": "x"}},
        {ENVIRONMENT_PATH: {**LIVE_ENVIRONMENT, "deployment_branch_policy": "x"}},
    ],
    ids=[
        "environment-not-json",
        "environment-not-object",
        "environment-runner-raises",
        "policies-not-json",
        "policies-runner-raises",
        "policies-list-missing",
        "policies-partial",
        "policy-without-type",
        "reviewer-rule-without-list",
        "rules-not-a-list",
        "branch-policy-not-object",
    ],
)
def test_an_unreadable_answer_sets_the_error(answers):
    facts = read_lock_facts(REPO, gh=_Api(answers))
    assert facts.error
    assert facts.reviewers == 0 and facts.can_admins_bypass is None
    assert len(lock_failures(facts)) == 1


@pytest.mark.parametrize(
    "key", ["can_admins_bypass", "protection_rules", "deployment_branch_policy"]
)
def test_a_missing_key_sets_the_error(key):
    environment = {k: v for k, v in LIVE_ENVIRONMENT.items() if k != key}
    facts = read_lock_facts(REPO, gh=_Api({ENVIRONMENT_PATH: environment}))
    assert key in facts.error


def test_a_runner_that_raises_anything_is_an_error_not_a_crash():
    def broken(argv):
        raise ValueError("unexpected")

    facts = read_lock_facts(REPO, gh=broken)
    assert facts.error == "ValueError: unexpected"


# --- collect: when the gate reads the lock, and what it reads -----------------------

WORKFLOW = "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"


class _CollectGh(_Api):
    """Answers for `collect` on one open PR that changes `files`."""

    def __init__(self, files, *, workflows=None, environment=LIVE_ENVIRONMENT):
        self.files = files
        workflows = {"ci.yml": WORKFLOW} if workflows is None else workflows
        listing = [{"name": n, "type": "file"} for n in workflows] + [
            {"name": "templates", "type": "dir"}
        ]
        answers: dict[str, object] = {
            ENVIRONMENT_PATH: environment,
            POLICIES_PATH: LIVE_POLICIES,
            f"repos/{REPO}/contents/.github/workflows?ref={HEAD}": listing,
        }
        for name, text in workflows.items():
            answers[_content(name)] = text
        super().__init__(answers)

    def __call__(self, argv):
        argv = list(argv)
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 9,
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefOid": HEAD,
                    "files": [{"path": f} for f in self.files],
                    "changedFiles": len(self.files),
                    "commits": [{"oid": HEAD, "committedDate": "2026-10-08T00:00:00Z"}],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            return "[]"
        if argv[:2] == ["api", "graphql"]:
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {"totalCount": 0, "nodes": []}
                            }
                        }
                    }
                }
            )
        if "/compare/" in argv[1] or "/git/trees/" in argv[1]:
            return "{}"
        if argv[-1].endswith("?ref=main"):  # the base side of a direction proof
            return WORKFLOW
        return super().__call__(argv)


@pytest.fixture
def _no_tables(monkeypatch):
    """A contract with empty tables, so a one-file tree can satisfy it."""
    monkeypatch.setattr(production_contract, "GATED_JOBS", {})
    monkeypatch.setattr(production_contract, "UNGATED_JOBS", {})


@pytest.mark.usefixtures("_no_tables")
def test_collect_reads_the_lock_for_a_pr_without_workflow_or_deploy_paths():
    """The gate reads the lock on every run of an open PR (#1138 F10)."""
    gh = _CollectGh(["tools/deploy_v2.py"])
    facts = client.collect(9, repo=REPO, gh=gh)
    assert facts.lock_failures == ()
    assert ENVIRONMENT_PATH in gh.paths and _content("ci.yml") in gh.paths


def test_a_lock_read_that_raises_is_a_failure_not_a_crash(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("unexpected shape")

    monkeypatch.setattr(client, "workflow_contract_failures", broken)
    facts = client.collect(9, repo=REPO, gh=_CollectGh(["tools/deploy_v2.py"]))
    assert facts.lock_failures == (
        "the lock check failed: ValueError: unexpected shape",
    )


@pytest.mark.usefixtures("_no_tables")
@pytest.mark.parametrize("path", [CI_WORKFLOW, "libs/alerting.py"])
def test_collect_reads_a_holding_lock_for_a_workflow_or_deploy_path(path):
    gh = _CollectGh([path])
    facts = client.collect(9, repo=REPO, gh=gh)
    assert facts.lock_failures == ()
    assert ENVIRONMENT_PATH in gh.paths
    assert _content("ci.yml") in gh.paths


@pytest.mark.usefixtures("_no_tables")
def test_collect_reports_a_lock_fact_and_a_contract_failure_together():
    secret_job = (
        "on: push\njobs:\n  ship:\n    steps:\n"
        "      - run: x\n        env:\n          K: ${{ secrets.DOKPLOY_API_KEY }}\n"
    )
    gh = _CollectGh(
        [CI_WORKFLOW],
        workflows={"ship.yml": secret_job},
        environment={**LIVE_ENVIRONMENT, "can_admins_bypass": True},
    )
    failures = client.collect(9, repo=REPO, gh=gh).lock_failures
    assert failures is not None and len(failures) == 2
    assert failures[0].startswith("administrators can bypass")
    assert failures[1].startswith("ship.yml:ship holds production credentials")


def test_collect_fails_closed_when_a_workflow_file_cannot_be_read():
    gh = _CollectGh([CI_WORKFLOW])
    del gh.answers[_content("ci.yml")]
    facts = client.collect(9, repo=REPO, gh=gh)
    # The contract does not run on a partial tree: its "does not exist" lines would
    # blame jobs that only could not be read.
    assert facts.lock_failures == (
        f"cannot read {WORKFLOW_PREFIX}ci.yml at {HEAD[:7]}",
    )


@pytest.mark.parametrize("listing", ["not json", RuntimeError("HTTP 502"), "[]"])
def test_collect_fails_closed_when_the_workflow_list_is_unreadable_or_empty(listing):
    gh = _CollectGh([CI_WORKFLOW])
    gh.answers[f"repos/{REPO}/contents/.github/workflows?ref={HEAD}"] = listing
    facts = client.collect(9, repo=REPO, gh=gh)
    assert facts.lock_failures == (f"cannot list the workflow files at {HEAD[:7]}",)


def test_collect_reads_no_lock_for_another_repository():
    gh = _CollectGh([CI_WORKFLOW])
    facts = client.collect(9, repo="wangzitian0/truealpha", gh=gh)
    assert facts.lock_failures is None
    assert not any("environments" in p for p in gh.paths)


# --- #1138 audit: contents URLs are quoted, the listing filter is exact -------------


class _Recorder:
    """Records the argv of each call and answers with a fixed text."""

    def __init__(self, answer: str = "text"):
        self.answer = answer
        self.argv: list[list[str]] = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        return self.answer


@pytest.mark.parametrize(
    ("name", "quoted"),
    [
        ("infra-ci.yml?x.yml", "infra-ci.yml%3Fx.yml"),
        ("a#b.yml", "a%23b.yml"),
        ("100%.yml", "100%25.yml"),
        ("a b.yml", "a%20b.yml"),
        ("部署.yml", "%E9%83%A8%E7%BD%B2.yml"),
    ],
    ids=["question-mark", "hash", "percent", "space", "unicode"],
)
def test_a_file_name_cannot_end_the_path_and_drop_the_ref(name, quoted):
    runner = _Recorder()
    assert client._file_at(REPO, HEAD, f"{WORKFLOW_PREFIX}{name}", gh=runner) == "text"
    assert runner.argv == [
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{REPO}/contents/{WORKFLOW_PREFIX}{quoted}?ref={HEAD}",
        ]
    ]


def test_the_ref_is_quoted_too():
    runner = _Recorder()
    client._file_at(REPO, "feat/x?y#z", "README.md", gh=runner)
    assert runner.argv[0][-1] == f"repos/{REPO}/contents/README.md?ref=feat%2Fx%3Fy%23z"


def test_the_workflow_list_url_is_quoted():
    runner = _Recorder("[]")
    client._workflow_names_at(REPO, "feat/x", gh=runner)
    assert runner.argv == [
        ["api", f"repos/{REPO}/contents/.github/workflows?ref=feat%2Fx"]
    ]


def test_the_lock_check_reads_a_listed_name_through_a_quoted_url():
    name = "infra-ci.yml?x.yml"
    gh = _CollectGh([CI_WORKFLOW], workflows={})
    gh.answers[f"repos/{REPO}/contents/.github/workflows?ref={HEAD}"] = [
        {"name": name, "type": "file"}
    ]
    gh.answers[_content("infra-ci.yml%3Fx.yml")] = WORKFLOW
    client._production_lock_failures(REPO, HEAD, gh=gh)
    assert _content("infra-ci.yml%3Fx.yml") in gh.paths
    assert _content(name) not in gh.paths


def test_the_listing_keeps_yml_and_yaml_files_only():
    listing = [
        {"name": "a.yml", "type": "file"},
        {"name": "b.yaml", "type": "file"},
        {"name": "c.json", "type": "file"},
        {"name": "x.yml", "type": "dir"},
        {"name": "templates", "type": "dir"},
    ]
    names = client._workflow_names_at(REPO, HEAD, gh=_Recorder(json.dumps(listing)))
    assert names == ("a.yml", "b.yaml")


@pytest.mark.usefixtures("_no_tables")
def test_the_lock_check_reads_a_yaml_workflow_of_the_head():
    secret_job = (
        "on: push\njobs:\n  ship:\n    steps:\n"
        "      - run: x\n        env:\n          K: ${{ secrets.DOKPLOY_API_KEY }}\n"
    )
    gh = _CollectGh([CI_WORKFLOW], workflows={"ship.yaml": secret_job})
    failures = client.collect(9, repo=REPO, gh=gh).lock_failures
    assert failures == (
        "ship.yaml:ship holds production credentials ['DOKPLOY_API_KEY'] and is in "
        "neither GATED_JOBS nor UNGATED_JOBS",
    )


def test_a_directory_named_like_a_workflow_is_not_read_as_one():
    gh = _CollectGh([CI_WORKFLOW])
    gh.answers[f"repos/{REPO}/contents/.github/workflows?ref={HEAD}"].append(
        {"name": "x.yml", "type": "dir"}
    )
    client._production_lock_failures(REPO, HEAD, gh=gh)
    assert _content("x.yml") not in gh.paths
