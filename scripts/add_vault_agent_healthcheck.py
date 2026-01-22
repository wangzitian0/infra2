#!/usr/bin/env python3
"""Add healthcheck to all vault-agent containers in compose.yaml files"""

import re
from pathlib import Path

HEALTHCHECK = """    healthcheck:
      test: ["CMD", "test", "-f", "/vault/secrets/.env"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 10s"""


def add_healthcheck_to_compose(file_path: Path) -> bool:
    content = file_path.read_text()

    if "vault-agent:" not in content:
        return False

    if (
        "vault-agent" in content
        and "healthcheck:" in content
        and "test.*vault/secrets" in content
    ):
        print(f"  ✅ {file_path.relative_to(Path.cwd())} - already has healthcheck")
        return False

    pattern = r"(  vault-agent:\n(?:.*\n)*?)(  \w+:|\nvolumes:|\nnetworks:)"

    def replacement(match):
        vault_agent_section = match.group(1)
        next_section = match.group(2)
        return f"{vault_agent_section}{HEALTHCHECK}\n\n{next_section}"

    new_content, count = re.subn(pattern, replacement, content, count=1)

    if count > 0:
        file_path.write_text(new_content)
        print(f"  ✅ {file_path.relative_to(Path.cwd())} - added healthcheck")
        return True
    else:
        print(f"  ❌ {file_path.relative_to(Path.cwd())} - pattern not matched")
        return False


def main():
    repo_root = Path.cwd()
    compose_files = list(repo_root.rglob("compose.yaml"))

    updated = 0
    for file_path in sorted(compose_files):
        if add_healthcheck_to_compose(file_path):
            updated += 1

    print(f"\n✅ Updated {updated} files")


if __name__ == "__main__":
    main()
