"""Fills an ECS task definition template and prints the JSON to stdout.

Called by infra/register_app_taskdefs.sh (moved out of its inline heredoc so
the rules below can be tested). Reads only the NAMED keys it needs from .env,
as literal JSON string values, never via sed or the shell (DECISIONS #119).

Usage:
    python infra/fill_taskdef.py <role> <image> <account_id> [env_file]
      role: api | migrate | tunnel-check
      tunnel-check: the api template cut down to its cloudflared container
        only, for the pre-migration rehearsal (DECISIONS #178). It is derived
        from the api template so the two can't drift.

Environment:
    TUNNEL_SIDECAR  "on" (default) or "off". Off drops the cloudflared
        container from the api definition and refuses the tunnel-check role.
        Only an explicit "off" turns the tunnel off: silently dropping it
        after the firewall is closed would take the app down.
"""
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = {
    "api": "infra/api-taskdef.template.json",
    "migrate": "infra/migrate-taskdef.template.json",
    "tunnel-check": "infra/api-taskdef.template.json",
}
REQUIRED = [
    "RDS_HOST", "RDS_MASTER_USERNAME", "RDS_MASTER_PASSWORD", "APP_RUNTIME_DB_PASSWORD",
    "OPENAI_API_KEY", "COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID",
]
SECRET_NAME = "applied/cloudflare-tunnel-token"
SIDECAR = "cloudflared"


def die(msg: str) -> None:
    sys.exit(msg)


def read_pin() -> dict:
    out = {}
    for line in (REPO_ROOT / "infra" / "CLOUDFLARED_PIN").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", out.get("CLOUDFLARED_DIGEST", "")):
        die("infra/CLOUDFLARED_PIN: CLOUDFLARED_DIGEST must be sha256:<64 hex>")
    if not re.fullmatch(r"\d{4}\.\d+\.\d+", out.get("CLOUDFLARED_VERSION", "")):
        die("infra/CLOUDFLARED_PIN: CLOUDFLARED_VERSION must look like 2026.10.4")
    return out


def main() -> None:
    if len(sys.argv) not in (4, 5):
        die(__doc__)
    role, image, account_id = sys.argv[1:4]
    env_file = sys.argv[4] if len(sys.argv) == 5 else ".env"
    if role not in TEMPLATES:
        die(f"unknown role {role!r}")

    sidecar = os.environ.get("TUNNEL_SIDECAR", "on")
    if sidecar not in ("on", "off"):
        die("TUNNEL_SIDECAR must be 'on' or 'off' (or unset for on)")
    if role == "tunnel-check" and sidecar == "off":
        die("tunnel-check needs the sidecar; TUNNEL_SIDECAR=off was set")

    env = {k: (v or "").strip() for k, v in dotenv_values(env_file, interpolate=False).items()}

    # The old token must not linger in .env once it lives in Secrets Manager.
    # Presence is checked by key name; the value is never read into a message.
    # Also catches a commented-out or exported copy: scan the raw text, and
    # report only the line number, never the line.
    raw = Path(env_file).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    hits = [i for i, line in enumerate(raw, 1)
            if re.match(r"[\s#]*(export\s+)?CLOUDFLARE_TUNNEL_TOKEN\s*=", line)]
    if hits or env.get("CLOUDFLARE_TUNNEL_TOKEN"):
        die("Refusing to register: .env still holds CLOUDFLARE_TUNNEL_TOKEN (line(s) "
            + ", ".join(map(str, hits)) + "), even if commented out. The token lives only "
            "in Secrets Manager (DECISIONS #178); delete that line from .env (and any backup or shell history).")

    # Shared check for every role, as before: one deploy registers everything,
    # so a missing value stops it before anything is registered.
    bad = [k for k in REQUIRED if not env.get(k) or env[k].lower().startswith(("changeme", "sk-changeme"))]
    if bad:
        die("Refusing to register: missing or still-placeholder in .env: " + ", ".join(bad))
    if not env["COGNITO_USER_POOL_ID"].startswith(env["COGNITO_REGION"] + "_"):
        die("Refusing to register: COGNITO_USER_POOL_ID should start with COGNITO_REGION + '_'")

    values = {
        "__IMAGE__": image,
        "__ACCOUNT_ID__": account_id,
        "__DB_HOST__": env["RDS_HOST"],
        "__RDS_MASTER_USERNAME__": env["RDS_MASTER_USERNAME"],
        "__RDS_MASTER_PASSWORD__": env["RDS_MASTER_PASSWORD"],
        "__APP_RUNTIME_DB_PASSWORD__": env["APP_RUNTIME_DB_PASSWORD"],
        "__OPENAI_API_KEY__": env["OPENAI_API_KEY"],
        "__COGNITO_REGION__": env["COGNITO_REGION"],
        "__COGNITO_USER_POOL_ID__": env["COGNITO_USER_POOL_ID"],
        "__COGNITO_APP_CLIENT_ID__": env["COGNITO_APP_CLIENT_ID"],
    }

    if sidecar == "on" and role in ("api", "tunnel-check"):
        arn = env.get("CLOUDFLARE_TUNNEL_SECRET_ARN", "")
        prefix = f"arn:aws:secretsmanager:us-east-1:{account_id}:secret:{SECRET_NAME}-"
        # Secrets Manager appends exactly six random characters. Anything
        # longer (a ":json-key:" selector, a path) would change what ECS reads.
        if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", arn):
            die("Refusing to register: CLOUDFLARE_TUNNEL_SECRET_ARN in .env must be the secret's ARN, "
                f"starting {prefix} plus six characters (this account, us-east-1). Use TUNNEL_SIDECAR=off only for a deliberate rollback.")
        pin = read_pin()
        values["__TUNNEL_TOKEN_SECRET_ARN__"] = arn
        values["__CLOUDFLARED_IMAGE__"] = (
            f"{account_id}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app@{pin['CLOUDFLARED_DIGEST']}"
        )

    template = json.loads((REPO_ROOT / TEMPLATES[role]).read_text(encoding="utf-8"))
    containers = template["containerDefinitions"]
    if role == "api" and sidecar == "off":
        template["containerDefinitions"] = [c for c in containers if c["name"] != SIDECAR]
        print("NO TUNNEL SIDECAR: TUNNEL_SIDECAR=off, the api task definition has no cloudflared.", file=sys.stderr)
    elif role == "tunnel-check":
        keep = [c for c in containers if c["name"] == SIDECAR]
        if len(keep) != 1:
            die("api template has no single cloudflared container")
        c = keep[0]
        c.pop("dependsOn", None)       # the api container isn't part of the rehearsal
        c.pop("healthCheck", None)
        c["logConfiguration"]["options"]["awslogs-stream-prefix"] = "cloudflared-check"
        template["containerDefinitions"] = [c]
        template["family"] = "south-st-pete-tunnel-check"
        template.pop("taskRoleArn", None)  # nothing in it needs AWS permissions

    def fill(node):
        if isinstance(node, dict):
            return {k: fill(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill(v) for v in node]
        if isinstance(node, str):
            for placeholder, value in values.items():
                node = node.replace(placeholder, value)
            return node
        return node

    filled = fill(template)
    # Scan the TEMPLATE for placeholders we did not fill, never the filled
    # output: a secret that happens to contain "__X__" must not be echoed.
    leftover = sorted(p for p in set(re.findall(r"__[A-Z][A-Z0-9_]*__", json.dumps(template)))
                      if p not in values)
    if leftover:
        die("Unfilled placeholders: " + ", ".join(leftover))
    json.dump(filled, sys.stdout)


if __name__ == "__main__":
    main()
