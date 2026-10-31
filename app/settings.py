"""Shared settings: env vars, niche config, paths."""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
# API keys can live in another project's .env (path in KEYS_ENV_FILE) so they aren't copied around
if os.getenv("KEYS_ENV_FILE") and Path(os.environ["KEYS_ENV_FILE"]).exists():
    load_dotenv(os.environ["KEYS_ENV_FILE"], override=True)


def _p(env, default):
    v = os.getenv(env, default)
    p = Path(v)
    return p if p.is_absolute() else (ROOT / p).resolve()


DB_PATH = _p("DB_PATH", "./data/app.db")
NICHE_CONFIG = _p("NICHE_CONFIG", "./config/street_food.yaml")
SCOUT_PROFILE_DIR = _p("SCOUT_PROFILE_DIR", "./profiles/scout")
CHECKER_PROFILE_DIR = _p("CHECKER_PROFILE_DIR", "./profiles/checker")
TMP_DIR = ROOT / "data" / "tmp"
MODE = os.getenv("MODE", "hackathon")  # hackathon | production
SCOUT_ACCOUNT = os.getenv("SCOUT_ACCOUNT", "scout1")

# "chrome" = your installed Google Chrome (separate profile dir); "" = Playwright's bundled Chromium
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

QUICK_MODEL = "claude-haiku-5-5"
DEEP_MODEL = "claude-opus-5-5"


@lru_cache
def niche():
    with open(NICHE_CONFIG) as f:
        return yaml.safe_load(f)


def branches():
    return list(niche()["branches"])


def prompt(name):
    return (ROOT / "app" / "prompts" / f"{name}.txt").read_text()
