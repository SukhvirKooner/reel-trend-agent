# Reel Trend Agent

**Team:** EVOKE  
**Repository:** [SukhvirKooner/reel-trend-agent](https://github.com/SukhvirKooner/reel-trend-agent) (public)

## Problem statement

Short-form video trends on Instagram Reels move fast. Creators and marketers usually spot them late — by scrolling, copying what already went viral, or relying on gut feel. By then the format is saturated. There is no practical way to systematically watch a niche, measure which reels are *taking off* (not just popular accounts posting), and turn that into a clear “film this next” brief with evidence.

## Solution / product

**Reel Trend Agent** is a multi-niche Instagram Reels trend finder. For each niche it:

1. **Scouts** Reels in a real browser (Playwright), quick-tags them with AI, and steers the feed toward in-niche content  
2. **Checks** view and follower counts over time  
3. **Detects rising reels** using reach, speed and momentum (not raw view count alone)  
4. **Finds trends** shared by multiple rising creators (styles, sounds, locations)  
5. **Explains** why a trend works and produces a short playbook creators can shoot tomorrow  

Niches today: **Indian street food**, **Concert reels** (Diljit-focused), and **Fitness** — each with its own Instagram account, Chrome profile, SQLite database, and config under `config/niches.yaml`.

A live dashboard shows overview, trends, agent decisions, reel analysis, and view tracking.

```
app/scout.py     Scout: glance → quick tag → watch/skip → save link + frames
app/analyzer.py  Haiku quick tag + Opus deep analysis + tag tidying
app/checker.py   Re-check views / followers on an adaptive schedule
app/trends.py    Reach / speed / acceleration → rising → sub-genres → scores
app/explain.py   Winners-vs-rest lift → trend card (why + playbook)
app/dashboard.py Live HTML dashboard
```

## Presentation

PowerPoint / Google Slides: *[add link here]*

## Team — EVOKE

| Member | |
|--------|--|
| Sukhvir Singh | |
| Mayank | |
| Shivam Kaushik | |
| Aruj Grag | |

## Niches

| Niche | Flag | Instagram profile | Database | Config |
|-------|------|-------------------|----------|--------|
| Street food (default) | `--niche street_food` | `profiles/scout` | `data/app.db` | `config/street_food.yaml` |
| Concert (Diljit focus) | `--niche concert` | `profiles/concert` | `data/concert.db` | `config/concert.yaml` |
| Fitness | `--niche fitness` | `profiles/fitness` | `data/fitness.db` | `config/fitness.yaml` |

First time on a niche: log into that niche’s Instagram account in its Chrome profile. Other niches’ logins and data stay untouched.

## Setup

1. `source .venv/bin/activate` (or create a venv and `pip install -r requirements.txt`). For transcripts, also `pip install faster-whisper`.  
2. Set `ANTHROPIC_API_KEY` (see `.env.example`).  
3. Warm Instagram accounts on real phones before long scout runs.  
4. Prefer an Indian mobile hotspot for the scout — avoid datacenter IPs.

## Run

```bash
# Street food (default)
python -m app.scout --no-like --start-search
python -m app.checker
python -m app.analyzer
python -m app.trends

# Concert / Fitness (separate account + DB each)
python -m app.scout --niche concert --no-like --start-search
python -m app.scout --niche fitness --no-like --start-search
python -m app.checker --niche fitness
python -m app.analyzer --niche fitness
python -m app.trends --niche fitness

# Dashboard
python -m app.dashboard          # → http://localhost:8080
```

- `python -m app.scout --no-tag` saves reels without calling Claude; the analyzer tags the backlog later.  
- If the scout hits a challenge or “action blocked” page, do **not** solve it automatically.  
- Sync local DBs to a deployed host with `scripts/sync-to-digitalocean.sh` when using the cloud dashboard + analyzer setup.

### Dashboard pages

**Overview** · **Trends** (evidence + playbook) · **Agent activity** · **Reel analysis** · **View tracking**.  
Shortcuts: **1–5** switch pages, **?** opens the guide, sidebar for light/dark and pause refresh. Live refresh every 30s.

## Notes

- Tags are stored as `facet:tag`. Sub-genres combine tags from **different** facets (a style, not synonyms).  
- Rising uses speed percentile, reach vs followers, and creator breadth across niches.  
- If follower count is unknown, reach cannot be computed; rising then requires a higher speed percentile.  
- Opus calls use server-side refusal fallbacks so a false-positive refusal is retried instead of skipping the reel.

## Tests

```bash
python -m pytest -q
RUN_LIVE=1 python -m pytest tests/test_live.py   # optional real-API smoke test
```
