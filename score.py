#!/usr/bin/env python3
"""Defence tender watch — step 2: score.
Asks an LLM how relevant each collected notice is to the company profile (1-5) and why.
The model writes ONLY the score, the "why relevant" line, the scope (complete system or
components/services) and, for titles in other languages, a French translation marked "(trad.)"
in the digest. Business caps are applied by code (see SCOPES and the profile's [scoring] table). Every fact (object, buyer, deadline,
supplier) is copied by code from the notice, so the model cannot invent one.
Reads output/<profile>/notices.csv, writes output/<profile>/scored.csv.
Usage: uv run score.py profiles/<profile>.toml [max_new_calls]   (e.g. 3 for a cheap test)
"""
import csv, json, os, re, sys, pathlib, tomllib
from dotenv import load_dotenv
from openai import OpenAI
from config import MODEL, OPENROUTER_BASE_URL

if len(sys.argv) < 2:
    sys.exit("Usage: uv run score.py profiles/<profile>.toml [max_new_calls]")
PROFILE_PATH = pathlib.Path(sys.argv[1])
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None   # e.g. 3 to test cheaply before a full run
PROFILE = tomllib.loads(PROFILE_PATH.read_text(encoding="utf-8"))
OUT = pathlib.Path(__file__).resolve().parent / "output" / PROFILE_PATH.stem
NOTICES, SCORED = OUT / "notices.csv", OUT / "scored.csv"

load_dotenv()   # reads OPENROUTER_API_KEY from the .env file
client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])

# Bump this when the prompt changes: scores made with an older prompt are then redone.
PROMPT_VERSION = "5"
PROMPT = """You assess public tenders for a company.

Company description:
{company}

Tender notice:
- Title: {title}
- Tender's own title (without country and category): {own_title}
- Buyer: {buyer} ({country})
- Notice type: {notice_type}
- Matched keywords: {keywords}
- Description: {description}
- Lots (what is actually bought, lot by lot): {lots}

How relevant is this tender for the company, as a potential bidder or subcontractor?
1 = unrelated (keyword matched by coincidence), 3 = adjacent market, 5 = core business.
Judge only WHAT is being bought. Do not describe or guess the buyer (its type, size or role):
the reader already sees the buyer's name.
Answer with JSON only, no other text:
{{"score": <integer 1-5>,
  "why": "<one sentence IN FRENCH, 20 words maximum, explaining the score>",
  "scope": "<complete_system if the tender buys whole systems (e.g. complete vehicles),
components_or_services if it buys only parts, sub-systems, upgrades, spare parts or services>",
  "title_fr": "<if the tender's own title is neither French nor English: a short French
translation of that own title only, 12 words maximum; otherwise an empty string>"}}
Example of the right length and language for "why":
"Achat de drones sous-marins autonomes : cœur de métier, appel ouvert aux PME.\""""

# Optional business cap from the profile, applied by code so the model cannot override it:
# [scoring] max_score_components = 3  ->  a components-only tender never scores above 3.
SCOPES = {"complete_system", "components_or_services"}
MAX_SCORE_COMPONENTS = PROFILE.get("scoring", {}).get("max_score_components")

MAX_WHY_WORDS = 25   # 20 asked + a small margin; longer answers don't fit the one-page PDF
FIX = 'Your answer has a problem: {problems}. Reply with the same JSON, fixed.'

FR_WORDS = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "pour", "avec", "sans",
            "pas", "au", "aux", "est", "sur", "mais", "ne", "ou", "par", "dans"}
EN_WORDS = {"the", "and", "for", "with", "without", "not", "of", "to", "is", "but", "market",
            "related", "same", "this", "which", "only"}

def looks_french(text):
    """Rough language check by counting common small words: enough to catch an English sentence
    or a sentence that switches to English halfway ("marché adjacent, but not a full vehicle")."""
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    fr = sum(w in FR_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    return en == 0 or en * 2 < fr

def own_title(row):
    """TED titles start with 'Country – Category – '; the tender's own title is what follows."""
    parts = row["title"].split(" – ", 2)
    return parts[2] if row["source"] == "TED" and len(parts) == 3 else row["title"]

def problems_with(why):
    """List what is wrong with a 'why' line (empty list = fine)."""
    problems = []
    if len(why.split()) > MAX_WHY_WORDS:
        problems.append(f'"why" has {len(why.split())} words, 20 maximum')
    if not looks_french(why):
        problems.append('"why" must be entirely in French')
    return problems

def call_report(resp):
    """One line on how the call ended: 'stop' = finished normally, 'length' = ran out of tokens."""
    usage = resp.usage
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) or 0
    return (f"finish={resp.choices[0].finish_reason}, output tokens={usage.completion_tokens} "
            f"(of which reasoning={reasoning})")

