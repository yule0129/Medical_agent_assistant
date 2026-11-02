ACTION_RULES='''
第三层：可在actions中提出最多3条行政工作流行动候选，不执行行动。没有明确可行下一步时actions=[]。
- RESULT_AVAILABLE：可CREATE_FOLLOWUP，purpose=REVIEW_RESULT，提醒查看现有结果并记录处理；不能建议新的检查或治疗。
- CONFLICT/UNCLEAR：可CREATE_FOLLOWUP，purpose=VERIFY_INFORMATION，核实原事项的资料差异；不得编造新的临床计划。
- RESPONSE_RECORDED：可COMPLETE_MATTER，请医生核对执行依据后决定完成；也可CREATE_FOLLOWUP，purpose=RECORD_RESPONSE，补充现有事项的处理记录。
- PLAN_CHANGED：可REVISE_PLAN或CANCEL_PLAN，必须引用新医生记录中的明确变更或取消；不能自行取消计划。
- WAITING且past_due=true：可RESCHEDULE，请医生重新安排时间；没有依据时不能指定新日期。
- matter.parent_id不为空时，不再CREATE_FOLLOWUP，避免重复产生下一层子任务。
source_ids只能取本次references中已经引用的来源，due_text必须逐字出自所引原文中的时间表达，无时间留空。
RESCHEDULE/REVISE_PLAN不提供模型猜测的绝对时间，具体时间与负责人由医生批准时选择。purpose仅CREATE_FOLLOWUP使用，其余为null。
每条建议必须对应matter的具体问题，不能把其他事项的计划变化用于当前事项。关联不清时不给可执行建议。
所有建议都需说明rationale，并清楚体现医生批准后才生效；不得开立医嘱、药物、检查或自动宣称事项完成。
'''
