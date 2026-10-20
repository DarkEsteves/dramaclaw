"""Bounded, media-free graphic effects shared by plan revisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphicEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    template: Literal["question_card", "keyword", "title_bar"]
    text: str = Field(min_length=1, max_length=160)
    emphasis: str | None = Field(default=None, max_length=80)
    foreground: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    background: str = Field(default="#111827", pattern=r"^#[0-9a-fA-F]{6}$")
    accent: str = Field(default="#00BDCF", pattern=r"^#[0-9a-fA-F]{6}$")
    enter: Literal["none", "fade", "pop"] = "fade"
    enter_frames: int = Field(default=6, ge=0, le=120)
    exit_frames: int = Field(default=6, ge=0, le=120)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    font_size: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_emphasis(self) -> "GraphicEffect":
        if self.emphasis and self.emphasis not in self.text:
            raise ValueError("graphic emphasis must occur in its text")
        if self.enter == "none" and self.enter_frames:
            self.enter_frames = 0
        return self
