from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class JobPosting(BaseModel):
    id: str = Field(..., description="唯一标识，格式：{platform}_{platform_job_id}")
    title: str = Field(..., description="职位名称")
    company: str = Field(..., description="公司名称")
    location: str = Field(..., description="工作地点")
    salary_range: Optional[str] = Field(None, description="薪资范围，如 '15k-25k'")
    jd_text: str = Field(..., description="完整的职位描述文本")
    platform: str = Field(..., description="来源平台：shixiseng | boss | linkedin")
    url: str = Field(..., description="职位详情页 URL")
    crawled_at: datetime = Field(default_factory=datetime.now, description="抓取时间")

    # 筛选后填入
    score: Optional[float] = Field(None, ge=0, le=100, description="LLM 评分（0-100）")
    score_reason: Optional[str] = Field(None, description="LLM 评分理由")
    match_points: Optional[list[str]] = Field(None, description="匹配点列表")
    concern_points: Optional[list[str]] = Field(None, description="关注点列表")

    # 状态流转：new → filtered → tailored → applied → rejected
    status: str = Field(default="new", description="当前状态")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
