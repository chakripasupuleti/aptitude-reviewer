# Aptitude Content Reviewer — Complete Project Documentation

  

This document describes, in full detail, what this project does, how it is built, every check it runs, what it logs, and where each piece of data goes. It is meant to be the single source of truth for understanding the system end to end.

  

---

  

## 1. What this project does

  

It is a web app + CLI tool that automatically reviews aptitude MCQ (multiple-choice question) content — the kind used in exam prep — for correctness before publishing. A reviewer uploads an Excel file of questions; the system checks each row for math errors, key mismatches, ambiguous wording, gender/pronoun inconsistencies, formatting errors, etc., and returns an annotated file plus a web dashboard of results.

  

The review pipeline has **two tiers**:

  

| Tier | Name | What it is | Cost |

|---|---|---|---|

| 1 | **V1** | Pure Python, rule-based / regex / arithmetic checks. No AI involved. | Free, instant |

| 2 | **V2** | A single LLM call (one "reasoning agent") that catches issues V1 can't — ambiguity, wrong formulas, invalid assumptions, etc. | Costs API tokens |

  

V1 always runs. V2 is optional (toggle in the UI / `--use-llm` flag) and is what later sections refer to as "the agent."

  

---

  

## 2. Tech stack — pinned versions

  

Installed in `venv/`, declared in [requirements.txt](requirements.txt) (unpinned there; exact resolved versions below):

  

| Package | Version | Purpose |

|---|---|---|

| `pandas` | 3.0.3 | Reading/writing Excel/CSV, row iteration |

| `openpyxl` | 3.1.5 | `.xlsx` read/write engine, cell styling on export |

| `sympy` | 1.14.0 | Available for symbolic math (imported transitively; arithmetic checks mostly use `fractions.Fraction` directly) |

| `requests` | 2.34.2 | HTTP calls to OpenRouter |

| `python-dotenv` | 1.2.2 | Loads `.env` into environment variables |

| `anthropic` | 0.104.1 | Installed but **not used directly** — all LLM calls go through OpenRouter's REST API via `requests`, not the Anthropic SDK |

| `fastapi` | 0.136.1 | Web server framework |

| `uvicorn` | 0.47.0 | ASGI server that runs FastAPI |

| `python-multipart` | 0.0.29 | Required by FastAPI to parse uploaded file form-data |

| `gspread` | 6.2.1 | Google Sheets client |

| `google-auth` | 2.53.0 | Service-account auth for Google Sheets |

  

Frontend: a single static HTML file ([frontend/index.html](frontend/index.html), 896 lines) using **Tailwind CSS** (via CDN) and vanilla JavaScript — no build step, no npm, no framework (React/Vue). FastAPI serves it directly as a static file via `StaticFiles`.

  

LLM gateway: **OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`), not a direct Anthropic/OpenAI API call. This lets the model string be swapped (Claude, Mistral, etc.) without code changes.

  

---

  

## 3. Repository layout

  

```

aptitude-reviewer/

├── app.py # CLI entry point + core review-orchestration functions

├── server.py # FastAPI web server (the API the frontend talks to)

├── requirements.txt

├── .env / .env.example # Secrets (gitignored) / template

├── .gitignore

├── README.md

├── ARCHITECTURE.md # High-level architecture overview (diagrams, design principles)

├── PROJECT_DOCUMENTATION.md # This file — exhaustive per-module reference

├── credentials.json # Google service-account key (gitignored)

├── frontend/

│ └── index.html # Entire SPA UI (upload, processing, results, dashboard views)

├── evals/

│ ├── run_evals.py # Accuracy eval harness against hand-labeled ground truth

│ ├── evals_1.xlsx # Ground-truth eval set 1 (gitignored, kept locally)

│ └── evals_2.xlsx # Ground-truth eval set 2

└── reviewer/ # The actual review engine (all V1 + V2 logic)

├── __init__.py

├── constants.py # Column names, severity order, status strings

├── models.py # ReviewIssue dataclass

├── normalize.py # Column-name aliasing + cell value cleanup

├── ingest.py # CSV/XLSX file reading

├── field_checks.py # Required-field presence checks

├── key_checks.py # Key format validity ("Option A".."Option D")

├── option_equivalence.py # Value parsing/equivalence (currency, ratio, fraction, units) + duplicate-option detection

├── currency_checks.py # "$" misused as currency vs LaTeX math delimiter

├── latex_checks.py # Unbalanced/unwrapped LaTeX detection

├── arithmetic_checks.py # Detects wrong arithmetic inside the Explanation text

├── final_answer_checks.py # Explanation's final answer vs Key/Options consistency

