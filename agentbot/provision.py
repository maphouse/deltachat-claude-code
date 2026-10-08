#!/usr/bin/env python3
"""Provision an agentbot instance in the current directory: config.toml,
chatmail account, logo avatar, systemd service. Prints the owner invite link.
Safe to re-run: finished steps are skipped and the link is printed again."""
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from deltachat_rpc_client import DeltaChat, Rpc

from .bot import RPC_SERVER_PATH

BOT_DIR = Path.cwd()
PYTHON = sys.executable
CHATMAIL_QR = "DCACCOUNT:https://chtml.ca/new"
LOGO = Path(__file__).parent / "logo.png"
CONFIG_TEMPLATE = Path(__file__).parent / "config.example.toml"
UNIT_PATH = Path("/etc/systemd/system/agentbot.service")


def write_config():
    """config.toml from the packaged example, rooted at this user's home."""
    path = BOT_DIR / "config.toml"
    if path.exists():
        print(f"keeping existing {path}")
        return
    home = os.path.expanduser("~")
    path.write_text(CONFIG_TEMPLATE.read_text().replace("/home/you", home))
    print(f"wrote {path} (sessions start in {home}; edit default_cwd/allowed_roots to change)")


def provision() -> tuple[str, str]:
    accounts_dir = str(BOT_DIR / "accounts")
    with Rpc(accounts_dir=accounts_dir, rpc_server_path=RPC_SERVER_PATH) as rpc:
        dc = DeltaChat(rpc)
        accounts = dc.get_all_accounts()
        if accounts:
            account = accounts[0]
            print("keeping existing Delta Chat account")
        else:
            print("creating Delta Chat account on chatmail relay...")
            account = dc.add_account()
            account.set_config("bot", "1")
            account.set_config_from_qr(CHATMAIL_QR)
            account.configure()
            account.set_config("displayname", "agentbot")
            account.set_avatar(str(LOGO))
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
Environment=PATH={path}
WorkingDirectory={work_dir}
ExecStart={python} -m agentbot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_service() -> bool:
    """Write the unit with sudo; without sudo, leave it next to config.toml
    for an admin to install. PATH is captured so the service finds the same
    `claude` binary this shell does."""
    unit_text = SERVICE_TEMPLATE.format(
        user=getpass.getuser(), home=os.path.expanduser("~"),
        path=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        work_dir=BOT_DIR, python=PYTHON,
    )
    try:
        subprocess.run(["sudo", "tee", str(UNIT_PATH)], input=unit_text.encode(),
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        local = BOT_DIR / "agentbot.service"
        local.write_text(unit_text)
        print(f"\ncouldn't install the systemd unit (no sudo?) — wrote {local}.\n"
              f"Have an admin run:\n"
              f"  sudo cp {local} {UNIT_PATH}\n"
              f"  sudo systemctl daemon-reload")
        return False
    print(f"wrote {UNIT_PATH}")
    return True


def main():
    if not shutil.which("claude"):
        print("warning: `claude` isn't on PATH — install Claude Code and log in "
              "(run `claude` once) as this user before starting the bot")
    write_config()
    addr, link = provision()
    installed = install_service()
    print(f"\nbot address: {addr}")
    print("\nOwner invite link — open it in Delta Chat (paste into New Chat or tap it")
    print("on your phone). The first person to join through it becomes the bot's")
    print("owner, with full shell access as this user, so don't share it:\n")
    print(f"  {link}\n")
    if installed:
        print("start the bot: sudo systemctl enable --now agentbot.service")
    else:
        print("once the unit is installed: sudo systemctl enable --now agentbot.service")
    print("test in the foreground instead: deltachat-claude-code")


if __name__ == "__main__":
    main()
