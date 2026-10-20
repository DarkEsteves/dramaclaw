from novelvideo.freezone.remix.reference_analysis_identity import analysis_admission_key


def test_unicode_operation_id_becomes_stable_scoped_ascii_key():
    args = dict(project_id="p", canvas_id="c", source_node_id="source", user_goal="换产品",
                inputs={"direction": "咖啡"}, operation_id="分析原片-第一轮")
    first = analysis_admission_key(**args)
    assert first == analysis_admission_key(**args)
    assert first.isascii() and len(first) <= 160
    assert first != analysis_admission_key(**{**args, "source_node_id": "other"})
    assert first != analysis_admission_key(**{**args, "operation_id": "分析原片-第二轮"})
    assert first != analysis_admission_key(**{**args, "inputs": {"direction": "茶"}})


def test_existing_valid_key_is_preserved_for_replay():
    assert analysis_admission_key(project_id="p", canvas_id="c", source_node_id="s",
                                  user_goal="goal", inputs={}, operation_id="turn-a") == "turn-a"
