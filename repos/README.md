# Workspace Repositories

This directory provides one recursive-clone entry point for infra2 and its related
repositories:

- `infra2-sdk`: versioned cross-repository contracts.
- `finance_report`: Finance Report application source.
- `truealpha`: TrueAlpha application source.

Initialize or refresh the pinned workspace snapshot from the infra2 root:

```bash
git submodule update --init --recursive
git submodule update --remote repos/infra2-sdk repos/finance_report repos/truealpha
```

Commit updated gitlinks only after the selected repository revisions pass integration
checks together. These submodules are a development workspace, not package, runtime, or
deployment dependencies. Production identities remain infra2 release tags, SDK SemVer,
and application image refs or digests.
