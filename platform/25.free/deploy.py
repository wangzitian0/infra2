"""Free service deployment"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from libs.deploy.deployer import Deployer, make_tasks
from libs.console import error, run_with_status, success, header, info, warning
from libs.service_facets import BackupFacet, Exemption


def _generate_uuid() -> str:
    """Generate an RFC 4122 compliant UUID v4 without importing standard library 'uuid'.

    Standard library 'uuid' imports 'platform', which conflicts with this repo's
    top-level 'platform' directory when validating deployers.
    """
    b = bytearray(secrets.token_bytes(16))
    b[6] = (b[6] & 0x0F) | 0x40  # Version 4
    b[8] = (b[8] & 0x3F) | 0x80  # Variant 1 (RFC 4122)
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# Get shared_tasks from sys.modules (loaded by tools/loader.py)
shared_tasks = sys.modules.get("platform.25.free.shared")


class FreeDeployer(Deployer):
    service = "free"
    compose_path = "platform/25.free/compose.yaml"
    data_path = "/data/platform/free"

    backups = (
        BackupFacet(
            method="config_archive",
            restore_command="restore free config.json.",
        ),
    )

    uid = "0"
    gid = "0"
    chmod = "700"

    subdomain = None  # Handled via Traefik labels in compose.yaml
    service_port = 10000
    service_name = "free"

    exemptions = (
        Exemption(
            check_id="probes",
            reason="custom gateway endpoint with secret path",
        ),
    )
    secret_key = ""

    @classmethod
    def _ensure_remote_config(cls, c) -> tuple[str, str] | None:
        env = cls.env()
        host = env.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return None

        user = env.get("VPS_SSH_USER", "root")
        data_path = cls.data_path_for_env(env)
        config_remote_path = f"{data_path}/config.json"
        safe_path = shlex.quote(config_remote_path)

        if user != "root":
            error(
                f"FreeDeployer requires root SSH access to manage {config_remote_path}; "
                f"unsupported VPS_SSH_USER: {user}"
            )
            return None

        # Check if remote config already exists to keep UUID and Path stable
        existing_uuid = None
        existing_path = None
        check_cmd = f"ssh -o BatchMode=yes {shlex.quote(f'{user}@{host}')} {shlex.quote(f'cat {safe_path}')}"
        res = c.run(check_cmd, warn=True, hide=True)
        if res.ok and res.stdout.strip():
            try:
                cfg = json.loads(res.stdout.strip())
                inbounds = cfg.get("inbounds", [])
                if inbounds:
                    users = inbounds[0].get("users", [])
                    if users:
                        existing_uuid = users[0].get("uuid")
                    existing_path = inbounds[0].get("transport", {}).get("path")
                if not existing_uuid or not existing_path:
                    error(
                        "Existing remote config is missing required uuid or path, refusing to overwrite"
                    )
                    return None
            except Exception as exc:
                error(
                    f"Existing remote config is malformed, refusing to overwrite: {exc}"
                )
                return None

        free_uuid = existing_uuid or os.getenv("FREE_UUID") or _generate_uuid()
        free_path = (
            existing_path or os.getenv("FREE_PATH") or f"/api/v1/{secrets.token_hex(16)}"
        )

        import re

        if not re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
            free_uuid,
        ):
            error("Invalid FREE_UUID format (must be valid RFC 4122 UUID)")
            return None
        if not re.match(r"^/api/v1/[a-zA-Z0-9_-]+$", free_path):
            error("Invalid FREE_PATH format (must match ^/api/v1/[a-zA-Z0-9_-]+$)")
            return None

        perm_cmd = f"ssh {shlex.quote(f'{user}@{host}')} {shlex.quote(f'chown {cls.uid}:{cls.gid} {safe_path} && chmod 600 {safe_path}')}"

        if existing_uuid == free_uuid and existing_path == free_path:
            perm_res = run_with_status(c, perm_cmd, "Set config permissions")
            if not perm_res.ok:
                return None
            return free_uuid, free_path

        template_path = Path(__file__).with_name("config.json.tmpl")
        if not template_path.exists():
            error("Missing config template", str(template_path))
            return None

        config_content = template_path.read_text(encoding="utf-8")
        config_content = config_content.replace("{{FREE_UUID}}", free_uuid)
        config_content = config_content.replace("{{FREE_PATH}}", free_path)

        tmp_path = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name

            target = f"{shlex.quote(f'{user}@{host}')}:{safe_path}"
            upload_res = run_with_status(
                c,
                f"scp {tmp_path} {target}",
                "Upload gateway config",
            )
            if not upload_res.ok:
                return None

            perm_res = run_with_status(c, perm_cmd, "Set config permissions")
            if not perm_res.ok:
                return None

            return free_uuid, free_path
        except OSError as exc:
            error("Failed to prepare config", str(exc))
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @classmethod
    def pre_compose(cls, c):
        if not cls._prepare_dirs(c):
            error("Failed to prepare free service directories")
            return None

        env = cls.env()
        internal_domain = env.get("INTERNAL_DOMAIN")
        if not internal_domain:
            error("Missing INTERNAL_DOMAIN")
            return None

        config_info = cls._ensure_remote_config(c)
        if not config_info:
            return None
        _free_uuid, free_path = config_info

        data_path = cls.data_path_for_env(env)
        result = cls.compose_env_base(env)
        result.update(
            {
                "FREE_PATH": free_path,
                "INTERNAL_DOMAIN": internal_domain,
                "DATA_PATH": data_path,
            }
        )
        return result

    @classmethod
    def composing(cls, c, env_vars: dict[str, str]) -> str:
        config_info = cls._ensure_remote_config(c)
        if not config_info:
            raise RuntimeError("Failed to ensure remote config for free gateway")
        _free_uuid, free_path = config_info
        env_vars["FREE_PATH"] = free_path
        return super().composing(c, env_vars)

    @classmethod
    def print_client_info(cls, c):
        env = cls.env()
        internal_domain = env.get("INTERNAL_DOMAIN")
        if not internal_domain:
            error("Missing INTERNAL_DOMAIN")
            return

        domain_suffix = env.get("ENV_DOMAIN_SUFFIX", "")
        host = env.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return

        user = env.get("VPS_SSH_USER", "root")
        data_path = cls.data_path_for_env(env)
        config_remote_path = f"{data_path}/config.json"

        if user != "root":
            warning(
                f"FreeDeployer requires root SSH access to inspect {config_remote_path}; "
                f"unsupported VPS_SSH_USER: {user}"
            )
            return

        free_uuid = None
        free_path = None
        safe_path = shlex.quote(config_remote_path)
        check_cmd = f"ssh -o BatchMode=yes {shlex.quote(f'{user}@{host}')} {shlex.quote(f'cat {safe_path}')}"
        res = c.run(check_cmd, warn=True, hide=True)
        if res.ok and res.stdout.strip():
            try:
                cfg = json.loads(res.stdout.strip())
                inbounds = cfg.get("inbounds", [])
                if inbounds:
                    users = inbounds[0].get("users", [])
                    if users:
                        free_uuid = users[0].get("uuid")
                    free_path = inbounds[0].get("transport", {}).get("path")
            except Exception as exc:
                warning(f"Failed to parse remote config: {exc}")
                return

        if free_uuid and free_path:
            fqdn = f"free{domain_suffix}.{internal_domain}"
            vless_url = (
                f"vless://{free_uuid}@{fqdn}:443"
                f"?encryption=none&security=tls&type=ws&host={fqdn}&path={quote(free_path)}#free"
            )
            show_secrets = os.getenv("FREE_SHOW_SECRETS") == "1"
            display_uuid = (
                free_uuid
                if show_secrets
                else f"{free_uuid[:8]}-****-****-****-************"
            )
            display_path = free_path if show_secrets else f"{free_path[:8]}****"

            header("Free Gateway Deployed", "Client Connection Info")
            info(f"Domain: {fqdn}")
            info("Port: 443 (TLS)")
            info(f"Path: {display_path}")
            info(f"UUID: {display_uuid}")
            if show_secrets:
                info(f"URL:  {vless_url}")
            else:
                info(
                    "URL:  (hidden; set FREE_SHOW_SECRETS=1 to view or export to file)"
                )

            # Optional client info file export via env var (requires FREE_SHOW_SECRETS=1)
            save_path_env = os.getenv("FREE_CLIENT_INFO_PATH")
            if save_path_env:
                if not show_secrets:
                    warning(
                        "FREE_CLIENT_INFO_PATH is set but ignored because FREE_SHOW_SECRETS=1 is required to export credentials to disk"
                    )
                else:
                    save_doc = Path(save_path_env)
                    if not save_doc.parent.exists():
                        warning(
                            f"Parent directory does not exist for FREE_CLIENT_INFO_PATH: {save_doc.parent}"
                        )
                    else:
                        content = (
                            f"# Free Gateway Connection Info\n\n"
                            f"- Host: `{fqdn}`\n"
                            f"- Port: `443`\n"
                            f"- TLS: `true`\n"
                            f"- Path: `{free_path}`\n"
                            f"- UUID: `{free_uuid}`\n\n"
                            f"```text\n{vless_url}\n```\n"
                        )
                        try:
                            fd = os.open(
                                save_doc,
                                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                0o600,
                            )
                            with open(fd, "w", encoding="utf-8") as f:
                                f.write(content)
                            success(f"Saved connection info to {save_doc} (mode 0600)")
                        except OSError as exc:
                            warning(
                                f"Failed to write connection info to {save_doc}: {exc}"
                            )

    @classmethod
    def post_compose(cls, c, shared_tasks):
        ok = super().post_compose(c, shared_tasks)
        if ok:
            cls.print_client_info(c)
        return ok


if shared_tasks:
    _tasks = make_tasks(FreeDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
