"""Runtime settings you can change from Telegram.

config.yaml stays the source of defaults — it keeps its comments and the
reasoning behind each number, which is most of its value. Anything you change
from the bot is stored in the database instead and layered on top at load time.
That means `/reset` genuinely restores what's in the file, and a bad value set
from your phone can never corrupt the config.

Only the keys in ALLOWED can be set. A bot token is guessable and this writes
to your settings, so the surface is deliberately small and typed.
"""
from __future__ import annotations

# path -> (type, low, high, human description)
ALLOWED: dict[str, tuple] = {
    "filters.max_price_chf":
        (int, 100, 200_000, "buy budget ceiling"),
    "filters.stale_days":
        (int, 7, 365, "days before a listing counts as stale"),
    "alert_threshold.buy":
        (float, 0, 60, "score needed to alert in buy mode"),
    "alert_threshold.broker":
        (float, 0, 60, "score needed to alert in broker mode"),
    "search.price_from":
        (int, 0, 200_000, "lowest price the hunt searches"),
    "search.price_to":
        (int, 100, 200_000, "highest price the hunt searches"),
}

PREFIX = "override."


def parse_value(path: str, raw: str):
    """Validate a value for a setting. Raises ValueError with a readable
    message, which is sent straight back to the user."""
    if path not in ALLOWED:
        raise ValueError(f"'{path}' is not a settable option")
    typ, lo, hi, _ = ALLOWED[path]
    cleaned = str(raw).replace("'", "").replace("’", "").replace(" ", "")
    try:
        val = typ(float(cleaned))
    except (TypeError, ValueError):
        raise ValueError(f"'{raw}' is not a number") from None
    if not lo <= val <= hi:
        raise ValueError(f"{path} must be between {lo} and {hi}")
    return val


def set_override(conn, path: str, raw):
    val = parse_value(path, raw)
    from . import db
    db.set_state(conn, PREFIX + path, val)
    return val


def clear_overrides(conn) -> int:
    cur = conn.execute("DELETE FROM state WHERE key LIKE ?", (PREFIX + "%",))
    conn.commit()
    return cur.rowcount


def current(conn, cfg: dict) -> list[tuple[str, object, bool]]:
    """(path, effective value, is_overridden) for every settable option."""
    out = []
    for path in ALLOWED:
        ov = _get_override(conn, path)
        out.append((path, ov if ov is not None else _dig(cfg, path),
                    ov is not None))
    return out


def _get_override(conn, path: str):
    from . import db
    raw = db.get_state(conn, PREFIX + path)
    if raw is None:
        return None
    try:
        return parse_value(path, raw)
    except ValueError:
        return None


def _dig(cfg: dict, path: str):
    cur: object = cfg
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def apply_overrides(cfg: dict, conn) -> dict:
    """Layer stored overrides onto a freshly loaded config."""
    for path in ALLOWED:
        val = _get_override(conn, path)
        if val is None:
            continue

        if path.startswith("search."):
            # Applies to every search block, whatever the source calls it.
            field = path.split(".", 1)[1]
            for key in list(cfg):
                if key == "searches" or key.startswith("searches_"):
                    for entry in cfg.get(key) or []:
                        if isinstance(entry, dict):
                            entry[field] = val
            continue

        parts = path.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = val
    return cfg
