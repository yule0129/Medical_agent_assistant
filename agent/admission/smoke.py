"""Online admission Agent check using only fixed synthetic sources; no development DB writes."""
import json
from pathlib import Path
from backend.config import settings  # load local configuration without displaying credentials
from .loop import generate
from .provider import get_provider
from .contracts import DraftError


def main():
    sources = {
        "read_patient_profile": [{"id": "profile:synthetic:1", "data": {
            "name": "合成入院测试患者", "gender": "女", "age": 45,
            "admitted_at": "2026-10-26T01:00:00+00:00", "history_reporter": "患者本人"}}],
        "read_interview": [{"id": "interview:synthetic:v1", "text":
            "【以下完全为合成测试】医生：哪里不舒服，多久了？患者：咳嗽3天，伴少量白痰。"
            "医生：有发热吗？患者：没有测量体温。医生：既往病史和过敏史还需要进一步核实。"
            "医生：暂未完成体格检查，初步诊断尚待明确。"}],
        "read_examinations": [{"id": "lab:synthetic:v1", "kind": "合成检查报告",
            "test_name": "血常规", "reported_at": "2026-10-26T02:00:00+00:00",
            "text": "【合成测试】白细胞计数12.0×10^9/L。未提供其他项目结果。"}],
    }
    try:
        draft, trace = generate(get_provider(), sources)
    except DraftError as exc:
        print('Generation failed:', str(exc), 'action trace:', exc.trace)
        raise
    artifact = {"synthetic_only": True, "draft": draft, "trace": trace, "sources": sources}
    path = Path('docs/verification/admission-deepseek-2026-10-26.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS: online admission Agent; model:', draft['model'], 'steps:', len(trace),
          'sections:', len(draft['sections']), 'missing sections:', len(draft['missing_sections']))
    print('Synthetic-only verification artifact:', path)


if __name__ == '__main__':
    main()