├── data_mismatch_checks.py # Question-vs-Explanation factual consistency (ranks, names, directions)

├── topic_checks.py # ~20 topic-specific formula validators (profit/loss, SI/CI, speed, pipes, etc.)

├── grammar_checks.py # Ordinal suffixes, spacing, repeated words, AI "verification step" artifacts

├── gender_relation_checks.py # Pronoun/gender consistency, blood-relation gender-assumption checks

├── issue_filters.py # Final false-positive suppression + Key Mismatch inference gate

├── consolidator.py # Dedup, sort, and roll issues up into one Agent Status/remarks block

├── metrics.py # Confusion-matrix (TP/FP/FN/TN, precision/recall/F1) for the dashboard

├── export.py # Writes the final reviewed .xlsx/.csv

├── python_sandbox.py # Subprocess-isolated arithmetic evaluator (fallback for the AST evaluator)

├── llm_client.py # OpenRouter HTTP client (model config, retries)

├── llm_prompts.py # System prompt + rubric + user-prompt builders for V2

├── llm_reasoning_checks.py # V2 orchestration: calls the LLM, validates/cleans its output

├── llm_json.py # Robust JSON object/array extraction from raw LLM text output

└── sheets_logger.py # Google Sheets logging (Runs / Errors / Feedback sheets)

```

  

---

  

## 4. Required input columns

  

Every uploaded file must contain (case/spacing-flexible — see §6 normalization):

  

```

S. No | Question or Question Content | Option A | Option B | Option C | Option D | Explanation | Key

```

  

`Key` must be exactly one of `Option A`, `Option B`, `Option C`, `Option D`.

  

An optional ground-truth column (`Has Issue`, `Ground Truth`, `Seeded Issue`, etc.) can be included — if present, the dashboard computes real precision/recall/F1 against it instead of just showing flag counts.

  

---

  

## 5. End-to-end data flow

  

```

1. User uploads .xlsx/.xls (max 10 MB) via the web UI

│

2. POST /api/jobs (server.py)

→ file saved to a temp dir, a job_id (uuid) is created,

a background thread starts _run_job()

│

3. _run_job():

a) read_input() — pandas reads the file

b) normalize_columns() — column-name aliases resolved

c) normalize_input_values() — fixes Excel date-coercion artifacts in option cells

d) missing_required_columns() — abort if required columns absent

e) Phase 1: for every row, run all 12 V1 checks (deterministic, in-process, fast)

f) Phase 2 (if "Use LLM" is on): batch rows into groups of 40,

send each batch as ONE OpenRouter API call to the V2 reasoning agent

g) Phase 3: merge V1 + V2 issues per row → filter known false positives →

consolidate into Agent Status / Error Types / Remarks / Suggested Corrections

h) compute confusion metrics (if ground truth present)

i) write the annotated output .xlsx (original columns + review columns appended)

j) best-effort log_run() to Google Sheets "Runs" sheet

│

4. Frontend polls GET /api/jobs/{id} for progress, then GET /api/jobs/{id}/results

│

5. User reviews each row in the UI, can Approve/Reject with remarks

→ POST /api/jobs/{id}/feedback → log_feedback() to Google Sheets "Feedback" sheet

│

6. User downloads the reviewed file via GET /api/jobs/{id}/download

