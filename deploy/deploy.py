"""
Deploy receipt-bot to VPS and restart systemd service.

Usage:
  python deploy/deploy.py              # upload bot.py + restart
  python deploy/deploy.py bot.py config_bot.py
  python deploy/deploy.py --full       # sync project (keeps remote .env / payments.json)

Credentials: deploy/.deploy.env (or env RECEIPT_HOST / RECEIPT_PASSWORD / …).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(__file__).resolve().parent / ".deploy.env"
SERVICE = "receipt-bot"
REMOTE_DIR = "/opt/receipt-bot"

KEEP_REMOTE = {".env", "payments.json"}
SKIP_NAMES = {
    ".cursor",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "output",
    "node_modules",
    ".deploy.env",
}
SKIP_SUFFIXES = (".pyc", ".tgz", ".log", ".session", ".session-journal")


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def connect() -> paramiko.SSHClient:
    _load_env()
    host = os.getenv("RECEIPT_HOST") or os.getenv("PDFBOT_HOST") or ""
    user = os.getenv("RECEIPT_USER") or os.getenv("PDFBOT_USER") or "root"
    password = os.getenv("RECEIPT_PASSWORD") or os.getenv("PDFBOT_PASSWORD") or ""
    key = os.getenv("RECEIPT_SSH_KEY") or os.getenv("PDFBOT_SSH_KEY") or ""
    if not host:
        raise SystemExit("RECEIPT_HOST not set — create deploy/.deploy.env")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key_path = os.path.expanduser(key) if key else ""
    if key_path and os.path.exists(key_path):
        pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
        client.connect(
            host,
            username=user,
            pkey=pkey,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
    elif password:
        client.connect(
            host,
            username=user,
            password=password,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
    else:
        raise SystemExit("No SSH key/password in deploy/.deploy.env")
    return client


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    print(">>>", cmd[:160], flush=True)
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip()[-2500:], flush=True)
    if err.strip():
        print("ERR:", err.rstrip()[-1500:], flush=True)
    print("exit", code, flush=True)
    return code, out, err


def _ensure_remote_dirs(ssh: paramiko.SSHClient, remotes: list[str]) -> None:
    parents = sorted({"/".join(r.split("/")[:-1]) for r in remotes if "/" in r})
    if not parents:
        return
    quoted = " ".join(f"'{p}'" for p in parents)
    run(ssh, f"mkdir -p {quoted}", timeout=60)


def upload_files(ssh: paramiko.SSHClient, rel_paths: list[str]) -> None:
    pairs: list[tuple[Path, str]] = []
    for rel in rel_paths:
        local = ROOT / rel
        if not local.is_file():
            raise SystemExit(f"missing file: {rel}")
        remote = f"{REMOTE_DIR}/{Path(rel).as_posix()}"
        pairs.append((local, remote))
    _ensure_remote_dirs(ssh, [r for _, r in pairs])
    sftp = ssh.open_sftp()
    try:
        for local, remote in pairs:
            print(f"put {local.relative_to(ROOT).as_posix()} -> {remote}", flush=True)
            sftp.put(str(local), remote)
    finally:
        sftp.close()


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_NAMES:
        return True
    if path.name in KEEP_REMOTE:
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    if path.name.startswith("tools/.tg_checker_session"):
        return True
    if path.name == "tg_check.env":
        return True
    return False


def upload_full(ssh: paramiko.SSHClient) -> None:
    pairs: list[tuple[Path, str]] = []
    for local in ROOT.rglob("*"):
        if not local.is_file():
            continue
        rel = local.relative_to(ROOT)
        if should_skip(rel):
            continue
        pairs.append((local, f"{REMOTE_DIR}/{rel.as_posix()}"))
    _ensure_remote_dirs(ssh, [r for _, r in pairs])
    sftp = ssh.open_sftp()
    try:
        for local, remote in pairs:
            print(f"put {local.relative_to(ROOT).as_posix()}", flush=True)
            sftp.put(str(local), remote)
    finally:
        sftp.close()


def restart(ssh: paramiko.SSHClient) -> None:
    code, _, _ = run(ssh, f"systemctl restart {SERVICE}")
    if code != 0:
        raise SystemExit("restart failed")
    time.sleep(2)
    code, out, _ = run(ssh, f"systemctl is-active {SERVICE}; tail -n 30 /var/log/receipt-bot.log")
    if "active" not in out:
        raise SystemExit("service not active after restart")
    print("DEPLOY_OK", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", help="Relative paths to upload (default: bot.py)")
    parser.add_argument("--full", action="store_true", help="Upload whole project (keep .env/payments)")
    args = parser.parse_args()

    ssh = connect()
    try:
        if args.full:
            upload_full(ssh)
        else:
            files = args.files or ["bot.py"]
            upload_files(ssh, files)
        restart(ssh)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
