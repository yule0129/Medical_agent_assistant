"""One bounded online check with fixed synthetic text only; no development DB reads."""
import json
from pathlib import Path
from backend.config import settings  # load local configuration; do not print credentials
from .provider import get_provider


def main():
    source={'kind':'ROUNDING','id':1,'version':1,'label':'合成查房问诊',
        'at':'2026-10-30T01:00:00+00:00','text':
        '【完全为合成测试】医生：药物过敏史还需向女儿核实。明天复查血常规。请联系心内科会诊。患者：今天咳嗽少了。'}
    provider=get_provider()
    result=provider.extract(source,[])
    assert {'VERIFY_INFO','RECHECK','CONTACT'} <= {i.kind for i in result.items}
    assert all(i.quote in source['text'] and (not i.due_text or i.due_text in i.quote) for i in result.items)
    artifact={'synthetic_only':True,'model':provider.model,'source':source,'extraction':result.model_dump(),
              'validated':'source quotes and explicit plan categories; clinician review still required'}
    path=Path('docs/verification/patient-matters-deepseek-2026-10-30.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(artifact,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: online patient-matter extraction;',len(result.items),'grounded proposals; model:',provider.model)
    print('Synthetic-only artifact:',path)


if __name__=='__main__':main()
