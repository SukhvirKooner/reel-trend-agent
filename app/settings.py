"""Shared settings: env vars, multi-niche registry, paths."""
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


def _resolve(rel):
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _p(env, default):
    v = os.getenv(env, default)
    return _resolve(v)


NICHES_FILE = _p("NICHES_FILE", "./config/niches.yaml")
MODE = os.getenv("MODE", "hackathon")  # hackathon | production
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

QUICK_MODEL = "claude-haiku-5-5"
DEEP_MODEL = "claude-opus-5-5"

# Populated by apply_niche(); street-food defaults until then.
DB_PATH = _resolve("./data/app.db")
NICHE_CONFIG = _resolve("./config/street_food.yaml")
SCOUT_PROFILE_DIR = _resolve("./profiles/scout")
CHECKER_PROFILE_DIR = _resolve("./profiles/checker")
TMP_DIR = ROOT / "data" / "tmp"
SCOUT_ACCOUNT = "scout1"
ACTIVE_NICHE = "street_food"


@lru_cache
def niche_registry():
    with open(NICHES_FILE) as f:
        return yaml.safe_load(f)


def list_niches():
    """name → resolved spec (label, config, db, scout_profile, checker_profile, account)."""
    reg = niche_registry()
    out = {}
    for name, raw in (reg.get("niches") or {}).items():
        out[name] = {
            "label": raw.get("label") or name.replace("_", " "),
            "config": _resolve(raw["config"]),
            "db": _resolve(raw["db"]),
            "scout_profile": _resolve(raw["scout_profile"]),
            "checker_profile": _resolve(raw.get("checker_profile") or raw["scout_profile"]),
            "account": raw.get("account") or name,
        }
    return out


def default_niche_name():
    return os.getenv("NICHE") or niche_registry().get("default") or "street_food"


def apply_niche(name=None):
    """Select the active niche for this process (DB + config + Instagram profile).

    Niche paths come from config/niches.yaml. A one-off DB_PATH=… env var still
    overrides only the database (demo / frozen snapshot runs), not the niche config.
    """
    global DB_PATH, NICHE_CONFIG, SCOUT_PROFILE_DIR, CHECKER_PROFILE_DIR, SCOUT_ACCOUNT, ACTIVE_NICHE, TMP_DIR
    name = name or default_niche_name()
    niches = list_niches()
    if name not in niches:
        raise SystemExit(f"Unknown niche {name!r}. Choose one of: {', '.join(niches)}")
    spec = niches[name]
    NICHE_CONFIG = spec["config"]
    SCOUT_PROFILE_DIR = spec["scout_profile"]
    CHECKER_PROFILE_DIR = spec["checker_profile"]
    SCOUT_ACCOUNT = os.getenv("SCOUT_ACCOUNT") or spec["account"]
    # DB_PATH env wins for one-off tools (demo.db, backups); otherwise niche db.
    DB_PATH = _resolve(os.environ["DB_PATH"]) if os.getenv("DB_PATH") else spec["db"]
    ACTIVE_NICHE = name
    TMP_DIR = ROOT / "data" / "tmp" / name
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    niche.cache_clear()
    return name


@lru_cache
def niche():
    with open(NICHE_CONFIG) as f:
        return yaml.safe_load(f)


def load_niche_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def niche_for_db(path):
    """Return (niche_name, config_dict) for a database path; fall back to the active niche."""
    path = Path(path).resolve()
    for name, spec in list_niches().items():
        if Path(spec["db"]).resolve() == path:
            return name, load_niche_config(spec["config"])
    return ACTIVE_NICHE, niche()


def branches():
    return list(niche()["branches"])


def niche_label(cfg=None):
    n = cfg or niche()
    return n.get("label") or str(n.get("niche", "niche")).replace("_", " ")


def niche_short(cfg=None):
    n = cfg or niche()
    return n.get("short_label") or niche_label(n).split()[0]


def score_name(cfg=None):
    n = cfg or niche()
    return n.get("score_name") or f"{niche_short(n)} score"


def prompt(name):
    """Load a prompt and fill niche-specific placeholders."""
    raw = (ROOT / "app" / "prompts" / f"{name}.txt").read_text()
    n = niche()
    return (raw
            .replace("{{NICHE_LABEL}}", niche_label(n))
            .replace("{{NICHE_DESCRIPTION}}", (n.get("description") or "").strip())
            .replace("{{REGION}}", str(n.get("region") or ""))
            .replace("{{ENTITY_FACET}}", str(n.get("entity_facet") or "dish"))
            .replace("{{CREATOR_AUDIENCE}}", str(n.get("creator_audience") or "creators")))


def add_niche_arg(parser):
    niches = list(list_niches())
    parser.add_argument("--niche", default=None, choices=niches,
                        help=f"which category to run (default: {default_niche_name()})")


try:
    apply_niche(default_niche_name())
except Exception:
    pass
