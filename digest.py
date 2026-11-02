#!/usr/bin/env python3
"""Defence tender watch — step 3: digest.
Builds a one-page PDF (in French) from output/<profile>/scored.csv:
  1. "Opportunités ouvertes": top 10 open calls scored 3+ with more than 2 days left (or no deadline),
     by score then deadline;
  2. "Signaux amont": best 3 early signals — prior information (pin-only), market consultations (pmc),
     qualification systems (qu-sy);
  3. "Appels récemment clos (pour info)": best 3 calls whose deadline has passed, one line each;
  4. "Veille concurrentielle": direct awards (VEAT) — buyer, named supplier, object;
  5. "Attributions notables": best 3 contract awards scored 4+ — buyer, winner if known, object.
The same tender is often published several times (BOAMP + TED, corrections): duplicates are merged.
Usage: uv run digest.py profiles/<profile>.toml
"""
import csv, html, re, sys, pathlib, tomllib, unicodedata, datetime as dt
from fpdf import FPDF

if len(sys.argv) < 2:
    sys.exit("Usage: uv run digest.py profiles/<profile>.toml")
PROFILE_PATH = pathlib.Path(sys.argv[1])
PROFILE = tomllib.loads(PROFILE_PATH.read_text(encoding="utf-8"))
OUT = pathlib.Path(__file__).resolve().parent / "output" / PROFILE_PATH.stem
TODAY = dt.date.today()
TOP_N, SIGNALS_N, CLOSED_N, VEAT_N, AWARDS_N = 10, 3, 3, 5, 3
MIN_SCORE_OPEN = 3      # below this, an open call is not worth a slot
MIN_DAYS_LEFT = 2       # a call closing within 2 days is too late to answer
MIN_SCORE_AWARD = 4
MIN_SCORE_SHOWN = 3     # every section: only "forte" (4-5) and "moyenne" (3) items are printed
GENERIC_TITLE_WORDS = 4   # a title this short (e.g. a project code) is replaced by the description

# Early-signal notice types (TED eForms codes), with the label shown in the PDF.
SIGNAL_TYPES = {"pin-only": "Pré-information", "pmc": "Consultation préalable du marché",
                "qu-sy": "Système de qualification"}
# Standard English notice labels, translated by a fixed table (not by the model) when a title
# starts with one: e.g. "Market Dialogue notice: ..." -> "Dialogue avec le marché : ...".
TITLE_PREFIXES_FR = {"Market Dialogue notice": "Dialogue avec le marché"}
# Contract modification: an older contract changed, not a new award.
MODIFICATION_TYPES = {"can-modif"}
# VEAT = voluntary ex-ante transparency: the buyer announces a contract given directly to a supplier.
VEAT_TYPES = {"veat", "Avis en cas de transparence ex ante volontaire"}   # TED code, BOAMP label

# Optional short French names for long buyer names, kept in a private, git-ignored file
# (see README). Without the file, long names are simply cut.
BUYER_NAMES_FILE = pathlib.Path(__file__).resolve().parent / "profiles" / "private" / "buyer_names.toml"
BUYER_SHORT = tomllib.loads(BUYER_NAMES_FILE.read_text(encoding="utf-8"))["names"] if BUYER_NAMES_FILE.exists() else {}

# ---------- Helpers ----------
def short_title(row):
    """TED titles start with 'Country – Category – '; drop that prefix so they match BOAMP titles.
    html.unescape: some titles arrive HTML-encoded ("&amp;" instead of "&")."""
    title = html.unescape(html.unescape(row["title"]))   # twice: some are encoded twice ("&amp;amp;")
    parts = title.split(" – ", 2)
    return parts[2] if row["source"] == "TED" and len(parts) == 3 else title