def chat(messages):
    """One model call; returns the text of the answer."""
    resp = client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=1000,
        # Turn off hidden "thinking": it used up the token budget before any answer was written.
        extra_body={"reasoning": {"enabled": False}},
        messages=messages)
    print("    " + call_report(resp))
    return (resp.choices[0].message.content or "").strip()

def parse_answer(text):
    """Turn the model's JSON text into (score, why, title_fr, scope); raises an error if it isn't usable."""
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    answer = json.loads(text)
    score = int(answer["score"])
    if not 1 <= score <= 5:
        raise ValueError(f"score out of range: {score}")
    scope = str(answer.get("scope") or "").strip()
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    return score, str(answer["why"]).strip(), str(answer.get("title_fr") or "").strip(), scope

def capped(score, scope):
    """Apply the profile's cap to components-only tenders. This is where the business rule is enforced."""
    if score and scope == "components_or_services" and MAX_SCORE_COMPONENTS:
        return min(score, MAX_SCORE_COMPONENTS)
    return score

def ask_model(row):
    """Return (score, why, title_fr, scope). Score is '' if the model's answer can't be used."""
    # Override the row's own "description" and "lots" so empty ones read clearly in the prompt.
    prompt = PROMPT.format(company=PROFILE["company"]["description"].strip(), own_title=own_title(row),
                           **{**row, "description": row["description"] or "not available",
                              "lots": row.get("lots") or "single lot (see title)"})
    messages = [{"role": "user", "content": prompt}]
    try:
        text = chat(messages)
        answer = parse_answer(text)
        problems = problems_with(answer[1])
        if problems:   # too long or not in French: ask once to fix it
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": FIX.format(problems="; ".join(problems))}]
            try:
                answer = parse_answer(chat(messages))
            except Exception as e:   # keep the first, valid answer rather than losing the score
                print(f"  NOTE {row['source']} {row['id']}: fix failed ({e}), keeping first answer")
        if problems_with(answer[1]):
            print(f"  NOTE {row['source']} {row['id']}: {'; '.join(problems_with(answer[1]))}"
                  " — will be retried next run")
        return answer
    except Exception as e:
        print(f"  WARNING {row['source']} {row['id']}: unusable answer ({e})")
        return "", "", "", ""

def summary(row):
    """Three-line summary built from the notice's own fields, never by the model."""
    return (f"Objet : {row['title']}\n"
            f"Acheteur : {row['buyer']} ({row['country']})\n"
            f"Date limite : {row['deadline'] or 'non précisée'}")

# Reuse scores from a previous run, so re-running only pays for new notices.
# Not reused (scored again): blank score, a 'why' that is too long or not French,
# or a score made with an older prompt.
previous = {}
if SCORED.exists():
    with open(SCORED, encoding="utf-8") as f:
        previous = {(r["source"], r["id"]): r for r in csv.DictReader(f)
                    if r["score"] and r.get("prompt_version") == PROMPT_VERSION
                    and not problems_with(r["why_relevant"])}

with open(NOTICES, encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
if not rows:
    sys.exit(f"No notices in {NOTICES}: run tender_watch.py first.")
print(f"Scoring {len(rows)} notices with {MODEL} ({len(previous)} already scored)")
if LIMIT is not None:
    print(f"Test mode: at most {LIMIT} new model calls")

calls = 0
for i, row in enumerate(rows, 1):
    old = previous.get((row["source"], row["id"]))
    if old:
        answer = int(old["score_raw"]), old["why_relevant"], old["title_fr"], old["scope"]
    elif LIMIT is not None and calls >= LIMIT:
        answer = "", "", "", ""   # left blank, scored on a later run
    else:
        calls += 1
        answer = ask_model(row)
    row["score_raw"], row["why_relevant"], row["title_fr"], row["scope"] = answer
    row["score"] = capped(row["score_raw"], row["scope"])   # the score the digest uses
    if not old and row["score"] != "":
        cap_note = f" (capped from {row['score_raw']})" if row["score"] != row["score_raw"] else ""
        print(f"  [{i}/{len(rows)}] {row['score']}{cap_note}  {row['title'][:70]}")
    row["prompt_version"] = PROMPT_VERSION
    row["summary"] = summary(row)

with open(SCORED, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print(f"Done -> {SCORED}")
