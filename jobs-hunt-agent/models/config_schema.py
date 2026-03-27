from typing import Optional
from pydantic import BaseModel, Field


class UserConfig(BaseModel):
    name: str
    email: str
    phone: str
    base_resume_path: str = "data/base_resume.md"


class PlatformConfig(BaseModel):
    enabled: bool = True
    cookie_file: str


class PlatformsConfig(BaseModel):
    shixiseng: Optional[PlatformConfig] = None
    boss: Optional[PlatformConfig] = None


class SearchConfig(BaseModel):
    cities: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    job_types: list[str] = Field(default_factory=lambda: ["实习", "全职"])
    salary_min: Optional[int] = None
    skills_required: list[str] = Field(default_factory=list)
    skills_bonus: list[str] = Field(default_factory=list)
    filter_score_threshold: float = Field(default=65, ge=0, le=100)


class LimitsConfig(BaseModel):
    max_jobs_per_run: int = 50
    max_applications_per_day: int = 20
    request_delay_seconds: list[float] = Field(default_factory=lambda: [2.0, 5.0])


class LLMConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 2048
    temperature: float = 0.3


class AppConfig(BaseModel):
    user: UserConfig
    search: SearchConfig
    platforms: PlatformsConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
