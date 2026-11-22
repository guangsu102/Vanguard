from datetime import datetime
from types import SimpleNamespace

from app.api.group_qualification import review_status


def test_exhausted_with_pending_event_reports_real_recovery_wait():
    row = SimpleNamespace(state='completed', decision='technical_wait', next_retry_at=None,
                          checked_at=datetime(2026, 11, 4))
    result = review_status(row, {'technical_recovery_exhausted':True, 'unknowns':[],
        'pending_collection':{'unknowns':['identity_unknown'], 'collection_progress':{
            'pending_message_ids':[1], 'not_evaluated_message_ids':[2,3]}}},
        {'required_reads':34, 'retry_at':'2026-11-03T01:00:00'}, {'kinds':['rules']})
    assert result['stage'] == 'technical_evidence_wait'
    assert result['event_pending'] and result['effective_unknowns'] == ['identity_unknown']
    assert result['pending_message_count'] == 1 and result['not_evaluated_message_count'] == 2
    assert result['required_reads'] is None
    assert result['waiting_reason'] == 'new_evidence_required'
