from types import SimpleNamespace

from novelvideo.chat.reference_production_observer import production_event
from novelvideo.chat.reference_remix_continuation import _production_stage_instruction


def test_outline_notification_does_not_advance_to_full_plan_review(monkeypatch, tmp_path):
    import novelvideo.chat.reference_production_observer as module
    plan = {'id': 'plan', 'project_id': 'p', 'revision': 2, 'schema': 'production_plan.v2',
            'content_hash': 'outline', 'payload': {'authoring_stage': 'outline', 'beats': []}}
    monkeypatch.setattr(module, '_canvas_refs', lambda *args: [plan])
    monkeypatch.setattr(module, '_latest_plan_generation', lambda *args: None)
    commission = {'phase': 'drafting_plan', 'canvas_id': 'c', 'commission_ref': {'artifact_id': 'commission'}}
    event = production_event(SimpleNamespace(project_dir=tmp_path, project_id='p'), {}, commission)
    assert event['event'] is None
    assert event['authoring_stage'] == 'outline'
    assert '结构与台词已保存' in event['notice']
    assert '尚未安排' in event['notice']


def test_outline_instruction_requires_explicit_next_action():
    instruction = _production_stage_instruction({'phase': 'drafting_plan', 'authoring_stage': 'outline'})
    assert '结构与台词已保存' in instruction
    assert 'generate_production_plan_arrangement' in instruction
    assert '不得自动' in instruction


def test_manually_arranged_outline_can_follow_existing_review_path():
    from novelvideo.freezone.remix.production_review import is_unarranged_outline
    assert is_unarranged_outline({'authoring_stage': 'outline', 'placements': []})
    assert not is_unarranged_outline({'authoring_stage': 'outline', 'placements': [{'id': 'manual'}]})
    assert not is_unarranged_outline({'beats': []})


def test_arrangement_failure_instruction_preserves_saved_outline(monkeypatch, tmp_path):
    import novelvideo.chat.reference_production_observer as observer
    from novelvideo.chat.reference_remix_continuation import _active_plan_failure_instruction
    monkeypatch.setattr(observer, '_failed_plan_generation', lambda *args: {
        'event_key': 'failed-arrangement', 'action': 'generate_production_plan_arrangement'})
    instruction = _active_plan_failure_instruction({'phase': 'drafting_plan'},
        {'commission_event_receipts': ['failed-arrangement']}, {}, tmp_path)
    assert '结构、台词及人工修改仍已保存' in instruction
    assert '只重试画面与声音' in instruction


def test_single_pass_plan_guidance_preserves_historical_outline_continuation():
    from pathlib import Path
    root = Path(__file__).parents[1]
    for relative in (
        "src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md",
        "agent-kit/skills/dramaclaw-workflows/references/reference-video-remix.md",
        ".hermes/skills/workflows/references/reference-video-remix.md",
    ):
        text = (root / relative).read_text()
        assert "one model call" in text
        assert "authoring_stage=arranged" in text
        assert "historical saved outline" in text
        assert "first plan task drafts structure and dialogue" not in text
    workspace = (root / "src/novelvideo/chat/hermes_workspace.py").read_text()
    assert "一次模型调用生成完整作品方案" in workspace
    assert "历史保存的 authoring_stage=outline" in workspace


def test_main_skills_do_not_send_complete_draft_back_to_outline():
    from pathlib import Path
    root = Path(__file__).parents[1]
    for relative in (
        "src/novelvideo/agent_skills/dramaclaw-workflows/SKILL.md",
        "agent-kit/skills/dramaclaw-workflows/SKILL.md",
        ".hermes/skills/workflows/SKILL.md",
        ".hermes/skills/freezone/SKILL.md",
    ):
        text = (root / relative).read_text()
        assert "saves a complete editable plan in one model call" in text
        assert "historical saved outline" in text
        assert "saves structure and dialogue only" not in text
