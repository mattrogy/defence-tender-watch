# Defence tender watch

A small tool that watches public procurement notices for a defence tech company and produces a
one-page weekly digest of the opportunities that matter.

It reads two official, free sources:

- **BOAMP**, the French public procurement bulletin;
- **TED**, the EU's Tenders Electronic Daily (all EU countries plus Norway, Switzerland, etc.).

The engine is generic. Everything specific to one company (what it builds, which keywords to
search) lives in a **profile** file, so the same code can serve any company.

## How it works

```
profile.toml ──► 1. collect ──► notices.csv ──► 2. score ──► scored.csv ──► 3. digest ──► digest.pdf
                 (BOAMP+TED)                    (LLM 1-5)                    (1 A4 page)
```

1. **Collect** (`tender_watch.py`): searches both APIs for every keyword in the profile over the
   last N days. Their full-text search is fuzzy ("AUV" also matches "Auvergne"), so every hit is
   re-checked for the exact keyword. Rejected hits are saved in `dropped.csv` to audit the filter.
2. **Score** (`score.py`): an LLM rates each notice from 1 (unrelated) to 5 (core business)
   against the profile's description, and writes a one-line reason in French.
   **The model only writes the score, the reason and title translations (marked "(trad.)").** Every fact (object, buyer, deadline,
   supplier) is copied by code from the official notice, so the model cannot invent one.
3. **Digest** (`digest.py`): a one-page PDF in French with five short sections:
   - *Opportunités ouvertes*: the top 10 open calls scored 3 or more, with more than 2 days left;
   - *Signaux amont*: early signals (prior information, market consultations, qualification
     systems), with their answer deadline when given;
   - *Appels récemment clos*: the best calls that just closed, for information;
   - *Attributions directes annoncées*: contracts given to a named supplier without competition
     (VEAT notices), one line per buyer with the publication dates;
   - *Attributions notables*: the best-scored new contract awards that name their winner
     (contract modifications are left out: they concern older contracts).

   Only items of "forte" or "moyenne" relevance are printed, in every section.

   Titles in languages other than French or English are shown as a short machine translation,
   marked "(trad.)". Long official buyer names are cut, or replaced by short French names from an optional
   private file (see Configuration).

   The same tender is often published several times (in both sources, or corrected); these
   duplicates are merged.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 and the dependencies).

1. Create a `.env` file at the root with an [OpenRouter](https://openrouter.ai) key
   (this file is git-ignored, never commit it):
   ```
   OPENROUTER_API_KEY=sk-or-...
   ```
2. Create a profile: copy `profiles/example_undersea.toml` into `profiles/private/`
   (git-ignored) and edit the company description and keywords.

## Run

```
uv run tender_watch.py profiles/private/<company>.toml 30   # collect the last 30 days
uv run score.py profiles/private/<company>.toml 3           # optional: test scoring on 3 notices
uv run score.py profiles/private/<company>.toml             # score everything
uv run digest.py profiles/private/<company>.toml            # build the PDF
```

Results go to `output/<company>/` (git-ignored):

| File | Content |
|---|---|
| `notices.csv` | all matching notices, one row each |
| `dropped.csv` | API hits rejected by the exact-keyword check |
| `scored.csv` | notices + score, reason and a 3-line factual summary |
| `digest.pdf` | the one-page digest |

Re-running `score.py` only pays for new notices: earlier scores are reused.

## Configuration

- **Model**: set once in `config.py` (default: a Claude model via OpenRouter). Any OpenRouter
  model works, e.g. a Mistral model, by changing one line.
- **Profile** (`profiles/*.toml`): `[company]` name and plain-English description (used by the
  LLM), `[keywords]` lists in French (BOAMP + TED) and English (TED), and an optional `[scoring]`
  table: `max_score_components = 3` caps tenders that buy only parts, sub-systems or services.
  The model says whether a tender is a complete system or components; the cap itself is applied by
  code, so it always holds. `scored.csv` keeps both the model's `score_raw` and the capped `score`.
- **PDF wording**: scores are shown as "Pertinence : forte" (4-5) or "moyenne" (3); items scored
  1-2 are not printed. Standard English notice labels (e.g. "Market Dialogue notice") are translated
  by a fixed table in `digest.py`, not by the model.

- **Buyer short names** (optional): `profiles/private/buyer_names.toml`, git-ignored, with a
  `[names]` table mapping the start of an official name to a short French name, e.g.
  `"Bundesamt für Ausrüstung" = "Office allemand de l'armement (BAAINBw)"`.
  Without the file, long names are simply cut.

## Example results

One 90-day run with an underwater-drone profile (aggregate figures only):

- about 775 raw API hits, of which about 700 were rejected by the exact-keyword check;
- 75 matching notices, merged into about 50 distinct tenders;
- 14 notices with several lots, sent lot by lot to the model;
- about 30 notices of medium or high relevance; two thirds of all notices bought components or
  services rather than complete systems;
- scoring cost: under one cent; the digest fits on one A4 page.

## Limitations

- Keyword search only: a notice that never uses one of the keywords is missed. For TED, the exact
  check reads the title and short description, not the full tender documents, so a few relevant
  notices written in other languages are dropped (see `dropped.csv`). Searching by CPV product
  codes would be the next improvement.
- The LLM score is an opinion to sort the list, not a bid/no-bid decision.
- The PDF uses built-in fonts: letters outside Western European alphabets are simplified
  (e.g. Polish "ł" becomes "l").

Design choices and their alternatives are logged in [DECISIONS.md](DECISIONS.md).
