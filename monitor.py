import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

BASE_URL = "https://prod-api.lzt.market"
CONFIG_PATH = Path(__file__).with_name("config.json")

# Railway filesystems are ephemeral unless a Volume is mounted.
# Set DATA_DIR=/app/data and mount a Railway Volume at /app/data.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).with_name("data"))))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "monitor.db"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_first(d: Dict[str, Any], *keys: str, default=None):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def nested_get(d: Dict[str, Any], path: str, default=None):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def normalize_items(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                name = get_first(v, "name", "title", "market_hash_name")
                if name:
                    out.append(str(name))
        return out
    return []


def item_id(item: Dict[str, Any]) -> str:
    return str(get_first(item, "item_id", "id", "itemId", default=""))


def title(item: Dict[str, Any]) -> str:
    return str(get_first(item, "title", "title_en", "name", default="(no title)"))


def price(item: Dict[str, Any]) -> float:
    raw = get_first(item, "price", "rub_price", "price_rub", default=0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def origin(item: Dict[str, Any]) -> str:
    return str(get_first(item, "origin", "account_origin", default="unknown"))


def daybreak(item: Dict[str, Any]) -> int:
    raw = get_first(item, "daybreak", "offline_days", default=0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def change_email(item: Dict[str, Any]) -> str:
    value = get_first(item, "change_email", "can_change_email", default="nomatter")
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).lower()


def email_login_data(item: Dict[str, Any]) -> bool:
    value = get_first(item, "email_login_data", "has_email_login_data", default=False)
    return bool(value)


def registration_years(item: Dict[str, Any]) -> float:
    # API search param is expressed as age; item payloads can vary. These fallbacks are best-effort.
    raw = get_first(item, "reg_years", "registration_years", "account_age_years")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 0.0


def seller_summary(item: Dict[str, Any]) -> str:
    user = get_first(item, "seller", "user", default={})
    if not isinstance(user, dict):
        return "seller: n/a"
    name = get_first(user, "username", "name", default="n/a")
    rating = get_first(user, "rating", "rating_percent", default="n/a")
    reviews = get_first(user, "reviews", "reviews_count", "sold_count", default="n/a")
    return f"seller={name}, rating={rating}, reviews={reviews}"


def extract_inventory(item: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    skin_names = normalize_items(
        get_first(item, "skins", "skin_list", default=nested_get(item, "fortnite.skins"))
    )
    pickaxe_names = normalize_items(
        get_first(item, "pickaxes", "pickaxe_list", default=nested_get(item, "fortnite.pickaxes"))
    )
    dance_names = normalize_items(
        get_first(item, "dances", "dance_list", "emotes", default=nested_get(item, "fortnite.dances"))
    )
    return skin_names, pickaxe_names, dance_names


def count_value(item: Dict[str, Any], *keys: str) -> int:
    raw = get_first(item, *keys, default=0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def score_item(item: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[int, List[str]]:
    blocked = set(cfg.get("blocked_origins", []))
    org = origin(item)
    if org in blocked:
        return -999, [f"blocked origin: {org}"]

    if not email_login_data(item):
        return -999, ["no email login data"]

    score = 0
    reasons: List[str] = []
    w = cfg.get("weights", {})

    db = daybreak(item)
    if db >= 90:
        score += w.get("daybreak_90_plus", 0)
        reasons.append(f"offline {db}d")
    elif db >= 45:
        score += w.get("daybreak_45_89", 0)
        reasons.append(f"offline {db}d")
    elif db >= 25:
        score += w.get("daybreak_25_44", 0)
        reasons.append(f"offline {db}d")

    ce = change_email(item)
    if ce == "yes":
        score += w.get("change_email_yes", 0)
        reasons.append("email change available")

    if org == "personal":
        score += w.get("personal_origin", 0)
        reasons.append("personal origin")
    elif org == "resale":
        score += w.get("resale_origin", 0)
        reasons.append("resale origin")
    elif org == "autoreg":
        score += w.get("autoreg_origin", 0)
        reasons.append("autoreg origin")

    score += w.get("email_login_data", 0)
    reasons.append("email login data")

    age = registration_years(item)
    if age >= 6:
        score += w.get("old_registration_years_6_plus", 0)
        reasons.append(f"age ~{age:g}y")
    elif age >= 3:
        score += w.get("old_registration_years_3_plus", 0)
        reasons.append(f"age ~{age:g}y")

    if str(get_first(item, "stw", "stw_edition", default="")).strip():
        score += w.get("stw", 0)
        reasons.append("STW")
    if bool(get_first(item, "rl_purchases", default=False)):
        score += w.get("rl_purchases", 0)
        reasons.append("Rocket League purchases")
    if str(get_first(item, "xbox_linkable", default="")).lower() == "yes":
        score += w.get("xbox_linkable_yes", 0)
        reasons.append("Xbox linkable")
    if str(get_first(item, "psn_linkable", default="")).lower() == "yes":
        score += w.get("psn_linkable_yes", 0)
        reasons.append("PSN linkable")

    for key, per_10 in [
        (("skins_shop", "skins_shop_count"), w.get("shop_skins_per_10", 0)),
        (("pickaxes_shop", "pickaxes_shop_count"), w.get("shop_pickaxes_per_10", 0)),
        (("dances_shop", "dances_shop_count"), w.get("shop_dances_per_10", 0)),
    ]:
        count = count_value(item, *key)
        if count > 0:
            score += (count // 10) * per_10

    skins, pickaxes, dances = extract_inventory(item)
    haystack = {x.casefold(): x for x in skins + pickaxes + dances}

    def add_rare(group: Dict[str, int], label: str):
        nonlocal score
        for name, pts in group.items():
            if name.casefold() in haystack:
                score += int(pts)
                reasons.append(f"{label}: {name}")

    add_rare(cfg.get("rare_skins", {}), "skin")
    add_rare(cfg.get("rare_pickaxes", {}), "pickaxe")
    add_rare(cfg.get("rare_dances", {}), "dance")

    return score, reasons


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            item_id TEXT PRIMARY KEY,
            first_seen_ts INTEGER NOT NULL,
            title TEXT,
            price REAL,
            score INTEGER,
            payload_json TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def seen_before(con: sqlite3.Connection, iid: str) -> bool:
    if not iid:
        return False
    row = con.execute("SELECT 1 FROM seen WHERE item_id=?", (iid,)).fetchone()
    return row is not None


def mark_seen(con: sqlite3.Connection, item: Dict[str, Any], score: int) -> None:
    iid = item_id(item)
    if not iid:
        return
    con.execute(
        "INSERT OR IGNORE INTO seen(item_id,first_seen_ts,title,price,score,payload_json) VALUES(?,?,?,?,?,?)",
        (iid, int(time.time()), title(item), price(item), score, json.dumps(item, ensure_ascii=False)),
    )
    con.commit()


def telegram_send(text: str, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()


def fetch_items(session: requests.Session, token: str, page: int, pmin: int, pmax: int, min_daybreak: int) -> List[Dict[str, Any]]:
    params = {
        "page": page,
        "pmin": pmin,
        "pmax": pmax,
        "currency": "rub",
        "order_by": "pdate_to_down",
        "email_login_data": "true",
        "daybreak": min_daybreak,
        "change_email": "nomatter",
    }
    # Do not surface accounts whose stated origin is credential theft / compromise.
    query_params = list(params.items())
    for value in ["brute", "phishing", "stealer", "retrieve_via_support"]:
        query_params.append(("not_origin[]", value))

    r = session.get(f"{BASE_URL}/fortnite", params=query_params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "data", "accounts"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def item_link(item: Dict[str, Any]) -> str:
    # LZT payloads may already expose a URL. Prefer it; otherwise leave a stable text ID.
    direct = get_first(item, "url", "link", "market_url")
    if direct:
        return str(direct)
    iid = item_id(item)
    return f"LZT item id: {iid}" if iid else "LZT item"


def format_message(item: Dict[str, Any], score: int, reasons: List[str], label: str) -> str:
    reason_text = ", ".join(reasons[:10]) if reasons else "no extra signals"
    return (
        f"{label} | score {score}\n"
        f"{title(item)}\n"
        f"Price: {price(item):.0f} RUB | offline: {daybreak(item)}d | origin: {origin(item)}\n"
        f"{seller_summary(item)}\n"
        f"Signals: {reason_text}\n"
        f"{item_link(item)}"
    )


def main() -> None:
    load_dotenv()
    token = os.environ.get("LZT_TOKEN", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not tg_token or not chat_id:
        raise SystemExit("Fill LZT_TOKEN, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    poll = max(30, env_int("POLL_INTERVAL_SECONDS", 60))
    pmin = env_int("MIN_PRICE_RUB", 500)
    pmax = env_int("MAX_PRICE_RUB", 2000)
    min_daybreak = env_int("MIN_DAYBREAK_DAYS", 25)
    min_review = env_int("MIN_REVIEW_SCORE", 45)
    min_strong = env_int("MIN_STRONG_SCORE", 70)

    cfg = load_config()
    con = init_db()
    session = requests.Session()
    session.headers.update({"User-Agent": "lzt-safe-monitor-v1/1.0"})

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting monitor: %s-%s RUB, daybreak >= %s, every %ss", pmin, pmax, min_daybreak, poll)

    while True:
        try:
            items = fetch_items(session, token, 1, pmin, pmax, min_daybreak)
            logging.info("Fetched %d items", len(items))
            for item in items:
                iid = item_id(item)
                if iid and seen_before(con, iid):
                    continue

                score, reasons = score_item(item, cfg)
                mark_seen(con, item, score)
                if score < 0:
                    continue

                if score >= min_strong:
                    telegram_send(format_message(item, score, reasons, "STRONG CANDIDATE"), tg_token, chat_id)
                elif score >= min_review:
                    telegram_send(format_message(item, score, reasons, "REVIEW"), tg_token, chat_id)
        except requests.HTTPError as e:
            logging.exception("HTTP error: %s", e)
        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(poll)


if __name__ == "__main__":
    main()
