#!/usr/bin/env python3
"""Provision the agentbot Delta Chat account: chatmail address, pixel avatar,
systemd service. Run once."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from deltachat_rpc_client import DeltaChat, Rpc

from agentbot.avatar import make_avatar

BOT_DIR = Path.cwd()
RPC_SERVER_PATH = shutil.which("deltachat-rpc-server") or "deltachat-rpc-server"
PYTHON = sys.executable
CHATMAIL_QR = "DCACCOUNT:https://chtml.ca/new"


def provision():
    accounts_dir = str(BOT_DIR / "accounts")
    with Rpc(accounts_dir=accounts_dir, rpc_server_path=RPC_SERVER_PATH) as rpc:
        dc = DeltaChat(rpc)
        account = dc.add_account()
        account.set_config("bot", "1")
        account.set_config_from_qr(CHATMAIL_QR)
        account.configure()
        account.set_config("displayname", "agentbot")
        account.set_avatar(str(BOT_DIR / "avatar.png"))
        return account.get_config("addr"), account.get_qr_code()


SERVICE_TEMPLATE = """\
[Unit]
Description=agentbot — Claude Code sessions over Delta Chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Environment=HOME={home}
WorkingDirectory={work_dir}
ExecStart={python} -m agentbot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_service():
    user = os.environ.get("USER", "nobody")
    home = os.path.expanduser("~")
    unit_path = Path("/etc/systemd/system/agentbot.service")
    unit_text = SERVICE_TEMPLATE.format(
        user=user, home=home,
        work_dir=BOT_DIR, python=PYTHON,
    )
    subprocess.run(
        ["sudo", "tee", str(unit_path)],
        input=unit_text.encode(), stdout=subprocess.DEVNULL, check=True,
    )
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    print(f"wrote {unit_path}")


def main():
    if (BOT_DIR / "accounts").exists() and any((BOT_DIR / "accounts").iterdir()):
        sys.exit("accounts/ already populated — provision has already run")

    print("generating avatar...")
    make_avatar(BOT_DIR / "avatar.png")

    print("provisioning on chatmail relay...")
    addr, qr = provision()
    print(f"addr: {addr}")
    print(f"contact QR: {qr}")

    print("installing systemd unit...")
    install_service()

    print("\ndone — start with: sudo systemctl enable --now agentbot.service")


if __name__ == "__main__":
    main()
