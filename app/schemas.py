"""Pydantic output schemas for every Claude call."""
from typing import Literal, Optional

from pydantic import BaseModel, Field

Facet = Literal["dish", "format", "hook", "setting", "audio", "emotion"]


class FacetTag(BaseModel):
    facet: Facet
    tag: str = Field(description="reuse an existing tag if one fits; otherwise a new snake_case tag")
    is_new: bool


class QuickTag(BaseModel):
    niche_score: float = Field(description="0-1: how clearly this is Indian street food content")
    food_related: float = Field(description="0-1: any food content at all")
    branch: Optional[str] = Field(description="one of the given branches, or null")
    tags: list[FacetTag]
    language: str


class CategoryScore(BaseModel):
    category: Literal["food", "humour", "emotional", "informative", "travel", "news",
                      "music_dance", "lifestyle"]
    score: float


class Driver(BaseModel):
    driver: Literal["hook", "storyline", "edit_cuts", "audio", "location", "format", "caption",
                    "person_on_camera", "timing_moment"]
    detail: str
    strength: float


class DeepAnalysis(BaseModel):
    hook_type: str
    hook_text: str
    hook_seconds: float
    storyline: str
    format: str
    edit_pace: Literal["slow", "medium", "fast"]
    on_screen_text: list[str]
    audio_role: Literal["trending_song", "original_sound", "voiceover", "asmr", "mixed", "none"]
    location: Optional[str]
    person_on_camera: Literal["creator", "vendor", "both", "none"]
    category_scores: list[CategoryScore]
    drivers: list[Driver]
    one_line_summary: str


class TagMapping(BaseModel):
    candidate: str
    maps_to: str = Field(description="an existing tag from the same facet, or KEEP_NEW")


class TagMappings(BaseModel):
    mappings: list[TagMapping]


class SubgenreName(BaseModel):
    name: str = Field(description="short readable sub-genre name, max 7 words")


class WhyItem(BaseModel):
    driver: str
    explanation: str
    evidence: str
    confidence: Literal["high", "medium", "low"]


class Playbook(BaseModel):
    hook_options: list[str]
    audio: list[str]
    edit_style: str
    ideal_length_s: str
    location_ideas: list[str]
    shot_list: list[str]
    caption_template: str
    avoid: list[str]


class TrendCard(BaseModel):
    title: str
    one_liner: str
    why: list[WhyItem]
    playbook: Playbook
    example_codes: list[str]
