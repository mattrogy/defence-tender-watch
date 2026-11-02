#!/usr/bin/env python3
"""Defence tender watch — step 1: collect.
Collects public notices (BOAMP France + TED EU) matching a company profile's keywords
over the last N days, dedupes them, and writes output/<profile>/notices.csv for scoring.
Usage: uv run tender_watch.py profiles/<profile>.toml [days=30]   (Python 3.12+, stdlib only)
"""
import csv, json, re, sys, time, pathlib, tomllib, datetime as dt
import urllib.request, urllib.parse, urllib.error

if len(sys.argv) < 2:
    sys.exit("Usage: uv run tender_watch.py profiles/<profile>.toml [days=30]")
PROFILE_PATH = pathlib.Path(sys.argv[1])
PROFILE = tomllib.loads(PROFILE_PATH.read_text(encoding="utf-8"))
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
SINCE = dt.date.today() - dt.timedelta(days=DAYS)
OUT = pathlib.Path(__file__).resolve().parent / "output" / PROFILE_PATH.stem
OUT.mkdir(parents=True, exist_ok=True)

KW_FR = PROFILE["keywords"]["fr"]
KW_EN = PROFILE["keywords"]["en"]
PAGE_SIZE = 100   # max results per API call (both APIs)
MAX_PAGES = 5     # safety cap: at most 500 raw results per keyword and source

def call(url, body=None):
    """GET (or POST if body) a JSON API.
    Retries with a growing pause on HTTP 429 (rate limit) and on cut-off downloads / dropped connections."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "defence-tender-watch/0.1"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                print(f"  HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}")
                return None
            error = "HTTP 429 (rate limit)"
        except Exception as e:   # e.g. IncompleteRead, connection closed: usually temporary
            error = repr(e)[:120]
        time.sleep(2 ** attempt)   # 1, 2, 4, 8, 16 seconds
    print(f"  {error}: still failing after 5 attempts, keyword skipped")
    return None

def pick(v):
    """Flatten TED multilingual / list fields to one string (English, then French, then any)."""
    if isinstance(v, dict):
        for lang in ("eng", "ENG", "fra", "FRA"):
            if lang in v:
                return pick(v[lang])
        return pick(next(iter(v.values()), ""))
    if isinstance(v, list):
        return "; ".join(dict.fromkeys(pick(x) for x in v if x))
    return "" if v is None else str(v)

def all_text(v):
    """Every language and every value of a field, joined — used only for keyword matching."""
    if isinstance(v, dict):
        return " ".join(all_text(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(all_text(x) for x in v)
    return "" if v is None else str(v)

def keyword_pattern(kw):
    """Whole-word match, case-insensitive, each word may take a plural s/x,
    and a space or hyphen between words counts the same ("anti-submarine" = "anti submarine").
    The APIs' own full-text search is fuzzy ("AUV" matches "Auvergne"), so we re-check."""
    words = [re.escape(w) + "[sx]?" for w in re.split(r"[\s-]+", kw)]
    return re.compile(r"\b" + r"[\s-]+".join(words) + r"\b", re.IGNORECASE)

def join_lots(titles):
    """Lot titles as one string, only when a notice really has several lots
    (a single lot usually just repeats the notice title)."""
    titles = list(dict.fromkeys(t.strip() for t in titles if t and t.strip()))
    return " | ".join(titles) if len(titles) > 1 else ""

def boamp_lot_titles(donnees):
    """Lot titles from BOAMP's full notice data. Only notices in the EU 'eForms' format list
    lots this way; older formats return an empty list."""
    try:
        data = json.loads(donnees) if isinstance(donnees, str) else (donnees or {})
        lots = data["EFORMS"]["ContractNotice"]["cac:ProcurementProjectLot"]
    except (KeyError, TypeError, ValueError):
        return []
    if isinstance(lots, dict):   # a single lot comes as one object, not a list
        lots = [lots]
    titles = []
    for lot in lots:
        name = (lot.get("cac:ProcurementProject") or {}).get("cbc:Name")
        titles.append(name.get("#text", "") if isinstance(name, dict) else str(name or ""))
    return titles

notices, raw = {}, {"boamp": [], "ted": []}
dropped = []   # API hits rejected by the exact-keyword check, saved so the filter can be audited

def add(key, row, kw):
    if key in notices:
        notices[key]["keywords"].add(kw)
    else:
        row["keywords"] = {kw}
        notices[key] = row

# ---------- BOAMP (OpenDataSoft API) ----------
BOAMP = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
BOAMP_STATUS = {"Avis de marché": "open", "Résultat de marché": "award"}
BOAMP_FIELDS = ("idweb,objet,nomacheteur,dateparution,datelimitereponse,nature_libelle,"
                "descripteur_libelle,titulaire,url_avis,donnees")   # donnees = full notice text, used for matching