```

  

The CLI path (`python app.py input.xlsx output.xlsx --use-llm`) runs the same V1/V2 logic via `review_file()` in [app.py](app.py), batching LLM calls 5 rows at a time by default (configurable via `--batch-size`), with a configurable delay between batches (`--llm-delay`, default 0.5s, env `OPENROUTER_ROW_DELAY_SECONDS`).

  

---

  

## 6. Column normalization (`reviewer/normalize.py`)

  

Input column headers are matched case/punctuation-insensitively. `_compact()` strips everything except letters and digits, then looks up the alias table:

  

| Accepted header variants | Canonical column |

|---|---|

| `S. No`, `SNo`, `Sl No`, `Serial No`, `Serial Number` | `S. No` |

| `Question`, `Question Content`, `Question Text`, `Ques` | `Question` |

| `Option A` / `Opt A` / `A` (and B/C/D) | `Option A`..`Option D` |

| `Explanation`, `Solution` | `Explanation` |

| `Key`, `Answer Key`, `Correct Option` | `Key` |

  

`normalize_input_values()` also repairs a common Excel artifact: an option cell intended to be a small integer like `24` that Excel silently coerced into a date (`1900-01-24 00:00:00`) is converted back to `24`.

  

---

  

## 7. V1 — Deterministic checks (no AI)

  

All of these run in-process, in milliseconds, for every row. They are pure Python/regex/arithmetic — there is no model call anywhere in V1. Each check returns a list of `ReviewIssue` (severity, error_type, field, evidence, reason, suggested_fix, confidence).

  

Run in this order inside `_run_v1_checks()` (in both [app.py](app.py) and [server.py](server.py)):

  

1.  **`check_required_fields`** ([field_checks.py](reviewer/field_checks.py)) — flags missing Question/Option/Key, missing or too-short (<15 chars) Explanation.

2.  **`check_key_format`** ([key_checks.py](reviewer/key_checks.py)) — Key must be exactly `Option A`–`Option D`, and that option cell must not be empty.

3.  **`check_duplicate_options`** ([option_equivalence.py](reviewer/option_equivalence.py)) — flags options that are textually identical OR mathematically equivalent (e.g. `0.5` vs `1/2` vs `50%`).

4.  **`check_currency_symbol`** ([currency_checks.py](reviewer/currency_checks.py)) — flags `$` used as a currency sign (should be "rupees"/"dollars" in words), while still allowing `$...$` as LaTeX math delimiters.

5.  **`check_latex_formatting`** ([latex_checks.py](reviewer/latex_checks.py)) — flags unbalanced `$...$` pairs and LaTeX commands (`\frac`, `\times`, `\sqrt`, etc.) used outside `$...$`.

6.  **`check_arithmetic`** ([arithmetic_checks.py](reviewer/arithmetic_checks.py)) — the biggest deterministic check. Parses arithmetic equality statements straight out of the Explanation text (e.g. `12 + 43 = 54`) and **actually evaluates them** using a restricted AST-walker (falls back to a subprocess sandbox, see §7a) to catch wrong calculations, wrong percentage math, and wrong unit conversions. Tolerant of LaTeX (`\frac{}{}`), Unicode operators (`×`, `÷`, `−`), comma thousands separators, and ordinary display rounding.

7.  **`check_final_answer_and_key`** ([final_answer_checks.py](reviewer/final_answer_checks.py)) — extracts the "final answer" stated in the Explanation (via "hence/therefore/answer is" phrasing, or the last bare `= VALUE` line) and checks it actually matches the option the Key points to. Also checks: does the explanation's *body* (an intermediate "12 hours") contradict its own *conclusion* ("11 hours 40 minutes")?

8.  **`check_data_mismatch`** ([data_mismatch_checks.py](reviewer/data_mismatch_checks.py)) — for ranking/seating-style questions, verifies the Explanation uses the same names, positions, and directions stated in the Question (catches name swaps, direction flips, wrong gender labels in counts). Name detection is deliberately conservative: a capitalized word is only treated as a candidate person-name if it isn't a common English word (an explicit blocklist covering conjunctions, sentence-starters, etc.) and doesn't end in "-ly" (sentence adverbs like "Initially"/"Finally"/"Consequently" get capitalized mid-explanation but are never person names) — this closes off a whole class of false positives instead of only the specific words seen so far. See §8.8 for the additional LLM-backed safety net on this specific check.

9.  **`check_topic_specific`** ([topic_checks.py](reviewer/topic_checks.py)) — ~20 independent formula validators that recompute the expected answer from the question text and compare it to the Explanation/Key. Covers: alligation/mixture ratios, ranking "total students"/"below-above count" formulas (reads the direction from the actual phrase being asked, not just keyword presence anywhere in the question), profit %, percentage increase, averages, speed (skips round-trip/average-speed questions where a single leg's distance/time isn't the final answer), simple interest, compound interest, clock angles, time & work, pipes & cisterns, discount→SP, average speed (round trip), HCF/LCM, probability bounds, divisibility by 9, coordinate/direction tracing, plus wording-ambiguity checks (age past/future wording; ambiguous pronoun references — skipped when the two named people have known, opposite genders that already disambiguate the pronoun, e.g. "Mohan told Sita that her...").

10.  **`check_grammar_basics`** ([grammar_checks.py](reviewer/grammar_checks.py)) — wrong ordinal suffixes (`2th` → `2nd`), missing space after punctuation, repeated words, singular "position" where plural is needed.

11.  **`check_verification_step`** ([grammar_checks.py](reviewer/grammar_checks.py)) — flags leftover "Let's verify:" / "Cross-check:" sections — an AI-generation artifact that shouldn't appear in a published explanation.

12.  **`check_gender_relation`** ([gender_relation_checks.py](reviewer/gender_relation_checks.py)) — pronoun-vs-stated-gender mismatches (explicit `"X is a boy"` + later `"her"`), and a curated list of ~70 strongly gendered Indian names used to catch implicit mismatches. Includes a **nearest-antecedent guard**: a known-gendered name is only flagged for a pronoun mismatch when no *other* name appears between it and the pronoun (same sentence) or anywhere else in the question (cross-sentence) — otherwise that closer name, not the known one, is the more likely antecedent (e.g. "Priya's brother Ravi says he is happy" correctly attributes "he" to Ravi, not Priya). For blood-relation questions specifically, flags explanations that **assume** an unstated gender to conclude uncle/aunt/grandfather/grandmother from a generic "sibling"/"parent"/"child" relation.

  

### 7a. Arithmetic evaluation internals

  

`evaluate_expression()` first tries a restricted Python `ast` walker that only allows `+ - * / unary-minus` on numeric constants (rejects anything else — no function calls, no names, nothing unsafe). If that AST walk fails for a string that still looks purely arithmetic, it falls back to `python_sandbox.safe_eval_expr()`, which runs the expression in a **separate subprocess** (`python -c "..."`) with a 2-second timeout, after first validating the string against a strict `^[\d\s+\-*/().,]+$` regex — this is the safety boundary against code injection via question content.

  

### 7b. Value equivalence engine (`option_equivalence.py`)

  

Used everywhere a "does this answer match this option" comparison is needed. Recognizes and cross-compares:

- Plain numbers, decimals, fractions (`3/4`), mixed fractions (`1 1/2`), LaTeX fractions (`\frac{1}{2}`)

- Percentages (`50%`)

- Ratios (`3:4`, normalized to lowest terms)

- Currency (`$13`, `13 dollars`, `₹100`, `Rs. 50`, `100₹` — with thousands-separator commas stripped: `₹12,000` → `12000`)

- Quantities with units (time, length, weight, volume, angle) — converted to a common base unit (seconds, metres, grams, litres, degrees) so `90 minutes` matches `1.5 hours`

- Cross-type matching: a plain number `12000` is treated as equal to the currency value `₹12,000`

  

---

  

## 8. V2 — The LLM Reasoning Agent

  

This is the only part of the system that calls an AI model. It exists specifically to catch things regex/arithmetic can't: ambiguity, invalid hidden assumptions, wrong-operation formula errors, question/explanation mismatches that aren't simple value swaps.

  

### 8.1 Model & gateway

  

-  **Gateway:** OpenRouter REST API (`reviewer/llm_client.py`), not a direct provider SDK.

-  **Default model:**  `anthropic/claude-sonnet-4-6` ([llm_client.py:15](reviewer/llm_client.py#L15)), overridable via the `OPENROUTER_MODEL` env var or `--llm-model` CLI flag.

-  **Auth:**  `OPENROUTER_API_KEY` env var (Bearer token).

-  **Retry policy:** up to 4 retries with exponential backoff (or `Retry-After` header) on HTTP 429/500/502/503/504.

-  **Temperature:**  `0.0` (deterministic-as-possible output) for both single-row and batch calls.

  

### 8.2 How it's invoked

  

-  **Web app:** rows are batched **40 at a time** ([server.py:128](server.py#L128), `_BATCH_SIZE`) — one OpenRouter call reviews 40 questions at once via `run_llm_reasoning_batch()`.

-  **CLI:** batches of 5 by default (`--batch-size`), with a configurable delay between batches.

- Each row is only sent if "Use LLM" was enabled for that job (`use_llm` form field / `--use-llm` flag).

  

### 8.3 The tool: `verify_calculation`

  

The agent is given exactly one tool ([llm_reasoning_checks.py:15-36](reviewer/llm_reasoning_checks.py#L15)):

  

```

