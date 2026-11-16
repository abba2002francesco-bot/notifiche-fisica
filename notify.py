"""Controlla i feed RSS in feeds.txt e ti manda su Telegram UN DIGEST ordinato per campo.

Formato di feeds.txt (una riga per feed):
    URL
    URL | parola1, parola2, parola3     <- manda solo gli articoli che le contengono

Lo stato (cosa è già stato inviato) sta in seen.json.
"""
import html
import json
import os
import pathlib
import sys
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = pathlib.Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
SEEN_FILE = ROOT / "seen.json"

MAX_ITEMS_PER_DIGEST = 40   # il resto resta in coda per il digest successivo
MAX_SEEN = 5000             # quanti ID ricordare al massimo
TG_LIMIT = 3800             # lunghezza massima di un messaggio (Telegram: 4096)
PREVIEW = os.environ.get("PREVIEW", "").lower() == "true"

# Gli articoli con queste parole vengono segnati con ⭐ e messi in cima al loro campo.
PRIORITY_WORDS = [
    "hawking", "everett", "many-worlds", "many worlds", "information paradox",
    "measurement problem", "decoheren",
]

# Campi: (etichetta, parole chiave). Vince il PRIMO campo che trova una parola
# nel titolo o nell'abstract, quindi l'ordine conta. Modificali come vuoi.
FIELDS = [
    ("🕳️ Gravità e buchi neri", [
        "black hole", "hawking", "gravitational wave", "general relativity",
        "quantum gravity", "spacetime", "buco nero", "buchi neri", "onde gravitazionali"]),
    ("🌌 Cosmologia e astrofisica", [
        "cosmolog", "dark matter", "dark energy", "galax", "supernova", "neutron star",
        "exoplanet", "telescope", "big bang", "cosmic", "universe", "solar system",
        "planet", "jwst", "astro"]),
    ("⚛️ Quantistica", [
        "quantum", "qubit", "entangle", "decoheren", "many-worlds", "everett",
        "bell's", "bell inequalit", "superposition", "quantist"]),
    ("🧲 Materia condensata", [
        "superconduct", "condensate", "topological", "semiconductor", "graphene",
        "magnet", "crystal", "lattice", "superfluid", "vortex", "vortic", "phonon"]),
    ("💥 Particelle e nucleare", [
        "particle", "higgs", "neutrino", "collider", "lhc", "cern", "quark", "muon",
        "nuclear", "nucleus", "boson", "standard model", "antimatter"]),
    ("💡 Ottica e laser", ["laser", "photon", "optic", "plasmon"]),
]
OTHER = "📰 Altro"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
if not TOKEN or not CHAT_ID:
    sys.exit("Mancano i secret TELEGRAM_BOT_TOKEN e/o TELEGRAM_CHAT_ID")


def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    for _ in range(2):
        r = requests.post(url, data=data, timeout=30)
        if r.status_code == 429:  # troppi messaggi: aspetta quanto richiesto
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        if not r.ok:
            print("Errore Telegram:", r.text)
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


def source_name(feed_url, feed_title):
    host = urlparse(feed_url).netloc.lower()
    for key, name in (("quantamagazine", "Quanta"), ("physicsworld", "Physics World"),
                      ("phys.org", "Phys.org"), ("arxiv", "arXiv")):
        if key in host:
            return name
    return feed_title or host


def classify(text):
    for field, words in FIELDS:
        if any(w in text for w in words):
            return field
    return OTHER


def collect_new(feeds, seen, ignore_seen=False):
    """Restituisce la lista degli articoli non ancora inviati (come dizionari)."""
    found, done = [], set()
    for url, keywords in feeds:
        parsed = feedparser.parse(url, agent="physics-notifier/1.0")
        if not parsed.entries:
            print(f"[avviso] nessun articolo da {url}")
            continue
        source = source_name(url, parsed.feed.get("title"))
        preprint = "arxiv" in urlparse(url).netloc.lower()
        batch = []
        for e in parsed.entries:
            uid = e.get("id") or e.get("link")
            if not uid or uid in done or (uid in seen and not ignore_seen):
                continue
            summary = e.get("summary", "")
            # arXiv: ignora le semplici revisioni di articoli già usciti
            if "Announce Type: replace" in summary:
                continue
            title = e.get("title", "(senza titolo)")
            text = (title + " " + summary).lower()
            if keywords and not any(k in text for k in keywords):
                continue
            done.add(uid)
            batch.append({
                "uid": uid,
                "title": " ".join(title.split()),
                "link": e.get("link", uid),
                "source": source,
                "preprint": preprint,
                "field": classify(text),
                "star": any(w in text for w in PRIORITY_WORDS),
            })
        found.extend(batch)
    return found


def format_item(it):
    mark = "⭐" if it["star"] else "•"
    tail = it["source"] + (" · preprint" if it["preprint"] else "")
    link = html.escape(it["link"], quote=True)
    return f'{mark} <a href="{link}">{html.escape(it["title"])}</a> — <i>{html.escape(tail)}</i>'


def build_messages(items):
    """Raggruppa gli articoli per campo e li impacchetta in uno o più messaggi."""
    now = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
    head = f"🆕 <b>{len(items)} nuovi articoli</b> · {now}"

    by_field = {}
    for it in items:
        by_field.setdefault(it["field"], []).append(it)

    messages, buf, size = [], [head], len(head)
    for field in [f for f, _ in FIELDS] + [OTHER]:
        group = by_field.get(field)
        if not group:
            continue
        group.sort(key=lambda it: not it["star"])  # i ⭐ per primi
        lines = [f"\n<b>{field}</b> ({len(group)})"] + [format_item(it) for it in group]
        for i, line in enumerate(lines):
            if size + len(line) + 1 > TG_LIMIT:
                messages.append("\n".join(buf))
                buf = [f"<b>{field}</b> (continua)"] if i > 0 else []
                size = sum(len(x) + 1 for x in buf)
            buf.append(line)
            size += len(line) + 1
    messages.append("\n".join(buf))
    return messages


def main():
    feeds = read_feeds()

    if PREVIEW:
        # Anteprima: digest con gli ultimi articoli, senza toccare lo stato
        items = collect_new(feeds, set(), ignore_seen=True)[:12]
        for msg in build_messages(items):
            send(msg)
            time.sleep(1)
        return

    first_run = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text()) if not first_run else []
    items = collect_new(feeds, set(seen))

    if first_run:
        # Prima esecuzione: segna tutto come già visto, senza inondarti di messaggi
        seen.extend(it["uid"] for it in items)
        send("✅ Notifiche di fisica attive. Da ora ricevi qui le novità.")
    else:
        batch = items[:MAX_ITEMS_PER_DIGEST]
        if batch:
            for msg in build_messages(batch):
                send(msg)
                time.sleep(1)
            seen.extend(it["uid"] for it in batch)
        rimasti = len(items) - len(batch)
        if rimasti > 0:
            print(f"{rimasti} articoli rimandati al prossimo digest")

    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:]))


if __name__ == "__main__":
    main()
