"""Real-model action suggestions on fixed synthetic sources, with no patient DB access."""
import json
from pathlib import Path
from backend.config import settings
from agent.patient_memory.progress.provider import get_provider
from agent.patient_memory.progress.runtime import validate
from .service import validation_error


def main():
    cases=[
        ('result','RECHECK','复查血常规','明天复查血常规。','RESULT_AVAILABLE',False,
         {'id':'lab','kind':'LAB','label':'血常规','text':'【合成结果】白细胞计数12×10^9/L。','version':1,'is_origin':False}),
        ('changed','CONTACT','联系心内科会诊','今天联系心内科会诊。','PLAN_CHANGED',False,
         {'id':'note','kind':'NOTE','label':'合成医生查房','text':'医生：原计划改为明天下午联系心内科会诊。','version':1,'is_origin':False}),
        ('response','VERIFY_INFO','向女儿核实过敏史','向女儿核实过敏史。','RESPONSE_RECORDED',False,
         {'id':'note','kind':'NOTE','label':'合成医生查房','text':'医生：已向女儿核实过敏史，确认青霉素过敏并已记录。','version':1,'is_origin':False}),
        ('overdue','CONTACT','联系家属','联系家属核实病史。','WAITING',True,None),
    ]
    provider=get_provider();results=[]
    for name,kind,title,quote,expected,past_due,new_source in cases:
        origin={'id':'origin','kind':'NOTE','label':'合成原计划','text':quote,'version':1,'is_origin':True,'at':'2026-10-31T01:00:00+00:00'}
        if new_source:new_source['at']='2026-10-31T02:00:00+00:00'
        snapshot={'matter':{'id':1,'kind':kind,'title':title,'quote':quote,'version':1,'parent_id':None,
            'active_child_purposes':[],'original_source':{'at':origin['at']}},'past_due':past_due,
            'coverage':{'complete':True,'scope':'固定合成资料'},'sources':[origin]+([new_source] if new_source else [])}
        observation=provider.observe(snapshot);validate(snapshot,observation)
        assert observation.state==expected,(name,observation.state)
        valid=[a for a in observation.actions if validation_error(a,observation,snapshot) is None]
        assert valid,(name,'no valid action proposal')
        results.append({'scenario':name,'snapshot':snapshot,'observation':observation.model_dump(),
            'valid_actions':[a.model_dump() for a in valid]})
        print('PASS:',name,','.join(a.action for a in valid),flush=True)
    path=Path('docs/verification/matter-actions-deepseek-2026-10-31.json')
    path.write_text(json.dumps({'synthetic_only':True,'model':provider.model,'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Synthetic-only artifact:',path)


if __name__=='__main__':main()