verify_calculation(expression: str) -> exact numeric result

```

  

This exists so the model never has to trust its own mental arithmetic before flagging a calculation as wrong — it can call this tool (which runs through the same `evaluate_expression()` used by V1) and get an exact `Fraction`-based answer. The system prompt explicitly instructs: *"Do not rely on mental arithmetic for calculations... call the verify_calculation tool before deciding to flag it."* The tool-call loop allows up to 4 round-trips (`_MAX_TOOL_TURNS`) before giving up and returning whatever state exists.

  

(Note: tool calling is only wired into the single-row path `run_llm_reasoning()`. The batch path `run_llm_reasoning_batch()` does **not** pass tools — it's a single plain completion call per batch.)

  

### 8.4 The prompt (`reviewer/llm_prompts.py`)

  

Three parts assembled into the request:

  

1.  **`SYSTEM_PROMPT`** — defines the agent's role ("Aptitude Content Review Agent"), and ~25 explicit rules, e.g.:

- Never invent facts not in the question

- Never infer gender from a name alone in blood-relation questions

- Return **at most 2 issues** per question, the most important first

- Don't propose a "suggested_fix" for complex topics (ranking/clock/blood-relation/direction) unless certain

- Specific worked examples (Formula Error, Invalid Assumption, Ambiguity, Wrong Substitution, Question-Explanation Mismatch) the model must pattern-match against

2.  **`RUBRIC_GUIDANCE`** — a detailed rubric (R1–R8) classifying issue types (Formula/reasoning validity, Wrong substitution, Solvability/ambiguity, Question-explanation mismatch, Invalid assumption, Final conclusion support, Clarity, Missing formula) plus topic-specific conventions (ranking count vs. position formulas, clock-angle formula, alligation ratio direction, age past/future sign-checking, etc.)

3.  **User prompt** (`build_user_prompt` / `build_batch_user_prompt`) — the row data (Question/Options/Explanation/Key) plus the **V1 findings already discovered**, explicitly telling the model not to duplicate what V1 already caught.

  

### 8.5 Output schema

  

The model must return strict JSON:

```json

