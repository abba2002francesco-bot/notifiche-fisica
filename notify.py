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
import re
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

MAX_ITEMS_PER_DIGEST = 7    # quanti articoli mandare in ogni digest (almeno 1 per campo, se c'è)
MAX_PER_SOURCE = {"arXiv": 2}   # al massimo 2 preprint per digest
SNIPPET_CHARS = 170         # lunghezza della descrizione sotto ogni titolo
TRADUCI = True              # traduce le descrizioni in italiano (se fallisce, resta l'inglese)
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
        "nuclear", "nucleus", "boson", "standard model", "antimatter", "axion", "qcd",
        "hadron", "collision"]),
    ("💡 Ottica e laser", ["laser", "photon", "optic", "plasmon", "ultracold", "optical clock"]),
]
OTHER = "📰 Altro"

# Punteggio di "interesse" per scegliere gli articoli migliori (modifica i pesi come vuoi).
# Gli articoli con le PRIORITY_WORDS (⭐) hanno un forte bonus a prescindere dal campo.
SOURCE_WEIGHT = {"Quanta": 3, "Physics World": 1, "Phys.org": 0, "arXiv": 0}
# Bonus per favorire un campo (es. "⚛️ Quantistica": 2). Vuoto = nessun campo favorito.
FIELD_WEIGHT = {}
PEER_WORDS = ["physical review", "phys. rev", "nature physics", "published in"]

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


def clean_snippet(summary, title):
    """Ricava una breve descrizione leggibile dal riassunto del feed."""
    text = re.sub(r"<[^>]+>", " ", summary)
    text = html.unescape(text)
    text = re.sub(r"arXiv:\S+\s+Announce Type:\s*\S+\s*", "", text)   # intestazione arXiv
    text = re.sub(r"^\s*Abstract:\s*", "", text)
    text = re.sub(r"The post .*? appeared first on .*", "", text, flags=re.S)
    text = " ".join(text.split())
    if not text or text.lower().startswith(title.lower()[:30]):
        return ""
    if len(text) > SNIPPET_CHARS:
        cut = text[:SNIPPET_CHARS]
        dot = cut.rfind(". ")
        cut = cut[:dot + 1] if dot > 80 else cut[:cut.rfind(" ")] + "…"
        text = cut
    return text


_translate_ok = True


def translate(text):
    """Traduzione automatica in italiano; se qualcosa va storto, resta l'originale."""
    global _translate_ok
    if not TRADUCI or not _translate_ok or not text:
        return text
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="it").translate(text) or text
    except Exception as exc:  # rete, blocchi di Google, libreria mancante...
        print("[avviso] traduzione non riuscita, uso l'originale:", exc)
        _translate_ok = False
        return text


def score(it):
    s = 5 if it["star"] else 0
    s += SOURCE_WEIGHT.get(it["source"], 0)
    s += FIELD_WEIGHT.get(it["field"], 0)
    if any(w in it["text"] for w in PEER_WORDS):
        s += 1
    return s


def select(items):
    """Sceglie gli articoli: prima il migliore di ogni campo (quadro completo),
    poi completa con i migliori rimasti. Rispetta il limite per fonte."""
    ranked = sorted(items, key=score, reverse=True)   # a parità, resta l'ordine dei feed
    chosen, taken, per_source = [], set(), {}

    def take(it):
        chosen.append(it)
        taken.add(it["uid"])
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1

    def allowed(it):
        cap = MAX_PER_SOURCE.get(it["source"])
        return cap is None or per_source.get(it["source"], 0) < cap

    fields_done = set()
    for it in ranked:                      # 1) il migliore di ogni campo
        if len(chosen) >= MAX_ITEMS_PER_DIGEST:
            break
        if it["field"] == OTHER or it["field"] in fields_done or not allowed(it):
            continue
        take(it)
        fields_done.add(it["field"])
    for it in ranked:                      # 2) poi i migliori rimasti
        if len(chosen) >= MAX_ITEMS_PER_DIGEST:
            break
        if it["uid"] not in taken and allowed(it):
            take(it)
    return chosen


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
        for e in parsed.entries:
            uid = e.get("id") or e.get("link")
            if not uid or uid in done or (uid in seen and not ignore_seen):
                continue
            summary = e.get("summary", "")
            # arXiv: ignora le semplici revisioni di articoli già usciti
            if "Announce Type: replace" in summary:
                continue
            title = " ".join(e.get("title", "(senza titolo)").split())
            text = (title + " " + summary).lower()
            if keywords and not any(k in text for k in keywords):
                continue
            done.add(uid)
            found.append({
                "uid": uid,
                "title": title,
                "link": e.get("link", uid),
                "source": source,
                "preprint": preprint,
                "field": classify(text),
                "star": any(w in text for w in PRIORITY_WORDS),
                "snippet": clean_snippet(summary, title),
                "text": text,
            })
    return found


def format_item(it):
    mark = "⭐" if it["star"] else "•"
    tail = it["source"] + (" · preprint" if it["preprint"] else "")
    link = html.escape(it["link"], quote=True)
    out = f'{mark} <a href="{link}">{html.escape(it["title"])}</a>\n'
    if it["snippet"]:
        out += f'{html.escape(it["snippet"])} '
    return out + f"<i>({html.escape(tail)})</i>"


def build_messages(items, total):
    """Raggruppa gli articoli per campo e li impacchetta in uno o più messaggi."""
    now = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
    head = f"🆕 <b>{len(items)} articoli scelti</b> su {total} nuovi · {now}"

    by_field = {}
    for it in items:
        by_field.setdefault(it["field"], []).append(it)

    messages, buf, size = [], [head], len(head)
    for field in [f for f, _ in FIELDS] + [OTHER]:
        group = by_field.get(field)
        if not group:
            continue
        group.sort(key=lambda it: not it["star"])  # i ⭐ per primi
        lines = [f"\n<b>{field}</b>"] + [format_item(it) for it in group]
        for i, line in enumerate(lines):
            if size + len(line) + 1 > TG_LIMIT:
                messages.append("\n".join(buf))
                buf = [f"<b>{field}</b> (continua)"] if i > 0 else []
                size = sum(len(x) + 1 for x in buf)
            buf.append(line)
            size += len(line) + 1
    messages.append("\n".join(buf))
    return messages


def send_digest(all_items):
    chosen = select(all_items)
    for it in chosen:
        it["snippet"] = translate(it["snippet"])
    for msg in build_messages(chosen, len(all_items)):
        send(msg)
        time.sleep(1)


def main():
    feeds = read_feeds()

    if PREVIEW:
        # Anteprima: digest con gli articoli più recenti, senza toccare lo stato
        items = collect_new(feeds, set(), ignore_seen=True)
        if items:
            send_digest(items)
        return

    first_run = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text()) if not first_run else []
    items = collect_new(feeds, set(seen))

    if first_run:
        # Prima esecuzione: segna tutto come già visto, senza inondarti di messaggi
        send("✅ Notifiche di fisica attive. Da ora ricevi qui le novità.")
    elif items:
        send_digest(items)

    # Gli articoli non scelti vengono scartati: non ricompaiono nei digest successivi
    seen.extend(it["uid"] for it in items)
    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:]))


if __name__ == "__main__":
    main()
