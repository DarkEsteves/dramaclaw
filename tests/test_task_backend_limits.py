from novelvideo.task_backend import limits


def test_default_video_limits_match_stable_video_admission_policy(monkeypatch):
    for name in (
        "ST_PROJECT_MAX_ACTIVE_VIDEO_TASKS",
        "ST_PROJECT_USER_MAX_ACTIVE_VIDEO_TASKS",
        "ST_CE_GLOBAL_MAX_ACTIVE_VIDEO_TASKS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert limits.project_lane_active_limit("video") == 4
    assert limits.project_user_lane_active_limit("video") == 1
    assert limits.project_lane_effective_active_limit("video", eligible_user_count=1) == 1
    assert limits.global_lane_concurrency("video") == 2


def test_speech_lane_defaults_enforce_one_global_and_one_user_job(monkeypatch):
    for name in (
        "ST_PROJECT_MAX_ACTIVE_SPEECH_TASKS",
        "ST_PROJECT_USER_MAX_ACTIVE_SPEECH_TASKS",
        "ST_CE_GLOBAL_MAX_ACTIVE_SPEECH_TASKS",
        "ST_CE_GLOBAL_MAX_QUEUED_SPEECH_TASKS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert limits.project_lane_active_limit("speech") == 1
    assert limits.project_user_lane_active_limit("speech") == 1
    assert (
        limits.project_lane_effective_active_limit("speech", eligible_user_count=8)
        == 1
    )
    assert limits.global_lane_concurrency("speech") == 1
    assert limits.global_lane_queue_limit("speech") == 64