{"issues": [

{"severity": "Critical|Major|Minor|Suggestion",

"error_type": "Formula Error|Missing Formula|Wrong Substitution|Ambiguity|Question-Explanation Mismatch|Invalid Assumption|Final Conclusion Mismatch|Clarity Issue|Needs Human Review|Key Mismatch",

"field": "Question|Explanation|Options|Key|Multiple",

"evidence": "...", "reason": "...", "suggested_fix": "...", "confidence": "High|Medium|Low"}

]}

```

For batch calls, the model returns a JSON **array** of `{"sno": ..., "issues": [...]}`, one per input question, same order.

  

### 8.6 Post-processing / guardrails (`llm_reasoning_checks.py`)

  

Raw model output is never trusted as-is — it goes through several validation/cleanup layers:

  

1.  **`_normalize_issue`** — coerces any out-of-vocabulary severity/error_type/field/confidence to safe defaults; **drops** an issue entirely if it has no evidence or reason text, or if it contains disallowed nitpicks ("extra space", "double space" etc.); downgrades to `Needs Human Review`/`Low confidence` if the model's own wording is hedgy ("may be", "might", "possibly", "seems", "could be", "unclear").

2.  **`_dedupe_against_v1`** — strips any V2 issue that's just an echo of a V1 finding (same error_type/field/evidence text already reported).

3.  **`_postprocess_v2_issues`** — extra precision gate:

- If V1 found *only* grammar issues, blocks V2 from escalating to Formula/Substitution/Conclusion-level errors (avoids the LLM "rationalizing" a grammar nitpick into a fake math error).

- For "complex reasoning" topics (ranking, clock, direction, blood relation, seating, interchange) — strips any `suggested_fix` unless confidence is `High`, and even then strips fixes for Formula/Substitution/Conclusion error types (too risky to auto-suggest a corrected answer for these topics).

- Caps output at **2 issues max** per row.

4. Result wrapped in `LLMReviewResult(issues, status, error, model)` — `status` is one of `Success`, `Failed`, `Invalid JSON`, `Not Run`.

  

### 8.7 Effect of LLM failure on the final verdict

  

In `_build_result()` ([app.py:50](app.py#L50)): if the LLM was requested but didn't succeed (`status not in {"Success", "Not Run"}`), an otherwise-clean row's status gets downgraded — `Approved` → `System Error / Needs Retry`, `Approved with Minor Fixes` → `Needs Review` — so a silent LLM failure never gets reported to the user as a clean pass.

### 8.8 V1 false-positive veto (Data Mismatch only)

V1's name-detection heuristic (§7 item 8) is necessarily conservative but can still mistake an unusual word for a person's name. Rather than adding a second API call, the *existing* V2 call doubles as a semantic sanity check for this one fragile category, at zero extra cost:

- `V1_DATA_MISMATCH_VETO_RULE` ([llm_prompts.py](reviewer/llm_prompts.py)) is folded into both the single-row and batch V2 prompts. It shows the model any V1 finding with `error_type == "Data Mismatch"` and asks it to identify — by exact evidence text — any that mistake a common word (adverb, ordinal, label) for a person's name, while explicitly telling it NOT to veto a genuine name swap or a real numeric rank/position contradiction.
- The model returns an additional top-level `v1_false_positives` array (alongside the normal `issues` array): `[{"evidence": "...", "reason": "..."}]`.
- `_extract_v1_false_positives()` ([llm_reasoning_checks.py](reviewer/llm_reasoning_checks.py)) validates each entry — only an **exact evidence-text match** against a V1 issue whose `error_type` is literally `"Data Mismatch"` is accepted; anything else is silently ignored. This is the scope guard that stops V2 from vetoing any other check category.
- `_build_result()` ([app.py](app.py)) drops the matched V1 issue(s) before combining with V2's own issues, but only when `llm_result.status == "Success"` — if the LLM call fails or `--use-llm` wasn't used, the V1 finding stands unchanged. The veto can only ever remove noise, never silently swallow a failure.

This is the same "let the model double-check a fragile heuristic before trusting it" pattern already used for arithmetic (§8.3's `verify_calculation` tool), applied to natural-language name detection instead of numeric evaluation.

  

---

  

## 9. Issue filtering & consolidation

  

### 9.1 `reviewer/issue_filters.py` — `filter_known_false_positives()`

  

The last precision gate before results are shown to the user. Applied to the **combined** V1+V2 issue list. Key suppressions:

  

- Drops `Calculation Error` when an `Alligation` formula issue or a `Wrong Substitution` issue is already the root cause (avoids reporting downstream arithmetic-of-wrong-numbers as a separate bug).

- Drops `Key Mismatch` when `Answer Not in Options` is already present (the corrected option would be misleading).

- Suppresses ranking/position "Formula Error" flags on interchange/swap/shift/between questions (these correctly use post-interchange positions; the naive `left + right - 1` formula doesn't apply).

- Suppresses clock gain/loss formula/unit flags unless evidence is about elapsed time/true-time confusion specifically.

- Suppresses blood-relation gender-ambiguity flags when (a) the question explicitly states the person's gender, (b) the answer options don't include the opposite-gender relation (so gender is uniquely pinned by elimination), or (c) the Key is itself a gender-neutral term (Sibling/Parent/Grandparent) intentionally covering both genders. Condition (a) depends on `_extract_claimed_person_name()` correctly identifying which person's gender is being discussed in an LLM-produced evidence string — it scans the whole string for the first non-common-word candidate rather than assuming the name is the literal first word, since LLM phrasing often leads with something else ("The explanation assumes Priya is male...").

- Strips risky `suggested_fix` text on Formula/Substitution/Conclusion/Mismatch issues for complex topics (ranking, clock, blood relation, direction, alligation, age, work-rate) — keeps the issue, removes the (possibly wrong) proposed correction.

-  **Adds** a missing `Key Mismatch` issue when a Critical Formula/Substitution/Conclusion/Mismatch issue's `suggested_fix`/`reason` text unambiguously names one option.

  

### 9.2 `reviewer/consolidator.py` — `consolidate()`

  

Takes the final issue list and:

1.  **Dedupes** (`dedupe_issues`) by (severity, error_type, field, evidence, reason).

2.  **Sorts** (`sort_issues`) by severity first, then a fixed root-cause-before-consequence order (e.g. `Question-Explanation Mismatch` before `Key Mismatch`).

3. Derives:

-  **`Agent Status`** — `Rejected` if any Critical issue exists; `Needs Review` if any Major; `Approved with Minor Fixes` if only Minor/Suggestion; else `Approved`.

-  **`Error Count`**, **`Error Types`** (semicolon-joined), **`Agent Remarks`** (numbered list of human-readable issue descriptions), **`Suggested Corrections`** (numbered list of non-empty fixes), **`Confidence`** (lowest confidence among all issues), **`Reviewed Key`** (auto-filled only from a High-confidence Key Mismatch fix).

  

These become the `REVIEW_COLUMNS` appended to the output spreadsheet: `Agent Status, Error Count, Error Types, Agent Remarks, Suggested Corrections, Confidence, Reviewed Key, LLM Status, LLM Error, LLM Model`.

  

---

  

## 10. Rubric categories (dashboard grouping)

  

`server.py`'s `RUBRIC_MAP` buckets the ~25 raw `error_type` values into 11 dashboard-facing categories for the UI's per-question rubric breakdown:

  

`Calculation`, `Key Mismatch`, `Key Missing`, `Data Mismatch`, `Entity / Value Swap`, `Conclusion Mismatch`, `LaTeX Formatting`, `Pronoun / Gender`, `Name Consistency`, `Question Error`, `Duplicate`.

  

---

  

## 11. Accuracy metrics (`reviewer/metrics.py`)

  

If the input file has a ground-truth column, `compute_confusion_metrics()` computes a real confusion matrix:

  

-  **Flag** = Agent Status is `Rejected` or `Needs Review`. **Approve** = `Approved` or `Approved with Minor Fixes`.

-  **TP** = real problem + flagged, **FN** = real problem + approved (missed), **FP** = clean + flagged (false alarm), **TN** = clean + approved (correct).

- Derives **Precision**, **Recall**, **F1**, **Accuracy** (as percentages), plus auto-generated plain-English insights (e.g. "Low precision (62%) — 8 clean questions were incorrectly flagged").

  

This same logic powers [evals/run_evals.py](evals/run_evals.py), the offline eval harness that runs V1 (+ optional V2) against two hand-labeled ground-truth files (`evals_1.xlsx`, `evals_2.xlsx`, kept locally, not committed) and prints a per-file and combined precision/recall/F1 report, plus full false-positive/false-negative listings. Latest recorded scores (re-verified against the current V1 checks): **evals_1 F1 = 0.85** (TP=17, TN=9, FP=1, FN=5), **evals_2 F1 = 0.59** (TP=10, TN=2, FP=1, FN=13), **combined F1 = 0.73** (precision 0.93, recall 0.60). Recall is the weaker half of the score — most of the gap is real issues V1 doesn't have a check for yet (see §7 for what's covered), not false alarms; precision is already high (0.93) and is where the accuracy-hardening work described throughout §7/§8 is targeted, since a wrongly-rejected valid question costs more reviewer time than a missed one that a human catches on read-through.

  

---

  

## 12. Google Sheets logging (`reviewer/sheets_logger.py`)

  

### 12.1 Setup / auth

  

- Activated only if `GOOGLE_SHEET_ID` env var is set (checked via `sheets_configured()`); otherwise every logging call is a silent no-op (logged as a warning, never crashes the app).

- Auth via a **Google service-account key** (`GOOGLE_CREDENTIALS_PATH`, default `credentials.json`), scopes: `spreadsheets` + `drive`.

- If the credentials file looks like an OAuth client-secret file instead of a service-account key, it's detected and a specific warning is logged instead of failing silently.

- Worksheets are auto-created with headers on first use (`_ensure_sheet`).

  

### 12.2 What gets logged, and where

  

**Sheet: "Runs"** — one row per completed review job (`log_run()`, called automatically at the end of every job, both CLI and web):

```

