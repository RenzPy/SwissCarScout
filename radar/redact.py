"""Strip secrets from text before it is printed or sent anywhere.

The Telegram bot token is part of every Bot API URL -- Telegram offers no
header alternative -- and `requests` puts the full request URL into its
exception messages. Printing an exception as-is therefore wrote the token
into journald. Everything that prints or sends an exception from Telegram
code paths goes through redact() first.

Two layers: the actual secret values from the environment are replaced
wherever they appear, and anything *shaped* like a Telegram token or a
Google API key is replaced too, in case a value arrives by some other route.
"""
from __future__ import annotations

import os
import re

_ENV_SECRETS = ("TG_TOKEN", "GEMINI_API_KEY")

_SHAPES = (
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),   # Telegram bot token
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),         # Google API key
)


def redact(text) -> str:
    text = str(text)
    for var in _ENV_SECRETS:
        value = os.environ.get(var, "").strip()
        if len(value) >= 8:
            text = text.replace(value, "***")
    for rx in _SHAPES:
        text = rx.sub("***", text)
    return text
