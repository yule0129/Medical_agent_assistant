"""Deterministic task extraction; suggestions only, never medical orders."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentCandidate:
    intent_type: str
    raw_text: str
    target: str
    confidence: float
    fingerprint: str


def extract_intent(text: str) -> list[IntentCandidate]:
    found: list[IntentCandidate] = []
    seen: set[str] = set()
    for clause in re.split(r"[，,。；;！？!?\n]+", text):
        clause = clause.strip()
        if not clause or re.search(r"无需|无须|不用|不必|暂不|不需要|不再|不予|取消|已经|已查看|已完成|已(?:复查|复核|检查|监测|会诊|联系|安排)|不(?:复查|复核|检查|监测|观察|随访|会诊|联系|安排)|(?:昨日|昨天|上周|既往).*?(?:复查|检查|会诊)|结果显示|复查.*(?:示|正常|下降|升高)", clause):
            continue
        candidates: list[tuple[str, str, str]] = []
        match = re.search(r"([\u4e00-\u9fffA-Za-z0-9·_-]{1,40}?)(?:结果|报告)(?:出来|返回|回报|出具)?(?:后|时)?.*?(?:通知|提醒|告知)(?:我|医生)?", clause)
        if match:
            candidates.append(("FOLLOW_RESULT", match.group(1).removeprefix("等待"), clause))
        elif re.search(r"(?:结果|报告).*?(?:通知|提醒|告知)", clause):
            candidates.append(("FOLLOW_RESULT", "检验报告", clause))
        else:
            pattern = r"(复查|复核|检查|监测|观察|关注|随访|会诊|联系|安排)(.+?)(?=(?:并|及|然后)?(?:复查|复核|检查|监测|观察|关注|随访|会诊|联系|安排)|$)"
            for match in re.finditer(pattern, clause):
                verb, target = match.groups()
                target = target.strip(" 、及并")
                if not target:
                    continue
                kind = "RECHECK" if verb in {"复查", "复核", "检查"} else "MONITOR" if verb in {"监测", "观察", "关注", "随访"} else "ACTION"
                candidates.append((kind, target, clause))
            if not candidates and "会诊" in clause:
                candidates.append(("ACTION", clause, clause))
        for kind, target, raw in candidates:
            fingerprint = f"{kind}|{target}"
            if fingerprint not in seen:
                found.append(IntentCandidate(kind, raw, target, 0.85, fingerprint))
                seen.add(fingerprint)
    return found