Timestamp | Job ID | Model | Total | TP | FP | FN | TN |

Precision | Recall | F1 | Accuracy | Has Ground Truth |

Approved | Needs Review | Rejected

```

`Model` is `"V1 only"` if the LLM wasn't used for that job, otherwise the resolved model string (e.g. `anthropic/claude-sonnet-4-6`).

  

**Sheet: "Errors"** — one row per distinct error type found in a job, with its count (`log_run()` also writes this, only if any errors were found):

```

Timestamp | Job ID | Error Type | Count

```

  

**Sheet: "Feedback"** — one row per human approve/reject action taken in the UI (`log_feedback()`, called from `POST /api/jobs/{id}/feedback`):

```

Timestamp | Job ID | Question No | Question | Agent Status | User Verdict | User Remarks

```

The `Question` column holds the **full question text** (not just the `Q010`-style label) so the feedback log is self-contained and usable for analyzing what the agent gets wrong without cross-referencing the original file.

  

### 12.3 Reading it back

  

`fetch_run_history()` and `fetch_error_history()` read the "Runs"/"Errors" sheets back as dicts — used by `GET /api/dashboard` to show historical runs (merged with the current in-memory session's jobs) and an aggregated "top error types across all runs" chart.

  

---

  

## 13. Web server / API (`server.py`)

  

FastAPI app, title "Aptitude Reviewer API", Swagger docs at `/api/docs`. In-memory job store (`jobs: Dict[str, Any]`, keyed by an 8-character uuid) — **not persisted**; restarting the server loses in-progress/completed job state (Google Sheets is the only durable record).

  

| Method & Path | Purpose |

|---|---|

| `POST /api/jobs` | Upload a file (`multipart/form-data`, field `file`), optional `use_llm` form field (default `"true"`). Validates extension (`.xlsx`/`.xls`) and size (≤10 MB). Returns `{job_id}` and starts a background thread. |

| `GET /api/jobs/{id}` | Poll progress: status, done/total counts, current row label, elapsed/estimated-remaining seconds. |

| `GET /api/jobs/{id}/results` | Full results once done: per-row data, approved/needs-review/rejected counts, top 6 common issue types, confusion metrics. |

| `GET /api/jobs/{id}/download` | Download the annotated `.xlsx`. |

| `POST /api/jobs/{id}/feedback` | Submit `{sno, verdict: "approve"|"reject", remarks}` for one row; stored in-memory and logged to the Feedback sheet. |

| `GET /api/template` | Generates and returns a blank `.xlsx` with just the required header row. |

| `GET /api/dashboard` | Combines in-session job summaries with historical Sheets data; returns aggregated top-error-types list. |

| `GET /` (and any static path) | Serves [frontend/index.html](frontend/index.html) and assets via `StaticFiles(html=True)`. |

  

Background job execution (`_run_job`) runs in a **daemon thread** per upload (`threading.Thread`) — there is no task queue/worker process; concurrency is whatever Python threads under the GIL allow (fine for this I/O-bound + occasional-LLM-call workload, but all state lives in one process's memory).

  

---

  

## 14. Frontend (`frontend/index.html`)

  

Single HTML file, 896 lines, four view states toggled by JS (`hidden` class), no client-side router/build step:

  

1.  **`view-upload`** — drag-and-drop or click-to-browse `.xlsx`/`.xls` upload, "Use LLM" toggle, download-template link.

2.  **`view-processing`** — polls job status, animated progress bar, done/elapsed/remaining stat tiles.

3.  **`view-results`** — stat tiles (Total/Approved/Needs Review/Rejected with percentages), common-issues tag cloud, per-question rubric breakdown, approve/reject feedback buttons, "Download Excel" button.

4.  **`view-dashboard`** — historical run list (session + Google Sheets combined) and aggregated top error types.

  

Styled with Tailwind (CDN), no other JS dependencies.

  

---

  

## 15. CLI tool (`app.py`)

  

```

