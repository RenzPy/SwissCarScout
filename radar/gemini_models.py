"""Choose a Gemini model from what the API actually offers right now.

WHY THIS EXISTS
---------------
Both projects shipped with "gemini-2.0-flash" hardcoded. Google retired it,
every call returned 404, and because AI features fail soft by design, nothing
visibly broke -- extraction and photo analysis just quietly stopped working.
Hardcoding a newer name only postpones the same failure, so instead the model
list is fetched from the API and a model is chosen by rule.

THE RULE (written against the real model list, not a guess)
-----------------------------------------------------------
1. GEMINI_MODEL set to a specific name and still offered -> use it.
2. Otherwise the newest stable versioned flash: gemini-<version>-flash with
   nothing after it. That excludes -tts, -image, -preview, -lite,
   -transcribe, Omni, Gemma and the rest by construction.
   Versions compare as numbers, so 3.10 correctly beats 3.8.
3. Newest stable flash-lite, only if no plain flash is left.
4. Google's moving alias gemini-flash-latest, only as a last resort -- the
   name never changes when the model underneath does, so a switch behind it
   can't be detected or announced.

Flash rather than pro on purpose: pro's free-tier daily limits are far lower,
and both projects make enough calls to hit them.

NEVER SILENT
------------
Every change of model is announced on Telegram, and so is a pinned model
that has disappeared. Output can change when the model does -- a different
model extracts and writes differently -- and a change you weren't told about
is the same kind of silent failure this module exists to fix.

The choice is cached in gemini_model.json next to the project so listing
costs one request a day, not one per call. The listing call is not a
generation call and doesn't use generation quota. On a 404 from a
generation call, the list is refreshed immediately and the call retried once.

This file is identical in job-scout and SwissCarScout. Keep it that way.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time

import requests

LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
CACHE_PATH = os.environ.get(
    "GEMINI_MODEL_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                 "gemini_model.json"))
RECHECK_SECONDS = 24 * 3600
RETRY_AFTER_FAILURE = 300

_TIERS = (
    (re.compile(r"^gemini-(\d+(?:\.\d+)*)-flash$"), "newest stable flash"),
    (re.compile(r"^gemini-(\d+(?:\.\d+)*)-flash-lite$"),
     "no stable flash offered; newest flash-lite"),
)
ALIAS = "gemini-flash-latest"

_memo: dict = {"model": None, "reason": "", "at": 0.0,
               "failed_at": 0.0, "problem": ""}


# --------------------------------------------------------------- selection

def _norm(name: str | None) -> str:
    name = (name or "").strip()
    name = name[len("models/"):] if name.startswith("models/") else name
    return name or "auto"


def _version(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


def pick(available: list[str], preferred: str = "auto") -> tuple[str | None, str]:
    """(model, reason). Pure function over a list of model names."""
    preferred = _norm(preferred)
    if preferred != "auto" and preferred in available:
        return preferred, "your GEMINI_MODEL setting"
    for rx, why in _TIERS:
        found = [(_version(m.group(1)), n) for n in available
                 if (m := rx.match(n))]
        if found:
            return max(found)[1], why
    if ALIAS in available:
        return ALIAS, "only Google's moving alias left"
    return None, "no usable flash model in the list"


# --------------------------------------------------------------- listing

def list_models(key: str) -> tuple[list[str] | None, str]:
    """(names, problem). Only models that support generateContent.

    Errors report the exception TYPE only, never its message: messages from
    requests can contain the request URL, and past versions of these projects
    leaked keys into logs exactly that way.
    """
    names: list[str] = []
    token = None
    for _ in range(10):                       # pagination safety cap
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        try:
            r = requests.get(LIST_URL, headers={"x-goog-api-key": key},
                             params=params, timeout=30)
        except requests.RequestException as exc:
            return None, f"couldn't reach the Gemini API ({type(exc).__name__})"
        if r.status_code in (400, 401, 403):
            return None, f"the Gemini API rejected the key (HTTP {r.status_code})"
        if not r.ok:
            return None, f"the model list returned HTTP {r.status_code}"
        try:
            data = r.json()
        except ValueError:
            return None, "the model list wasn't valid JSON"
        for m in data.get("models", []) or []:
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                names.append(_norm(m.get("name")))
        token = data.get("nextPageToken")
        if not token:
            break
    return names, ""


# --------------------------------------------------------------- cache

def _load() -> dict | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _save(d: dict) -> None:
    """Atomic write: the listener and the nightly job are separate processes
    and may both refresh the choice; neither may read a half-written file."""
    folder = os.path.dirname(os.path.abspath(CACHE_PATH))
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[ai] couldn't save model cache ({type(exc).__name__})")


# --------------------------------------------------------------- notify

def telegram_notify(text: str) -> None:
    """Plain-text Telegram message from whichever process made the choice.

    Self-contained on purpose, so the nightly job can announce a switch as
    well as the listener. Plain text avoids HTML-parsing failures; errors
    are logged by type only because the token is part of the URL.
    """
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    print(f"[ai] {text}")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    except requests.RequestException as exc:
        print(f"[ai] couldn't send model notice ({type(exc).__name__})")


# --------------------------------------------------------------- resolve

def resolve(key: str, preferred: str = "auto", force: bool = False,
            retired: str | None = None, notify=telegram_notify) -> str | None:
    """The model to use. Refreshes at most daily unless forced; `retired` is
    a model that just returned 404 and must not be chosen again."""
    preferred = _norm(preferred)
    now = time.time()

    if not force:
        if _memo["model"] and now - _memo["at"] < RECHECK_SECONDS:
            return _memo["model"]
        if now - _memo["failed_at"] < RETRY_AFTER_FAILURE:
            cached = _load()
            return (cached or {}).get("model")

    cache = _load()
    if (not force and cache and cache.get("pinned") == preferred
            and cache.get("model")
            and now - float(cache.get("checked_at", 0)) < RECHECK_SECONDS):
        _memo.update(model=cache["model"], reason=cache.get("reason", ""),
                     at=now, problem="")
        return cache["model"]

    names, problem = list_models(key)
    if names is None:
        # Can't list right now. A stale cached choice is far more likely to
        # work than nothing, so keep using it and try again in 5 minutes.
        _memo.update(failed_at=now, problem=problem)
        print(f"[ai] model list unavailable: {problem}")
        return (cache or {}).get("model")

    if retired:
        names = [n for n in names if n != _norm(retired)]

    chosen, reason = pick(names, preferred)
    _save({"model": chosen, "reason": reason, "pinned": preferred,
           "checked_at": now})
    _memo.update(model=chosen, reason=reason, at=now, failed_at=0.0,
                 problem="" if chosen else reason)

    changed = (cache is None or cache.get("model") != chosen
               or cache.get("pinned") != preferred)
    if changed and notify:
        if chosen is None:
            notify("Gemini: no usable model is available, so AI features are "
                   "off until one is. Check GEMINI_API_KEY and the model list.")
        elif preferred != "auto" and chosen != preferred:
            notify(f"Gemini: your GEMINI_MODEL ({preferred}) isn't offered any "
                   f"more, so I'm using {chosen} instead ({reason}). Set "
                   f"GEMINI_MODEL=auto in .env to stop pinning.")
        elif cache and cache.get("model") and cache.get("model") != chosen:
            was = cache["model"]
            why = " — the previous one was retired" if _norm(retired) == was else ""
            notify(f"Gemini model changed: {was} → {chosen} ({reason}){why}. "
                   f"Extraction and drafting may behave slightly differently.")
        else:
            notify(f"Gemini model in use: {chosen} ({reason}).")
    return chosen


def looks_retired(response) -> bool:
    """A generateContent 404 means the model name no longer exists -- the
    rest of that URL is fixed, so there's nothing else it can be."""
    return getattr(response, "status_code", None) == 404


def status() -> dict:
    """For /stats: which model is active, why, and any current problem."""
    cache = _load() or {}
    model = _memo["model"] or cache.get("model")
    checked = cache.get("checked_at")
    return {
        "model": model,
        "reason": _memo["reason"] or cache.get("reason", ""),
        "checked": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(checked))
                   if checked else "never",
        "problem": _memo["problem"],
    }


def _reset_for_tests() -> None:
    _memo.update(model=None, reason="", at=0.0, failed_at=0.0, problem="")
