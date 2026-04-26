from __future__ import annotations

import pytest

from app.cli import build_parser, build_scheduler_job_definitions, run_named_stage


def test_build_scheduler_job_definitions_for_stages() -> None:
    jobs = build_scheduler_job_definitions("stages")
    assert [job["name"] for job in jobs] == ["ingestion", "normalization", "feature_generation", "prediction_run", "alert_check", "evaluation"]
    assert all("seconds" in job for job in jobs)


def test_build_scheduler_job_definitions_for_research_cycle() -> None:
    jobs = build_scheduler_job_definitions("research-cycle")
    assert jobs == [{"name": "research_cycle", "seconds": 3600}]


def test_parser_accepts_run_stage() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "research_cycle"])
    assert args.command == "run"
    assert args.stage == "research_cycle"


def test_parser_accepts_schedule_mode() -> None:
    parser = build_parser()
    args = parser.parse_args(["schedule", "--mode", "research-cycle"])
    assert args.command == "schedule"
    assert args.mode == "research-cycle"


@pytest.mark.asyncio
async def test_run_named_stage_uses_registry(monkeypatch) -> None:
    called: list[str] = []

    async def fake_stage() -> None:
        called.append("research_cycle")

    monkeypatch.setattr("app.cli.build_stage_registry", lambda: {"research_cycle": fake_stage})

    await run_named_stage("research_cycle")

    assert called == ["research_cycle"]