print(f"BOAMP since {SINCE}   (kept = exact keyword match / raw API hits)")
for kw in KW_FR:
    pat, res, total = keyword_pattern(kw), [], 0
    for page in range(MAX_PAGES):
        q = urllib.parse.urlencode({"where": f'"{kw}" AND dateparution >= date\'{SINCE}\'',
                                    "select": BOAMP_FIELDS,
                                    "limit": PAGE_SIZE, "offset": page * PAGE_SIZE,
                                    "order_by": "dateparution desc"})
        data = call(f"{BOAMP}?{q}")
        if data is None:
            break
        total = data.get("total_count", 0)
        res += data.get("results", [])
        if len(res) >= total:
            break
        time.sleep(0.3)
    kept = 0
    for r in res:
        if not pat.search(f"{r.get('objet') or ''} {r.get('donnees') or ''}"):
            dropped.append({"source": "BOAMP", "keyword": kw, "id": r.get("idweb"),
                            "title": r.get("objet", ""), "url": r.get("url_avis", "")})
            continue   # false positive from the fuzzy full-text search
        kept += 1
        raw["boamp"].append(r)
        idw = r.get("idweb") or r.get("id") or r.get("objet")
        nature = r.get("nature_libelle") or ""
        add(f"boamp:{idw}", {
            "source": "BOAMP", "id": idw, "date": r.get("dateparution", ""),
            "title": r.get("objet", ""), "buyer": r.get("nomacheteur", ""), "country": "FR",
            "deadline": (r.get("datelimitereponse") or "")[:10],
            "notice_type": nature, "status": BOAMP_STATUS.get(nature, "other"),
            "description": "; ".join(r.get("descripteur_libelle") or []),
            "lots": join_lots(boamp_lot_titles(r.get("donnees"))),
            "supplier": pick(r.get("titulaire")),   # named winner, on award / direct-award notices
            "url": r.get("url_avis") or f'https://www.boamp.fr/pages/avis/?q=idweb:"{idw}"'}, kw)
    capped = " (capped)" if total > len(res) else ""
    print(f"  {kw!r}: {kept} / {total}{capped}")
    time.sleep(0.3)

# ---------- TED (EU, API v3) ----------
TED = "https://api.ted.europa.eu/v3/notices/search"
FIELDS = ["publication-number", "notice-title", "buyer-name", "buyer-country",
          "publication-date", "deadline-receipt-tender-date-lot", "deadline-receipt-request-date-lot",
          "deadline-receipt-answers-date-lot",
          "notice-type", "description-proc", "description-lot", "title-lot", "organisation-name-tenderer"]

def ted_status(notice_type):
    """TED notice types: cn-* = contract notice (open call), can-* = contract award notice."""
    if notice_type.startswith("cn-"):
        return "open"
    if notice_type.startswith("can-"):
        return "award"
    return "other"

print(f"TED since {SINCE}   (kept = exact keyword match / raw API hits)")
for kw in dict.fromkeys(KW_EN + KW_FR):   # dict.fromkeys = dedupe, keep order ("sonar" is in both lists)
    pat, res, total = keyword_pattern(kw), [], 0
    for page in range(1, MAX_PAGES + 1):
        body = {"query": f'FT ~ ("{kw}") AND PD >= {SINCE:%Y%m%d}', "fields": FIELDS,
                "limit": PAGE_SIZE, "page": page, "paginationMode": "PAGE_NUMBER"}
        data = call(TED, body)
        if data is None:
            break
        total = data.get("totalNoticeCount", 0)
        res += data.get("notices", [])
        if len(res) >= total or not data.get("notices"):
            break
        time.sleep(1)
    kept = 0
    for n in res:
        text = " ".join(all_text(n.get(f)) for f in ("notice-title", "description-proc", "description-lot"))
        if not pat.search(text):
            pn = pick(n.get("publication-number"))
            dropped.append({"source": "TED", "keyword": kw, "id": pn, "title": pick(n.get("notice-title")),
                            "url": f"https://ted.europa.eu/en/notice/-/detail/{pn}"})
            continue   # false positive from the fuzzy full-text search
        kept += 1
        raw["ted"].append(n)
        pn = pick(n.get("publication-number"))
        ntype = pick(n.get("notice-type"))
        add(f"ted:{pn}", {
            "source": "TED", "id": pn, "date": pick(n.get("publication-date"))[:10],
            "title": pick(n.get("notice-title")), "buyer": pick(n.get("buyer-name")),
            "country": pick(n.get("buyer-country")),
            # Open procedures have a deadline for bids; restricted/negotiated ones (common in defence)
            # first have a deadline for requests to participate; market consultations have a
            # deadline for answers. Whichever the notice has, it's the date not to miss.
            "deadline": (pick(n.get("deadline-receipt-tender-date-lot"))
                         or pick(n.get("deadline-receipt-request-date-lot"))
                         or pick(n.get("deadline-receipt-answers-date-lot")))[:10],
            "notice_type": ntype, "status": ted_status(ntype),
            "description": (pick(n.get("description-proc")) or pick(n.get("description-lot")))[:1500],
            "lots": join_lots(pick(n.get("title-lot")).split("; ")),   # what is really bought, lot by lot
            "supplier": pick(n.get("organisation-name-tenderer")),   # named winner, on award / VEAT notices
            "url": f"https://ted.europa.eu/en/notice/-/detail/{pn}"}, kw)
    capped = " (capped)" if total > len(res) else ""
    print(f"  {kw!r}: {kept} / {total}{capped}")
    time.sleep(1)   # TED rate-limits fast callers (HTTP 429)

# ---------- Output ----------
rows = sorted(notices.values(), key=lambda r: r["date"], reverse=True)
cols = ["source", "id", "date", "title", "buyer", "country", "deadline",
        "notice_type", "status", "keywords", "description", "lots", "supplier", "url"]
with open(OUT / "notices.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({**r, "keywords": ", ".join(sorted(r["keywords"]))})
(OUT / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
with open(OUT / "dropped.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["source", "keyword", "id", "title", "url"])
    w.writeheader()
    w.writerows(dropped)
by_status = {s: sum(r["status"] == s for r in rows) for s in ("open", "award", "other")}
print(f"\nDropped by exact-keyword check: {len(dropped)} -> {OUT/'dropped.csv'}")
print(f"Done: {len(rows)} unique notices "
      f"({sum(r['source']=='BOAMP' for r in rows)} BOAMP, {sum(r['source']=='TED' for r in rows)} TED; "
      f"{by_status}) -> {OUT/'notices.csv'}")
