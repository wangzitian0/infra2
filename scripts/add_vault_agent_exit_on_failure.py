#!/usr/bin/env python3
"""
Add exit_on_retry_failure and exit_on_err to all vault-agent.hcl files.

This script modifies vault-agent.hcl files to exit on authentication failures
instead of retrying indefinitely with invalid tokens.

Related to: HIGH-5 - vault-agent should exit on permanent failures
"""

import re
from pathlib import Path

# Find all vault-agent.hcl files
repo_root = Path(__file__).parent.parent
vault_agent_files = list(repo_root.glob("**/vault-agent.hcl"))

print(f"Found {len(vault_agent_files)} vault-agent.hcl files\n")

for file_path in vault_agent_files:
    print(f"Processing: {file_path.relative_to(repo_root)}")

    content = file_path.read_text()

    # Check if already modified
    if "exit_on_retry_failure" in content and "exit_on_err" in content:
        print("  ✓ Already has exit settings, skipping\n")
        continue

    # Add exit_on_retry_failure to template_config block
    if "exit_on_retry_failure" not in content:
        content = re.sub(
            r'(template_config\s*\{[^}]*static_secret_render_interval\s*=\s*"[^"]*")',
            r"\1\n  exit_on_retry_failure = true",
            content,
        )

    # Add exit_on_err to auto_auth block (before closing brace)
    if "exit_on_err" not in content:
        # Find the auto_auth block and add exit_on_err before the closing brace
        content = re.sub(
            r"(auto_auth\s*\{.*?sink\s*\{.*?\}.*?)\n(\})",
            r"\1\n\n  exit_on_err = true\n\2",
            content,
            flags=re.DOTALL,
        )

    # Write back
    file_path.write_text(content)
    print("  ✓ Added exit settings\n")

print("Done!")
