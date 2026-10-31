# Reel Trend Agent: Indian street food, Delhi

This is the implementation of `~/reel-trend-agent-hackathon-plan.md`. A scout bot watches Reels and saves links and metadata. A checker re-opens the links to record view counts over time. A trend engine finds rising reels and the sub-genres, sounds and locations behind them. An explainer writes a "why it works" card with a playbook. A Streamlit UI shows the results.

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

## Run order. Start M1 as early as possible, because views over time need *time*.
```bash
python -m app.scout                 # M1  log in by hand in the window the first time
python -m app.checker               # M2  separate window and account; log in by hand the first time
python -m app.analyzer              # M3  quick-tags any backlog, deep-analyzes in-niche reels, tidies tags every 30 min
python -m app.trends                # M4+M5  runs every 15 min and generates trend cards after each run
streamlit run ui.py                 # M6
crontab -e  →  */30 * * * * ~/reel-trend-agent/scripts/snapshot_db.sh
```
- `python -m app.scout --no-tag` saves reels without calling Claude. The analyzer tags that backlog later. Use this before the quick-tag call works.
- If the scout stops with a challenge or "action blocked" page, do **not** solve it automatically. Restart it on the spare account: `python -m app.scout --profile ./profiles/checker --account scout2`.
- If the checker gets blocked, run `APIFY_TOKEN=... python -m app.checker --apify`. Confirm the actor name and its output fields first.
- At the end of the hackathon, run `python -m app.checker --once --final` to re-check reels older than 14 days one last time.

## Dashboard (show this to mentors)
```bash
python -m app.dashboard          # → http://localhost:8080   (Live data · Demo data · frozen backups switch in the header)
python -m app.report             # → reports/report-*.html   (same page as one offline file to send around)
```
Tabs: **Overview** (KPIs, pipeline funnel, latest decision, how it works) · **Trends** (trend cards: why with
evidence chart + playbook + example reels; or progress towards the first trend) · **Agent activity** (every decision
with reason, branch coverage, exploration events) · **Reel analysis** (hook, story, measured edit + sound, drivers,
view growth) · **View tracking** (sortable). Refreshes in place every 30 s; dark mode follows the system.

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
- If a creator's follower count is still unknown, `reach` can't be computed. In that case a reel counts as rising only if its speed is in the top 10% (instead of the usual top 25%).
- Groups that are eligible but are not top-5, emerging or saturated get the label `active`.
- Opus calls use server-side refusal fallbacks (`fallbacks: "default"`). A false-positive refusal on a reel is then retried on a fallback model instead of the reel being skipped.

## Tests
```bash
python -m pytest -q                         # unit tests: Claude, the browser and the clock are faked; no network needed
RUN_LIVE=1 python -m pytest tests/test_live.py   # optional real-API smoke test (< $0.05): checks every schema and the Opus fallback config
```
