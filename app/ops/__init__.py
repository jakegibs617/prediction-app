"""Operations and orchestration helpers."""

__all__ = ["ResearchCycleResult", "ResearchOrchestrator"]


def __getattr__(name: str):
    if name in __all__:
        from app.ops.orchestrator import ResearchCycleResult, ResearchOrchestrator

        exports = {
            "ResearchCycleResult": ResearchCycleResult,
            "ResearchOrchestrator": ResearchOrchestrator,
        }
        return exports[name]
    raise AttributeError(name)
