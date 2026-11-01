# Reel Trend Agent

Multi-niche Instagram Reels trend finder. **Street food (Delhi)** and **Concert reels (Diljit-focused)** each have their own Instagram account, browser profile, SQLite database, and category config under `config/niches.yaml`. A scout watches Reels and saves links and metadata. A checker re-opens the links to record view counts over time. A trend engine finds rising reels and the sub-genres, sounds and locations behind them. An explainer writes a "why it works" card with a playbook.

```
app/scout.py     Process 1  Playwright scout: glance → quick tag → watch/skip → save link + frames
app/analyzer.py  Process 2  Haiku quick tag (all reels) + Opus deep analysis (in-niche) + tag tidying
app/checker.py   Process 3  re-opens links on an adaptive schedule, records views and follower counts (Apify backup)
app/trends.py    Process 4  reach / speed / acceleration → rising → sub-genres → scores + labels
app/explain.py   Process 5  winners-vs-rest lift table → Opus trend card (why + playbook)
ui.py                       Streamlit: niche tree, trend card, agent live view, raw data
```

## Phase 0: once, everyone together
1. `source .venv/bin/activate`. Dependencies are already installed. For transcripts, also run `pip install faster-whisper` (ffmpeg is already on this Mac).
2. `ANTHROPIC_API_KEY` is read from your shell environment. Copy `.env.example` to `.env` if you need to override any paths.
3. **Accounts.** Set up 2 burner Instagram accounts on real phones and warm each one up for 15–20 minutes (plan section 2.1).
4. **DevTools field check (plan section 2.4).** Confirm the field names Instagram uses right now, then edit `FIELD_VIEWS`, `FIELD_FOLLOWERS` and `parse_reel()` in `app/ig_parse.py`. To check quickly: log in, run `python -m app.scout --no-tag --minutes 2`, and look at `reels` and `snapshots` in `data/app.db`.
5. Connect the scout laptop to the **Indian mobile hotspot**. Never run the scout from a datacenter IP.

## Niches (street food stays; concert is additive)
| Niche | Flag | Instagram profile | Database | Config |
|-------|------|-------------------|----------|--------|
| Street food (default) | `--niche street_food` | `profiles/scout` | `data/app.db` | `config/street_food.yaml` |
| Concert (Diljit focus) | `--niche concert` | `profiles/concert` | `data/concert.db` | `config/concert.yaml` |

First time on concert: log into the **new** Instagram account in the Chrome window that opens for `profiles/concert`. Street-food login and data are untouched.

## Run order. Start M1 as early as possible, because views over time need *time*.
```bash
# Street food (default)
python -m app.scout --no-like --start-search
python -m app.checker
python -m app.analyzer
python -m app.trends

# Concert (separate account + DB)
python -m app.scout --niche concert --no-like --start-search
python -m app.checker --niche concert
python -m app.analyzer --niche concert
python -m app.trends --niche concert
```
- `python -m app.scout --no-tag` saves reels without calling Claude. The analyzer tags that backlog later.
- If the scout stops with a challenge or "action blocked" page, do **not** solve it automatically.
- At the end of the hackathon, run `python -m app.checker --once --final` to re-check reels older than 14 days one last time.

## Dashboard (show this to mentors)
```bash
python -m app.dashboard          # → http://localhost:8080   (Street food · Concert · Demo · frozen backups in the sidebar)
python -m app.report --niche concert
```
Left sidebar with five pages: **Overview** (plain-English headline: trend found or progress towards the first one,
agent pulse of its last decisions, KPIs, pipeline funnel, latest decisions, fastest-growing reels, how it works) ·
**Trends** (one card per trend: key numbers, why it works with confidence + evidence bars, a black "film this
tomorrow" playbook with a Copy button, example reels) · **Agent activity** (decision feed with Watched/Skipped
filter, branch coverage, decision mix, exploration events) · **Reel analysis** (searchable cards: hook with a
position marker on the reel's timeline, story, measured edit + sound, drivers, view growth) · **View tracking**
(search, status filters, sortable columns).

Presenting: press **1–5** to switch pages, **?** for a plain-English "How to read this" guide, use the sidebar for
light/dark and **Pause refresh**. The page refreshes in place every 30 s and keeps your page, filters, search text and
open/closed cards. Times show as "x min ago" in your own time zone. Styles and behaviour live in `app/ui/dashboard.css`
and `app/ui/dashboard.js`; the page works offline (no CDN, no fonts to download).

## Demo safety net
- To freeze the DB, pick a `backups/app-HHMM.db` file in the UI sidebar, or run `DB_PATH=./backups/app-HHMM.db streamlit run ui.py`.
- Trend cards are stored in `trend_runs.card_json`, so the demo never waits on an API call.
- Screen-record 2 minutes of the scout running.

## Test the pipeline without Instagram (synthetic data)
```bash
python scripts/seed_demo.py                                  # writes data/demo.db; codes all start with DEMO
DB_PATH=./data/demo.db python -m app.trends --once           # add --no-llm to skip Claude calls
DB_PATH=./data/demo.db streamlit run ui.py
```

## Small deviations from the plan
- The branch `hygiene_and_exposé` is spelled `hygiene_and_expose` (ASCII only).
- Tags are stored as `facet:tag`. Sub-genres must combine tags from **different** facets, so they describe a style rather than a pair of synonyms.
- Hackathon thresholds are softened for mentor demos: rising = top 50% speed (not 25%), reach ≥ 1× followers, trend needs ≥ 2 rising creators, sub-genres need ≥ 3 reels / 2 creators and lift ≥ 1.2.
- If a creator's follower count is still unknown, `reach` can't be computed. In that case a reel counts as rising only if its speed is in the top 25%.
- Groups that are eligible but are not top-5, emerging or saturated get the label `active`.
- Opus calls use server-side refusal fallbacks (`fallbacks: "default"`). A false-positive refusal on a reel is then retried on a fallback model instead of the reel being skipped.

## Tests
```bash
python -m pytest -q                         # unit tests: Claude, the browser and the clock are faked; no network needed
RUN_LIVE=1 python -m pytest tests/test_live.py   # optional real-API smoke test (< $0.05): checks every schema and the Opus fallback config
```