def dedupe_key(row):
    """Same tender = same title, ignoring case, accents and punctuation."""
    text = unicodedata.normalize("NFKD", short_title(row)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()

def short_buyer(name):
    plain = name.replace("’", "'").lower()
    for long_name, short in BUYER_SHORT.items():
        if plain.startswith(long_name.lower()):
            return short
    return cut(name, 50)

def score_of(row):
    return int(row["score"]) if row["score"] else 0

def cut(text, n):
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."

def cut_words(text, n):
    """Safety net: the model is asked for 20 words but doesn't always obey."""
    words = text.split()
    return text if len(words) <= n else " ".join(words[:n]) + "..."

def relevance(score):
    """The PDF shows a word, not a number: easier to read, and no false precision."""
    if score >= 4:
        return "forte"
    if score == 3:
        return "moyenne"
    return "faible" if score else "non évaluée"

def date_range(first, last):
    """'25/08–04/09/2026' for a range, or one date when first and last are the same day."""
    return fr_date(last) if first == last else f"{fr_date(first)[:5]}–{fr_date(last)}"

def signal_state(t):
    """Notice type, plus the answer deadline when the notice gives one."""
    state = SIGNAL_TYPES[t["notice_type"]]
    if days_left(t) is None:
        return state
    verb = "réponses closes le" if days_left(t) < 0 else "réponses avant le"
    return f"{state}  ·  {verb} {fr_date(t['deadline'])}"

def fr_date(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else "non précisée"

def fixed_translation(t):
    """The model's translation, with standard notice labels replaced by the fixed table."""
    title_fr = t.get("title_fr", "")
    for english, french in TITLE_PREFIXES_FR.items():
        if t["title"].startswith(english) and ":" in title_fr:
            title_fr = f"{french} :{title_fr.split(':', 1)[1]}"
    return title_fr

def display_title(t, n):
    """Title in at most n characters; only the French translation, marked (trad.), when the
    original is in another language."""
    if t.get("title_fr"):
        return f"{cut(fixed_translation(t), n - 8)} (trad.)"
    return cut(t["title"], n)

def object_text(t):
    """What is bought, for one-line entries: the French translation alone when there is one;
    the description when the title is too generic to say anything (e.g. a bare project code)."""
    if t.get("title_fr"):
        return f"{fixed_translation(t)} (trad.)"
    if len(t["title"].split()) <= GENERIC_TITLE_WORDS and t.get("description"):
        return t["description"]
    return t["title"]

# ---------- Load and merge duplicates ----------
with open(OUT / "scored.csv", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
unscored = sum(not r["score"] for r in rows)
if unscored:
    print(f"WARNING: {unscored} notices have no score yet (run score.py again); they rank last.")

def merge(rows):
    """Merge the publications of the same tender (see dedupe_key) into one entry each."""
    groups = {}
    for r in rows:
        groups.setdefault(dedupe_key(r), []).append(r)
    return [merge_group(group) for group in groups.values()]

def merge_group(group):
    latest = max(group, key=lambda r: r["date"])   # latest publication = current state (e.g. award)
    # Lowest score among the scored copies: when copies disagree, stay on the cautious side.
    lowest = min((r for r in group if r["score"]), key=score_of, default=group[0])
    return {
        **latest,
        "title": short_title(latest),
        "title_fr": next((r["title_fr"] for r in group if r.get("title_fr")), ""),
        "description": max((r["description"] for r in group), key=len),   # longest = most informative
        "deadline": latest["deadline"] or max(r["deadline"] for r in group),
        "score": score_of(lowest), "why_relevant": lowest["why_relevant"],
        "sources": " + ".join(sorted({r["source"] for r in group})),
    }

tenders = merge(rows)
print(f"{len(rows)} notices -> {len(tenders)} distinct tenders after merging duplicates")

# ---------- Select ----------
def days_left(t):
    """Days until the deadline; None if the notice gives no deadline."""
    return (dt.date.fromisoformat(t["deadline"]) - TODAY).days if t["deadline"] else None

def by_score_then_deadline(t):
    # Highest score first; among equal scores, the most urgent deadline first (no deadline last).
    return (-t["score"], t["deadline"] or "9999")

def best_recent(items, n, date_field="date"):
    """Highest score first; among equal scores, the most recent date_field first."""
    newest_first = sorted(items, key=lambda t: t[date_field], reverse=True)
    return sorted(newest_first, key=lambda t: -t["score"])[:n]   # sorted() keeps that order on ties

calls = [t for t in tenders if t["status"] == "open"]
top = sorted((t for t in calls if t["score"] >= MIN_SCORE_OPEN
              and (days_left(t) is None or days_left(t) > MIN_DAYS_LEFT)),
             key=by_score_then_deadline)[:TOP_N]
closed = best_recent([t for t in calls if days_left(t) is not None and days_left(t) < 0
                      and t["score"] >= MIN_SCORE_SHOWN], CLOSED_N,
                     date_field="deadline")   # on equal scores, the most recently closed first
signals = sorted((t for t in tenders if t["notice_type"] in SIGNAL_TYPES and t["score"] >= MIN_SCORE_SHOWN),
                 key=by_score_then_deadline)[:SIGNALS_N]
# Awards are merged again WITHOUT modification notices: a 2025 contract modified in 2026 is not a
# recent award. If a tender only has modifications in the period, it does not appear.
new_awards = merge([r for r in rows if r["status"] == "award" and r["notice_type"] not in MODIFICATION_TYPES])
# An award without a named winner tells the reader nothing about the competition: left out.
awards = best_recent([t for t in new_awards if t["score"] >= MIN_SCORE_AWARD and t.get("supplier")], AWARDS_N)

# Direct awards: one line per buyer, listing each object with its supplier(s).
# e.g. {buyer: {"date": latest date, "url": ..., "objects": {object key: (object text, [suppliers])}}}
veats = {}
for r in sorted(rows, key=lambda r: r["date"]):
    if r["notice_type"] not in VEAT_TYPES or score_of(r) < MIN_SCORE_SHOWN:
        continue
    buyer = veats.setdefault(r["buyer"], {"objects": {}, "first": r["date"]})   # rows are in date order
    buyer["date"], buyer["url"] = r["date"], r["url"]   # the last one wins: latest date and link
    text, suppliers = buyer["objects"].setdefault(dedupe_key(r), (object_text({**r, "title": short_title(r)}), []))
    supplier = r.get("supplier") or "fournisseur non précisé"
    if supplier not in suppliers:
        suppliers.append(supplier)
veats = sorted(veats.items(), key=lambda item: item[1]["date"], reverse=True)[:VEAT_N]

# ---------- PDF ----------
def pdf_text(text):
    """The built-in PDF fonts only know Western European characters (latin-1).
    Replace typographic signs, strip accents from other alphabets, '?' as last resort."""
    for a, b in {"’": "'", "‘": "'", "“": '"', "”": '"', "„": '"', "–": "-", "—": "-", "…": "...",
                 "œ": "oe", "Œ": "OE",
                 "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "\u202f": " ", "\u00a0": " "}.items():
        text = text.replace(a, b)
    out = []
    for ch in text:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFKD", ch).encode("latin-1", "ignore").decode("latin-1")
            out.append(base or "?")
    return "".join(out)

pdf = FPDF(format="A4")
pdf.set_margins(12, 10, 12)
pdf.set_auto_page_break(True, margin=10)
pdf.add_page()
W = pdf.epw   # usable page width

def write(text, size, style="", color=(0, 0, 0), h=None, link=""):
    pdf.set_font("Helvetica", style, size)
    pdf.set_text_color(*color)
    pdf.multi_cell(W, h or size * 0.45, pdf_text(text), link=link, new_x="LMARGIN", new_y="NEXT")

def entry(t, extra):
    """Three-line entry: title, facts from the notice, the model's reason."""
    write(display_title(t, 115), 9, "B", (0, 51, 153), link=t["url"])
    write(f"Pertinence : {relevance(t['score'])}  ·  Acheteur : {short_buyer(t['buyer'])} ({t['country']})  ·  "
          f"{extra}  ·  {t['sources']} {t['id']}", 7.5, color=(70, 70, 70))
    write(f"Pourquoi : {cut_words(t['why_relevant'], 25) or 'non évalué'}", 8, "I")
    pdf.ln(1.6)

def line(text, url, wrap=False):
    """One-line entry for the short sections: the text is shortened from the end,
    step by step, until it fits the page width. wrap=True keeps everything (may take 2 lines)."""
    pdf.set_font("Helvetica", "", 7.5)
    n = len(text)
    while not wrap and pdf.get_string_width(pdf_text(cut(text, n))) > W - 2 * pdf.c_margin:   # inner margin
        n -= 2
    write(cut(text, n), 7.5, link=url)
    pdf.ln(0.6)

def section(title):
    pdf.ln(1.5)
    write(title, 11, "B", h=6)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + W, pdf.get_y())
    pdf.ln(1.5)

dates = sorted(r["date"] for r in rows if r["date"])
write(f"Veille appels d'offres — {PROFILE['company']['name']}", 15, "B", h=7)
write(f"Avis publiés du {fr_date(dates[0])} au {fr_date(dates[-1])}  ·  sources BOAMP + TED  ·  "
      f"édition du {fr_date(TODAY.isoformat())}  ·  pertinence (forte / moyenne) évaluée par IA, "
      f"faits issus des avis, (trad.) = traduction automatique", 8, color=(90, 90, 90))

section("Opportunités ouvertes")
for t in top:
    entry(t, f"Date limite : {fr_date(t['deadline'])}")
if not top:
    write("Aucun appel ouvert de pertinence moyenne ou forte.", 8, "I")

section("Signaux amont")
for t in signals:
    entry(t, signal_state(t))
if not signals:
    write("Aucun signal amont sur la période.", 8, "I")

section("Appels récemment clos (pour info)")
for t in closed:
    line(f"Pertinence : {relevance(t['score'])}  ·  clos le {fr_date(t['deadline'])}  ·  {short_buyer(t['buyer'])}  :  {object_text(t)}",
         t["url"])

section("Attributions directes annoncées (sans mise en concurrence)")
for buyer, v in veats:
    objects = "  ;  ".join(f"{text} -> {', '.join(suppliers)}" for text, suppliers in v["objects"].values())
    line(f"publié{'s' if v['first'] != v['date'] else ''} {date_range(v['first'], v['date'])}  ·  "
         f"{short_buyer(buyer)}  :  {objects}", v["url"], wrap=True)   # suppliers matter most
if not veats:
    write("Aucune attribution directe sur la période.", 7.5, "I")

section("Attributions notables")
for t in awards:
    winner = f"  ->  {cut(t['supplier'], 40)}"
    line(f"Pertinence : {relevance(t['score'])}  ·  publié le {fr_date(t['date'])}  ·  {short_buyer(t['buyer'])}{winner}  :  {object_text(t)}",
         t["url"])
if not awards:
    write("Aucune attribution de pertinence forte.", 7.5, "I")

path = OUT / "digest.pdf"
pdf.output(str(path))
pages = pdf.page_no()
print(f"Done -> {path}  ({pages} page{'s' if pages > 1 else ''}"
      f"{', WARNING: more than one page' if pages > 1 else ''})")
