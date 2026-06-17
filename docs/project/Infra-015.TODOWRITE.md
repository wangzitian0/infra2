# Infra-015: TODOWRITE (deploy_v2 front door)

**Status**: Active
**Owner**: Infra

## Purpose
Track the residual items for the deploy_v2 unified front door
([Infra-015.deploy_v2_front_door.md](./Infra-015.deploy_v2_front_door.md)). The
implementation PRs (#354–#371) are merged and the path is live-verified; only the
cross-repo sender half and the archive step remain.

## Top Issues
- [ ] **Merge companion finance_report#1173** — the app-repo *sender* (app `main`
  push → `repository_dispatch` into infra2). Until it merges, the infra2 *receiver*
  (`deploy-report-main.yml`, this PR) is wired but nothing auto-fires it end-to-end.
  Closes the cross-repo cutover tracked by #370 / root finance_report#1072.
- [ ] **Archive Infra-015** once #1173 merges: move the record to
  `docs/project/archive/Infra-015.deploy_v2_front_door.md`, merge this TODOWRITE into
  it, set Status: Archived, and update `docs/project/README.md` + `docs/mkdocs.yml`.
