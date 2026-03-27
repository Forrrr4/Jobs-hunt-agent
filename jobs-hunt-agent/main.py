"""
Job Hunt Agent — 主入口

用法示例：
    python main.py                          # dry-run（默认，全流程但不真实投递）
    python main.py --mode semi-auto         # 半自动：每次投递前人工确认
    python main.py --mode full              # 全自动投递（谨慎使用）
    python main.py --mode crawl-only        # 只抓取职位
    python main.py --mode filter-only       # 抓取 + LLM 评分
    python main.py --mode tailor-only       # 抓取 + 评分 + 定制简历
    python main.py --resume                 # 从上次中断的节点恢复
    python main.py --thread-id 20260327     # 指定 checkpoint ID

环境变量：
    ANTHROPIC_API_KEY   Claude API 密钥（filter/tailor/apply 阶段必须）
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich import box

from agents.orchestrator import run_agent, resume_agent
from models.agent_state import AgentState

console = Console()

VALID_MODES = ("full", "dry-run", "semi-auto", "crawl-only", "filter-only", "tailor-only")


# ---------------------------------------------------------------------------
# CLI 解析
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-hunt",
        description="求职自动化 AI Agent：抓取岗位 → 智能筛选 → 定制简历 → 自动投递",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式说明：
  dry-run     全流程演练，apply 阶段只写 pending 记录，不真实投递（默认）
  semi-auto   全流程，apply 前在终端请求人工确认每个职位
  full        全流程，apply 全自动（需确认平台 cookie 有效）
  crawl-only  仅抓取职位并入库
  filter-only 抓取 + LLM 评分（需 ANTHROPIC_API_KEY）
  tailor-only 抓取 + 评分 + 生成定制简历 PDF（需 ANTHROPIC_API_KEY）

示例：
  python main.py                            # 演练全流程
  python main.py --mode semi-auto           # 半自动投递
  python main.py --mode crawl-only          # 只抓取，不评分
  python main.py --resume                   # 从昨天中断处继续
  python main.py --thread-id 20260326 --resume  # 恢复指定日期的 session
        """,
    )
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default="dry-run",
        metavar="MODE",
        help=f"运行模式：{' | '.join(VALID_MODES)}（默认：dry-run）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="配置文件路径（默认：config.yaml）",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        metavar="ID",
        help="Checkpoint 线程 ID（默认：当天日期 YYYYMMDD）",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从上次 checkpoint 恢复（必须有同 thread-id 的历史记录）",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        metavar="LEVEL",
        help="日志级别（默认：INFO）",
    )
    return parser


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _load_config(path: str) -> dict:
    """加载并基本校验 config.yaml。"""
    cfg_path = Path(path)
    if not cfg_path.exists():
        console.print(
            f"[bold red]错误：找不到配置文件 {cfg_path.resolve()}[/bold red]\n"
            "请复制 config.yaml.example 为 config.yaml 并填写个人信息。",
        )
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        console.print("[bold red]错误：config.yaml 格式不正确，应为 YAML 映射。[/bold red]")
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------


