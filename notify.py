"""Controlla i feed RSS elencati in feeds.txt e manda le novità su Telegram.

Formato di feeds.txt (una riga per feed):
    URL
    URL | parola1, parola2, parola3     <- manda solo gli articoli che le contengono

Lo stato (cosa è già stato inviato) sta in seen.json.
"""
import json
import os
import pathlib
import sys
import time

import feedparser
import requests

ROOT = pathlib.Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
SEEN_FILE = ROOT / "seen.json"

MAX_MESSAGES_PER_RUN = 15   # il resto viene mandato alla run successiva
MAX_SEEN = 5000             # quanti ID ricordare al massimo

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
if not TOKEN or not CHAT_ID:
    sys.exit("Mancano i secret TELEGRAM_BOT_TOKEN e/o TELEGRAM_CHAT_ID")


def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for _ in range(2):
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
        if r.status_code == 429:  # troppi messaggi: aspetta quanto richiesto
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Telegram continua a rispondere 429")


def read_feeds():
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, kw = line.partition("|")
        keywords = [k.strip().lower() for k in kw.split(",") if k.strip()]
        feeds.append((url.strip(), keywords))
    return feeds


def collect_new(feeds, seen):
    """Restituisce la lista di (uid, testo) degli articoli non ancora inviati."""
    found = []
    for url, keywords in feeds:
        parsed = feedparser.parse(url, agent="physics-notifier/1.0")
        if not parsed.entries:
            print(f"[avviso] nessun articolo da {url}")
            continue
        source = parsed.feed.get("title", url)
        batch = []
        for e in parsed.entries:
            uid = e.get("id") or e.get("link")
            if not uid or uid in seen:
                continue
            summary = e.get("summary", "")
            # arXiv: ignora le semplici revisioni di articoli già usciti
            if "Announce Type: replace" in summary:
                continue
            haystack = (e.get("title", "") + " " + summary).lower()
            if keywords and not any(k in haystack for k in keywords):
                continue
            batch.append((uid, f"{source}\n{e.get('title', '(senza titolo)')}\n{e.get('link', uid)}"))
        found.extend(reversed(batch))  # dal più vecchio al più recente
    return found


def main():
    first_run = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text()) if not first_run else []
    seen_set = set(seen)

    new_items = collect_new(read_feeds(), seen_set)

    if first_run:
        # Prima esecuzione: segna tutto come già visto, senza inondarti di messaggi
        seen.extend(uid for uid, _ in new_items)
        send("✅ Notifiche di fisica attive. Da ora ricevi qui le novità.")
    else:
        for uid, text in new_items[:MAX_MESSAGES_PER_RUN]:
            send(text)
            seen.append(uid)
            time.sleep(1)
        rimasti = len(new_items) - MAX_MESSAGES_PER_RUN
        if rimasti > 0:
            print(f"{rimasti} articoli rimandati alla prossima esecuzione")

    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:]))


if __name__ == "__main__":
    main()