python app.py input.xlsx output_reviewed.xlsx [--use-llm] [--llm-model MODEL]

[--llm-delay SECONDS] [--batch-size N] [--skip-llm-if-critical]

```

  

-  `--skip-llm-if-critical`: skips the (costly) V2 call entirely for rows where V1 already found a Critical issue — saves API spend on rows that are going to be rejected regardless.

- Same `_run_v1_checks` / `_build_result` functions are shared with `server.py` and `evals/run_evals.py`, so CLI, web, and eval runs are guaranteed to apply identical logic.

  

---

  

## 16. Environment variables

  

From [.env.example](.env.example):

  

```bash

OPENROUTER_API_KEY=your_openrouter_api_key_here  # required for any V2/LLM usage

OPENROUTER_MODEL=anthropic/claude-sonnet-4-6  # optional override, default in llm_client.py

GOOGLE_SHEET_ID=your_google_sheet_id_here  # optional, enables Sheets logging

GOOGLE_CREDENTIALS_PATH=credentials.json  # optional, service-account key path

OPENROUTER_ROW_DELAY_SECONDS=0.5  # optional, CLI-only batch delay (not in .env.example, read directly via os.getenv)

```

  

`credentials.json` and `.env` are both gitignored — never committed. On Render, `credentials.json` is supplied via Render's **Secret Files** feature mounted at `/etc/secrets/credentials.json`.

  

---

  

## 17. Deployment (Render)

  

Already pushed to GitHub at `https://github.com/chakripasupuleti/aptitude-reviewer`. To deploy:

  

