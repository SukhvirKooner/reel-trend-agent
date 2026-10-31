"""Thin wrapper around the Anthropic SDK for structured (Pydantic) outputs."""
import base64
import logging

import anthropic

from app import settings

log = logging.getLogger("llm")
_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def img_block(path, media_type="image/jpeg"):
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def parse(model, system, content, schema, max_tokens=4000, effort="low"):
    """Run a structured-output call. Returns the parsed Pydantic object, or None on refusal.

    Opus calls opt into server-side refusal fallbacks ("default" routing), so a safety
    false-positive on, say, a knife-heavy kebab reel is retried on a fallback model instead of
    dropping the reel.
    """
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        output_config={"effort": effort},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_format=schema,
    )
    if model == settings.DEEP_MODEL:
        try:
            resp = client().beta.messages.parse(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except (TypeError, anthropic.BadRequestError) as e:
            log.warning("fallbacks unavailable (%s); calling without", e)
            resp = client().messages.parse(**kwargs)
    else:
        resp = client().messages.parse(**kwargs)
    if resp.stop_reason == "refusal":
        log.warning("refusal from %s: %s", model, getattr(resp, "stop_details", None))
        return None
    if resp.stop_reason == "max_tokens":
        log.warning("hit max_tokens on %s", model)
        return None
    return resp.parsed_output
