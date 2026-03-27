from typing import TypedDict, Optional
from models.job_posting import JobPosting


class AgentState(TypedDict):
    # 运行配置
    config: dict                        # 从 config.yaml 读取的完整配置
    run_mode: str                       # full / crawl-only / dry-run

    # 各阶段数据
    jobs_found: list[JobPosting]        # 爬取到的原始职位列表
    jobs_filtered: list[JobPosting]     # 筛选后（有 score）的职位列表
    jobs_to_apply: list[JobPosting]     # 达到分数线的职位（待投递）

    # 执行结果
    resumes_generated: list[dict]       # [{job_id: str, resume_path: str}]
    applications_sent: list[dict]       # [{job_id: str, status: str, timestamp: str}]

    # 错误追踪
    errors: list[dict]                  # [{module: str, error: str, job_id: Optional[str]}]

    # 流程控制
    current_phase: str                  # crawl / filter / tailor / apply / done
    should_stop: bool                   # 遇到严重错误时置为 True


def make_initial_state(config: dict, run_mode: str = "dry-run") -> AgentState:
    """创建初始 AgentState，所有列表字段初始化为空。"""
    return AgentState(
        config=config,
        run_mode=run_mode,
        jobs_found=[],
        jobs_filtered=[],
        jobs_to_apply=[],
        resumes_generated=[],
        applications_sent=[],
        errors=[],
        current_phase="crawl",
        should_stop=False,
    )
