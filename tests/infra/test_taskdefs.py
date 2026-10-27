"""Task definition templates and the script that fills them (DECISIONS #178).

Runs infra/fill_taskdef.py as a subprocess against a throwaway env file, so no
real .env is read and no AWS call is made. All values here are fake.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app:abc1234"
ARN = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:applied/cloudflare-tunnel-token-AbCdEf"
FAKE_TOKEN = "eyJFAKE-token-value-for-tests"

BASE_ENV = {
    "RDS_HOST": "db.example.internal",
    "RDS_MASTER_USERNAME": "master",
    "RDS_MASTER_PASSWORD": "pw-master",
    "APP_RUNTIME_DB_PASSWORD": "pw-runtime",
    "OPENAI_API_KEY": "sk-test-not-real",
    "COGNITO_REGION": "us-east-1",
    "COGNITO_USER_POOL_ID": "us-east-1_TESTPOOL",
    "COGNITO_APP_CLIENT_ID": "clientid123",
    "CLOUDFLARE_TUNNEL_SECRET_ARN": ARN,
}
NO_ARN = {k: v for k, v in BASE_ENV.items() if k != "CLOUDFLARE_TUNNEL_SECRET_ARN"}


def run(role, tmp_path, env=None, sidecar=None):
    env_file = tmp_path / "test.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in (BASE_ENV if env is None else env).items()))
    proc_env = {k: v for k, v in os.environ.items() if k != "TUNNEL_SIDECAR"}
    if sidecar is not None:
        proc_env["TUNNEL_SIDECAR"] = sidecar
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "infra" / "fill_taskdef.py"), role, IMAGE, ACCOUNT, str(env_file)],
        capture_output=True, text=True, env=proc_env, cwd=ROOT,
    )


def load(role, tmp_path, **kw):
    proc = run(role, tmp_path, **kw)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def containers(td):
    return {c["name"]: c for c in td["containerDefinitions"]}


def test_template_cloudflared_shape():
    tpl = json.loads((ROOT / "infra" / "api-taskdef.template.json").read_text())
    c = containers(tpl)["cloudflared"]
    assert c["essential"] is True
    assert c["image"] == "__CLOUDFLARED_IMAGE__"
    assert c["command"][-1] == "run" and "--metrics" in c["command"]
    assert c["dependsOn"] == [{"containerName": "south-st-pete-api", "condition": "START"}]
    assert c["healthCheck"]["command"][0] == "CMD"  # the image has no shell
    assert c["healthCheck"]["command"][-1] == "ready"
    assert c["logConfiguration"]["options"]["awslogs-group"] == "/ecs/south-st-pete-app"
    assert c["logConfiguration"]["options"]["awslogs-stream-prefix"] == "cloudflared"
    assert c["memoryReservation"] == 64 and "memory" not in c  # soft limit only
    assert tpl["cpu"] == "256" and tpl["memory"] == "512"


def test_token_only_as_a_secret_never_an_environment_value():
    tpl = json.loads((ROOT / "infra" / "api-taskdef.template.json").read_text())
    for c in tpl["containerDefinitions"]:
        for e in c.get("environment", []):
            assert "TUNNEL" not in e["name"].upper() and "CLOUDFLARE" not in e["name"].upper()
    cs = containers(tpl)
    assert cs["cloudflared"]["secrets"] == [{"name": "TUNNEL_TOKEN", "valueFrom": "__TUNNEL_TOKEN_SECRET_ARN__"}]
    assert "secrets" not in cs["south-st-pete-api"]
    migrate = json.loads((ROOT / "infra" / "migrate-taskdef.template.json").read_text())
    assert all("secrets" not in c for c in migrate["containerDefinitions"])
    assert "cloudflared" not in containers(migrate)


def test_pin_file_is_a_real_digest_not_latest():
    text = (ROOT / "infra" / "CLOUDFLARED_PIN").read_text()
    assert re.search(r"^CLOUDFLARED_DIGEST=sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^CLOUDFLARED_VERSION=\d{4}\.\d+\.\d+$", text, re.M)
    assert not re.search(r"^CLOUDFLARED_\w+=.*latest", text, re.M)


def test_api_filled_with_sidecar(tmp_path):
    td = load("api", tmp_path)
    c = containers(td)
    digest = re.search(r"CLOUDFLARED_DIGEST=(\S+)", (ROOT / "infra" / "CLOUDFLARED_PIN").read_text()).group(1)
    assert c["cloudflared"]["image"] == f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app@{digest}"
    assert c["cloudflared"]["secrets"][0]["valueFrom"] == ARN
    assert c["south-st-pete-api"]["image"] == IMAGE
    assert not re.search(r"__[A-Z]", json.dumps(td))  # no placeholder left


def test_sidecar_off_drops_container_and_does_not_need_the_arn(tmp_path):
    proc = run("api", tmp_path, env=NO_ARN, sidecar="off")
    assert proc.returncode == 0, proc.stderr
    assert list(containers(json.loads(proc.stdout))) == ["south-st-pete-api"]
    assert "NO TUNNEL SIDECAR" in proc.stderr


def test_unset_arn_with_sidecar_on_refuses(tmp_path):
    proc = run("api", tmp_path, env=NO_ARN)
    assert proc.returncode != 0 and "CLOUDFLARE_TUNNEL_SECRET_ARN" in proc.stderr


@pytest.mark.parametrize("bad", [
    "arn:aws:secretsmanager:us-east-1:999999999999:secret:applied/cloudflare-tunnel-token-AbCdEf",  # other account
    f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:applied/cloudflare-tunnel-token-AbCdEf",  # other region
    f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:applied/other-secret-AbCdEf",  # other secret
    f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:applied/cloudflare-tunnel-token-",  # no suffix
    f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:applied/cloudflare-tunnel-token-AbCdEf:TUNNEL_TOKEN::",  # selector
    f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:applied/cloudflare-tunnel-token-AbCdEfGh",  # too long
])
def test_wrong_arn_refused(tmp_path, bad):
    proc = run("api", tmp_path, env={**BASE_ENV, "CLOUDFLARE_TUNNEL_SECRET_ARN": bad})
    assert proc.returncode != 0 and "CLOUDFLARE_TUNNEL_SECRET_ARN" in proc.stderr


def test_leftover_token_in_env_refused_without_echoing_it(tmp_path):
    for sidecar in (None, "off"):
        proc = run("api", tmp_path, env={**BASE_ENV, "CLOUDFLARE_TUNNEL_TOKEN": FAKE_TOKEN}, sidecar=sidecar)
        assert proc.returncode != 0
        assert "CLOUDFLARE_TUNNEL_TOKEN" in proc.stderr
        assert FAKE_TOKEN not in proc.stdout + proc.stderr


def test_bad_sidecar_switch_refused(tmp_path):
    assert run("api", tmp_path, sidecar="no").returncode != 0
    assert run("api", tmp_path, sidecar="").returncode != 0  # empty is not "off"


def test_tunnel_check_is_cloudflared_alone(tmp_path):
    td = load("tunnel-check", tmp_path)
    assert td["family"] == "south-st-pete-tunnel-check"
    assert list(containers(td)) == ["cloudflared"]
    c = containers(td)["cloudflared"]
    assert "dependsOn" not in c and "healthCheck" not in c
    assert c["logConfiguration"]["options"]["awslogs-stream-prefix"] == "cloudflared-check"
    assert "taskRoleArn" not in td
    assert c["secrets"][0]["valueFrom"] == ARN
    assert run("tunnel-check", tmp_path, sidecar="off").returncode != 0


def test_migrate_unchanged_by_the_sidecar_and_never_gets_the_arn(tmp_path):
    td = load("migrate", tmp_path, env=NO_ARN)
    assert "cloudflared" not in containers(td)
    assert ARN not in json.dumps(td)


def test_missing_or_placeholder_required_value_refused(tmp_path):
    for k in ("OPENAI_API_KEY", "COGNITO_APP_CLIENT_ID"):
        assert run("api", tmp_path, env={x: y for x, y in BASE_ENV.items() if x != k}).returncode != 0
    assert run("api", tmp_path, env={**BASE_ENV, "OPENAI_API_KEY": "sk-changeme"}).returncode != 0


def test_register_script_fills_before_it_registers():
    text = (ROOT / "infra" / "register_app_taskdefs.sh").read_text()
    assert text.index("python infra/fill_taskdef.py") < text.index("aws ecs register-task-definition")
    assert text.count("for role in ${ROLES}") == 2  # fill loop, then register loop


def test_commented_or_exported_token_line_also_refused_and_not_echoed(tmp_path):
    for line in (f"# CLOUDFLARE_TUNNEL_TOKEN={FAKE_TOKEN}", f"export CLOUDFLARE_TUNNEL_TOKEN={FAKE_TOKEN}",
                 f"##CLOUDFLARE_TUNNEL_TOKEN={FAKE_TOKEN}"):
        env_file = tmp_path / "test.env"
        body = "".join(f"{k}={v}\n" for k, v in BASE_ENV.items()) + line + "\n"
        env_file.write_text(body)
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "infra" / "fill_taskdef.py"), "api", IMAGE, ACCOUNT, str(env_file)],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert proc.returncode != 0 and FAKE_TOKEN not in proc.stdout + proc.stderr
        assert "line(s) 10" in proc.stderr


def test_tunnel_check_also_refuses_leftover_token(tmp_path):
    assert run("tunnel-check", tmp_path, env={**BASE_ENV, "CLOUDFLARE_TUNNEL_TOKEN": FAKE_TOKEN}).returncode != 0


def test_a_secret_that_looks_like_a_placeholder_is_filled_not_echoed(tmp_path):
    proc = run("api", tmp_path, env={**BASE_ENV, "OPENAI_API_KEY": "sk-abc__SECRETBIT__def"})
    assert proc.returncode == 0  # filled values are never mistaken for unfilled placeholders
    assert "SECRETBIT" not in proc.stderr


def _bash():
    import shutil
    return shutil.which("bash")


AWS_STUB = "\n".join([
    "#!/bin/bash",
    'if [ "$1 $2" = "sts get-caller-identity" ]; then echo 123456789012; exit 0; fi',
    'if [ "$1 $2" = "ecr describe-images" ]; then echo "$DESCRIBE_OUT"; exit $DESCRIBE_RC; fi',
    "exit 99",
    "",
])
DOCKER_STUB = "#!/bin/bash\necho DOCKER-WAS-CALLED >&2\nexit 98\n"


@pytest.mark.skipif(_bash() is None, reason="needs bash")
@pytest.mark.parametrize("describe_out,describe_rc,expect_text", [
    ("sha256:" + "c" * 64, 0, "DIFFERENT digest"),  # tag exists with another digest
    ("An error occurred (AccessDeniedException) ...", 255, "Could not check ECR"),  # not a 'not found'
])
def test_mirror_script_refuses_on_ambiguous_ecr_state(tmp_path, describe_out, describe_rc, expect_text):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_bytes(AWS_STUB.encode())
    (bindir / "docker").write_bytes(DOCKER_STUB.encode())
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "AWS_PROFILE": "x",
           "DESCRIBE_OUT": describe_out, "DESCRIBE_RC": str(describe_rc)}
    proc = subprocess.run([_bash(), str(ROOT / "infra" / "mirror_cloudflared.sh")],
                          capture_output=True, text=True, env=env, cwd=ROOT)
    assert proc.returncode == 1 and expect_text in proc.stderr
    assert "DOCKER-WAS-CALLED" not in proc.stderr  # nothing pushed on an ambiguous state


def test_tunnel_token_policy_template_is_one_read_on_one_placeholder_resource():
    pol = json.loads((ROOT / "infra" / "tunnel-token-policy.json").read_text())
    (stmt,) = pol["Statement"]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "secretsmanager:GetSecretValue"  # one action, no list, no wildcard
    assert stmt["Resource"] == "__TUNNEL_TOKEN_SECRET_ARN__"  # a placeholder, never a literal or '*'