1. Render → New → **Web Service** (not Static Site) → connect the GitHub repo.

2. Build command: `pip install -r requirements.txt`

3. Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`

4. Environment variables: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_PATH=/etc/secrets/credentials.json`

5. Secret Files: add `credentials.json` with the service-account key contents.

6. Deploy — Render builds and serves the FastAPI app (which also serves the frontend) at the assigned `*.onrender.com` URL.

  

---

  

## 18. Known architectural notes / limitations

  

-  **No database** — job state is in-process memory; Google Sheets is the only durable history, and only when configured.

-  **No persisted feedback loop into model behavior** — approve/reject feedback is logged for human analysis, not (yet) fed back automatically into prompt tuning or fine-tuning.

-  **Tool-calling is single-row only** — the batched V2 path (used by the web app, 40 rows/call) does not give the model the `verify_calculation` tool; only the CLI/eval single-row path (`run_llm_reasoning`) does.

-  **V2 caps at 2 issues per row** — by design, to keep output focused on the most material problem(s) rather than an exhaustive list.

-  **`anthropic` and `sympy` packages are installed but not actively used** in the current code paths (LLM calls go through OpenRouter via `requests`; arithmetic uses `fractions.Fraction` rather than `sympy`).
-  **V1 checks are heuristic/regex-based, not a parser** — each one is a targeted pattern match, not a general grammar of aptitude-question phrasing, so any given check can in principle be fooled by phrasing it wasn't written for. False positives found in practice are fixed with a repro-verified patch to the specific function (confirm the failing case, fix, then re-verify a genuine-error case is *still* caught) rather than a broad try/except or a suppression rule — suppression rules accumulate in [issue_filters.py](reviewer/issue_filters.py) only for well-understood, recurring patterns (§9.1), not as a catch-all.