def _check_env(mode: str) -> None:
    """检查运行模式所需的环境变量和文件。"""
    needs_llm = mode not in ("crawl-only",)
    if needs_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[bold yellow]警告：未检测到 ANTHROPIC_API_KEY 环境变量。[/bold yellow]\n"
            "  filter / tailor / apply 阶段需要 Claude API，请先设置：\n"
            "  [dim]Windows CMD :[/dim]  set ANTHROPIC_API_KEY=sk-ant-...\n"
            "  [dim]PowerShell  :[/dim]  $env:ANTHROPIC_API_KEY = 'sk-ant-...'\n"
            "  [dim]Linux/macOS :[/dim]  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "\n  [dim]如果只想抓取职位（无需 API key），请使用 --mode crawl-only[/dim]",
        )
        if mode in ("filter-only", "tailor-only", "dry-run", "semi-auto", "full"):
            # 不直接退出，让用户决定是否继续
            try:
                ans = input("\n仍然继续？[y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans != "y":
                sys.exit(0)


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------


def _print_banner(mode: str, thread_id: str, resume: bool) -> None:
    """打印启动横幅。"""
    mode_colors = {
        "full": "bold red",
        "semi-auto": "bold yellow",
        "dry-run": "bold green",
        "crawl-only": "bold cyan",
        "filter-only": "bold cyan",
        "tailor-only": "bold cyan",
    }
    color = mode_colors.get(mode, "bold white")

    content = (
        f"  模式：[{color}]{mode}[/{color}]\n"
        f"  Session ID：{thread_id}\n"
        f"  时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  恢复模式：{'是' if resume else '否'}"
    )
    console.print(Panel(content, title="[bold]Job Hunt Agent[/bold]", border_style="blue"))


def _print_summary(state: AgentState) -> None:
    """运行结束后打印结果摘要表格。"""
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("指标", style="dim", width=20)
    table.add_column("数量", justify="right")

    jobs_found = len(state.get("jobs_found", []))
    jobs_apply = len(state.get("jobs_to_apply", []))
    resumes = len(state.get("resumes_generated", []))
    applications = state.get("applications_sent", [])
    sent = sum(1 for a in applications if a.get("status") == "sent")
    pending = sum(1 for a in applications if a.get("status") == "pending")
    errors = len(state.get("errors", []))

    table.add_row("抓取职位", str(jobs_found))
    table.add_row("达标职位（投递候选）", str(jobs_apply))
    table.add_row("定制简历", str(resumes))
    table.add_row("已发送投递", f"[green]{sent}[/green]" if sent else "0")
    table.add_row("待投递（dry-run）", str(pending))
    table.add_row("错误数", f"[red]{errors}[/red]" if errors else "0")
    table.add_row("最终阶段", state.get("current_phase", "unknown"))

    console.print("\n")
    console.print(Panel(table, title="[bold]运行结果[/bold]", border_style="green"))

    # 打印错误详情
    if errors:
        console.print("\n[bold red]错误列表：[/bold red]")
        for e in state.get("errors", []):
            job_id = e.get("job_id") or "—"
            console.print(f"  [{e.get('module','?')}] job={job_id}: {e.get('error','')}")

    # 打印生成的简历路径
    if resumes:
        console.print("\n[bold]生成的简历文件：[/bold]")
        for r in state.get("resumes_generated", []):
            pdf = r.get("pdf_path") or r.get("md_path") or "N/A"
            ok = "✓" if r.get("integrity_ok") else "⚠"
            console.print(f"  {ok}  {r.get('job_company','?')} — {r.get('job_title','?')}")
            console.print(f"      {pdf}", style="dim")


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # 日志配置（Rich 格式）
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(message)s",
        handlers=[
            RichHandler(
                console=console,
                show_time=True,
                show_path=False,
                rich_tracebacks=True,
            )
        ],
    )
    # 降低第三方库的日志噪音
    for noisy in ("httpx", "httpcore", "anthropic", "playwright", "fonttools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 加载配置
    config = _load_config(args.config)

    # 环境检查
    _check_env(args.mode)

    # 确定 thread_id
    thread_id = args.thread_id or datetime.now().strftime("%Y%m%d")

    # 打印启动横幅
    _print_banner(args.mode, thread_id, args.resume)

    # 运行 Agent
    try:
        if args.resume:
            state = asyncio.run(resume_agent(thread_id=thread_id, config=config))
        else:
            state = asyncio.run(
                run_agent(
                    config=config,
                    run_mode=args.mode,
                    thread_id=thread_id,
                    resume=False,
                )
            )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]已中断。当前进度已保存到 checkpoint，下次运行时加 --resume 可继续。[/bold yellow]")
        sys.exit(0)
    except RuntimeError as e:
        # resume_agent 找不到 checkpoint 时抛出 RuntimeError
        console.print(f"\n[bold red]错误：{e}[/bold red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]未预期错误：{e}[/bold red]")
        logging.getLogger(__name__).exception("未预期错误")
        sys.exit(1)

    # 打印结果摘要
    _print_summary(state)

    # 根据结果设置退出码
    has_errors = bool(state.get("errors"))
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
