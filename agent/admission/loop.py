import time
from .contracts import Decision, DraftError, HEADINGS, TOOLS


def generate(provider, sources, timeout=100):
    """Bounded decide/read/observe loop; evidence IDs checked before accepting a draft."""
    deadline = time.monotonic() + timeout
    observations, trace, read = [], [], set()
    try:
        for step in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DraftError("BUDGET_EXHAUSTED")
            decision = Decision.model_validate(provider.decide(observations, remaining))
            trace.append({"step": step+1, "action": decision.action, "tool_name": decision.tool_name})
            if time.monotonic() >= deadline:
                raise DraftError("BUDGET_EXHAUSTED")
            if decision.action == "call_tool":
                if not decision.tool_name or decision.sections:
                    raise DraftError("INVALID_TOOL_ACTION")
                read.add(decision.tool_name)
                observations.append({"tool_name": decision.tool_name, "sources": sources[decision.tool_name]})
                trace[-1]["source_ids"] = [s["id"] for s in sources[decision.tool_name]]
                continue
            if read != set(TOOLS):
                raise DraftError("REQUIRED_SOURCES_NOT_READ")
            if tuple(s.heading for s in decision.sections) != HEADINGS:
                raise DraftError("INVALID_SECTIONS")
            allowed = {s["id"] for rows in sources.values() for s in rows}
            exam_ids = {s["id"] for s in sources["read_examinations"]}
            for section in decision.sections:
                if not set(section.source_ids) <= allowed:
                    raise DraftError("UNKNOWN_SOURCE_REFERENCE")
                if not section.missing and not section.source_ids:
                    raise DraftError("MISSING_SOURCE_REFERENCE")
                if section.heading == "辅助检查" and not set(section.source_ids) <= exam_ids:
                    raise DraftError("EXAMINATION_SOURCE_MISMATCH")
            return {"sections": [s.model_dump() for s in decision.sections],
                    "missing_sections": [s.heading for s in decision.sections if s.missing],
                    "provider": provider.name, "model": provider.model}, trace
        raise DraftError("BUDGET_EXHAUSTED")
    except DraftError as exc:
        exc.trace = trace
        raise
    except Exception:
        raise DraftError("MODEL_FAILED", trace) from None
