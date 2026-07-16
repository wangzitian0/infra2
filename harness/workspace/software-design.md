# Software Design Preferences

These are cross-workspace engineering preferences. Repository-local architecture owns
domain semantics and may impose stronger constraints.

## Boundaries Before Reuse

- Keep dependency direction explicit and acyclic.
- Share only stable semantics. Do not extract code merely because two implementations
  look similar today.
- Separate contracts from instances: `infra2-sdk` owns versioned data models,
  validation, serialization, and compatibility; each repository owns its CI gate
  instances and implementation policy.
- Keep human workflow preferences out of `infra2-sdk`. They belong in this harness or
  the owning repository, not in runtime wire contracts.
- Never make workspace submodules package, runtime, deployment, or configuration-hash
  dependencies.

## Change Design

- Prefer small, composable units with explicit inputs, outputs, and side-effect
  boundaries.
- Fail closed before irreversible or Production side effects when authority, version,
  or evidence is ambiguous.
- Replace magic values with named configuration only when the value represents a real
  policy or reusable concept.
- Preserve backward compatibility across independently released repositories. Breaking
  serialized or public API changes require an explicit major-version path.
- Keep the default branch deployable and migrations backward compatible; make rollback
  a design input rather than an afterthought.

## Proof

- Test observable behavior and boundary failures, not implementation shape alone.
- A bug fix includes the root cause, why existing proof missed it, and the missing
  regression gate in the same change.
- Claims cannot exceed evidence. Unit proof, integration proof, staging proof, and
  Production proof are distinct and must name their exact revision and environment.
- Prefer generated or machine-validated indexes over parallel hand-maintained copies of
  the same fact.
