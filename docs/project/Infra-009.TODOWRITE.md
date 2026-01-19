# Infra-009 TODOWRITE

## Pending Tasks

- [ ] Create finance_report/finance_report/ structure
- [ ] Deploy PostgreSQL
- [ ] Deploy Redis
- [ ] Deploy App
- [ ] Verify end-to-end
- [x] Set OTEL Vault values for finance_report app (staging/production)
- [ ] Confirm logs appear in SigNoz UI

## Notes

Main documentation is in finance_report repository:
- EPIC-007.deployment.md

## Artifacts

- Added OTEL keys to finance_report app secrets template
- Documented OTEL keys in finance_report app README
- Added `IAC_CONFIG_HASH` to finance_report app compose for restart-safe updates
- Replaced unsupported template helpers in finance_report app secrets template
