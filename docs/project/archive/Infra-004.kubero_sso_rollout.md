# Infra-004: Kubero SSO Rollout and Emergency Access

**Status**: Archived  
**Owner**: Infra  
**Legacy Source**: ISSUE-2025-12-20 (Kubero SSO rollout)

## Summary
Roll out Casdoor OIDC for Kubero SSO while retaining a break-glass access path
for infra operators, documenting pitfalls and validation steps.

## PR Links
- PR #307: https://github.com/wangzitian0/infra/pull/307

## Change Log
- No dedicated change log entry (issue-driven operational work).

## Git Commits (Backtrace)
- eb1b5f8 feat(sso): decouple Casdoor OIDC from portal gate (#307)

## Legacy Issue (ISSUE-2025-12-20)

**Status**: 🟡 Open
**Owner**: Infra
**Created**: 2025-12-20

## 1) Context

Goal: Most users should use SSO; infra retains a break-glass path to resolve SSO/service deadlocks.

Scope:
- Portal SSO Gate is optional (Casdoor + OAuth2-Proxy + Traefik) for non-OIDC portals.
- Kubero should use Casdoor OIDC for primary login (native OIDC, no forwardAuth).
- Kubero emergency access should not rely on SSO.

## 2) Pitfalls hit

- Casdoor apps missing `tokenFormat=JWT` caused `unknown application TokenFormat`.
- Casdoor apps had `expireInHours/refreshExpireInHours=0`, causing OAuth2-Proxy to reject `id_token` as expired.
- `signupItems=null` caused Casdoor login white screen (`AgreementModal` crash).
- `enablePassword=false` / providers missing owner broke “Password + GitHub” login page.
- Dashboard still requires bearer token even after SSO (Dashboard has no OIDC login).
- Kubero has no “token login” path; break-glass must be local admin or a private ingress.
- Changes were merged but not applied, leaving live config stale.

## 3) TODO

1. Apply Kubero OIDC changes:
   - `atlantis apply -d 2.platform`
   - `atlantis apply -p apps-prod` (and/or `apps-staging`)
2. Verify Kubero SSO:
   - `https://kcloud.${INTERNAL_DOMAIN}/` redirects to Casdoor and returns to Kubero.
3. Confirm break-glass policy:
   - Keep local Kubero admin for emergency use (SSO disabled does not lock out infra).
4. Document expected behavior:
   - Dashboard: SSO gate + token login.
   - Kubero: OIDC primary (`enable_casdoor_oidc=true`) + local admin as break-glass.

## 4) Verification checklist

- Casdoor app `portal-gate` shows `tokenFormat=JWT`, TTL 168/168.
- Kubero login page shows Casdoor SSO; local admin still works.
- No `id_token is expired` errors in portal-auth or casdoor logs.
