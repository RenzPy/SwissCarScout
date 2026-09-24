"""Alerting. Telegram by default; console when --dry-run or unconfigured.

Setup, two minutes:
  1. Message @BotFather on Telegram, send /newbot, follow the prompts.
  2. It gives you a token. Export it:  export TG_TOKEN='123:ABC...'
  3. Send your new bot any message, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and read your chat id.
  4. export TG_CHAT='987654321'
"""
from __future__ import annotations

import html
import os

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, dry_run: bool = False):
        self.token = os.environ.get("TG_TOKEN", "").strip()
        self.chat = os.environ.get("TG_CHAT", "").strip()
        self.dry_run = dry_run or not (self.token and self.chat)
        if self.dry_run and not (self.token and self.chat):
            print("[notify] TG_TOKEN/TG_CHAT not set — printing to console.\n")

    def send(self, text: str) -> bool:
        if self.dry_run:
            print("\n" + "─" * 64)
            print(_strip_tags(text))
            print("─" * 64)
            return True
        try:
            r = requests.post(
                API.format(token=self.token),
                json={"chat_id": self.chat, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": False},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"[notify] telegram error {r.status_code}: {r.text[:200]}")
                return False
            return True
        except requests.RequestException as exc:
            print(f"[notify] telegram unreachable: {exc}")
            return False


def _strip_tags(text: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return html.unescape(text)
