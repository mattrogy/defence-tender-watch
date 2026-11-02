# Decisions

Each entry: the choice, the alternatives considered, and why.

## 1. Generic engine + private profiles

**Choice:** the code knows nothing about any company. A company is described in a TOML profile
(description + keywords). Real profiles live in `profiles/private/` and all results in `output/`,
both git-ignored; only a fictional example profile is public. Lists that grow out of a real watch
(e.g. short names for the buyers it meets) also live in `profiles/private/`.

**Alternatives:** hard-code one company's keywords in the script; or keep the whole repository
private.

**Why:** the same engine serves any defence tech company by adding one file, and the code can be
public without exposing who it is used for or what it found. It also separates the two things that
change at different speeds: the engine (rarely) and the watch list (often).

## 2. Re-check every API hit for the exact keyword

**Choice:** keep each API's broad full-text search (good recall), then keep a notice only if the
keyword appears as whole words (plural and hyphen/space variants allowed) in its text. Rejected
hits are written to `dropped.csv`.

**Alternatives:** trust the API results; or search only in titles (`search(objet, ...)`).

**Why:** both searches are fuzzy: a short keyword such as "AUV" also matches words that merely
start with it ("Auvergne"), and on a typical run most raw API hits are false matches. Title-only
search misses notices whose title is generic. The audit file makes the trade-off measurable:
reviewing it found a real bug (hyphenated keywords) that was then fixed.

## 3. The code writes the facts, the LLM only judges

**Choice:** the model returns only a score (1-5), a one-line reason, the tender's scope (complete
system or components/services) and, for foreign titles, a translation. Object, buyer, deadline and
supplier are copied by code from the official notice.

**Alternatives:** let the model write the full summary of each notice.

**Exception:** titles in languages other than French or English are translated by the model and
always marked "(trad.)", so the reader knows it is not a source fact. The model is also told not to
describe the buyer: an early version guessed what kind of organisation a buyer was, and guessed
wrong, while the buyer's real name was printed just above.

**Why:** a wrong deadline or buyer in a bid digest is costly, and models can invent details.
Limiting the model to a judgement keeps every fact traceable to the source, and makes the output
easy to check: if the score is wrong, the reader still has correct facts.

## 4. Model through OpenRouter, set in one place

**Choice:** OpenAI-compatible client pointed at OpenRouter, model name in `config.py`
(default `anthropic/claude-haiku-5.5`). Hidden reasoning is turned off. The 20-word reason is
asked for, retried once if too long, and cut at 25 words in the PDF.

**Alternatives:** a provider's own SDK; a larger model.

Answers not in French (a simple check counting common French and English words) are sent back
once for a fix. A prompt version number is stored with each score: changing the prompt re-scores
everything on the next run.

**Why:** one line switches provider (e.g. to Mistral). Scoring is a short, repetitive task: a small
model costs about $0.10 per 1,000 notices. Reasoning was turned off after it consumed the whole
token budget and returned empty answers.

## 5. Three scripts linked by CSV files

**Choice:** collect, score and digest are separate scripts; each reads the previous CSV.

**Alternatives:** one script doing everything.

**Why:** each step can be re-run alone (rebuild the PDF without paying for scoring again), and the
CSV in between can be opened in a spreadsheet to check what each step did.

## 6. Merging duplicate publications

**Choice:** calls with the same title (ignoring TED's "Country – Category –" prefix, case, accents
and punctuation) are one tender; the latest publication gives its status, and the **lowest** score
among the copies is kept. Direct awards are grouped one line per buyer, each object listed with its
supplier(s).

**Alternatives:** merge by reference number; no merging; keep the highest score.

**Why:** BOAMP and TED use different reference numbers for the same tender, so only the title is
shared. Without merging, a tender published several times (both sources, corrections) can fill most
of the top 10. The lowest score is the cautious choice when copies disagree. Known trade-off: BOAMP
often gives only a category label where TED gives the full description, so the lowest score can
come from the copy the model knew least about. Direct awards are grouped by buyer because one buyer
can give the same purchase to several suppliers, and the suppliers are what the section is for.

## 7. Five sections, each with one plain rule

**Choice:** open calls need a score of 3+ and more than 2 days left; "Signaux amont" holds only
early-signal notice types (prior information, market consultations, qualification systems); calls
whose deadline has passed get their own short "for information" section; awards appear only if
scored 4+ and naming their winner.

**Alternatives:** one ranked list; or mixing recently closed calls into "Signaux amont" (an earlier
version did this).

**Why:** each section answers one question for the reader: what can I bid on now, what is coming,
what did I just miss, who is winning. A closed call is not an early signal, and a call closing in
2 days is too late to answer. Low scores were removed because they filled slots with noise (e.g.
civil works matched by a keyword used in passing).

## 8. PDF with fpdf2 and built-in fonts

**Choice:** `fpdf2`, standard Helvetica font; characters outside Western European alphabets are
simplified.

**Alternatives:** `reportlab` (heavier API); HTML-to-PDF tools (need a browser engine or system
libraries); bundling a Unicode font file.

**Why:** pure Python, simple API, no system dependency. The cost (e.g. "ł" printed as "l") is
acceptable for a digest whose titles link to the original notice; buyer names that would become
unreadable get a short French name in the private buyer-names file.

## 9. Collector uses only the Python standard library

**Choice:** `urllib` for HTTP calls, with retries on rate limits (HTTP 429) and cut-off downloads.

**Alternatives:** `requests` or `httpx`.

**Why:** one fewer dependency for the step that runs most often; the retry logic is a few
readable lines.

## 10. Business caps are enforced by code, not by the prompt

**Choice:** the model classifies each tender's scope (`complete_system` or
`components_or_services`); the profile sets `max_score_components`; code applies
`min(score, cap)`. Both the raw and the capped score are saved.

**Alternatives:** write the rule in the profile description and trust the model (tried first).

**Why:** written in the description, the rule was followed most of the time but not always: a
spare-parts contract kept a high score although the model's own reason called it components.
Classifying is an easier task for a model than applying a numeric rule consistently, and a rule
applied by code always holds. Keeping the raw score shows what the cap changed, and the cap can be
changed in the profile without paying for a new scoring run.

## 11. Relevance shown as words in the PDF

**Choice:** "forte" (4-5) or "moyenne" (3) instead of "[4/5]"; items scored 1-2 are not printed
in any section.

**Alternatives:** the 1-5 number.

**Why:** a 1-5 number suggests more precision than an LLM judgement has; the reader needs to know
whether to look, not the difference between a 4 and a 5. The numbers stay in `scored.csv` for
sorting and checking.

## 12. Contract modifications are not "recent awards"

**Choice:** award notices of type `can-modif` are left out of "Attributions notables"; awards are
merged again without them. The publication date is labelled "publié le".

**Alternatives:** treat every award notice in the period as a recent award.

**Why:** checked against TED, a contract modification published in the period can concern a
contract signed a year earlier. A modification says the contract still runs, not that the market
just moved; and the publication date is not the award date.

## 13. The model sees the lots, not just the title

**Choice:** lot titles are collected (TED `title-lot`, BOAMP eForms data) and sent to the model
whenever a notice has several lots.

**Alternatives:** score from the title and summary only.

**Why:** a notice's title can describe the end goal ("acquisition of drones") while its lots buy
only components (hulls, motors, sensors, communications). Without the lots, such a tender whose
lots are only components was scored as core business.
