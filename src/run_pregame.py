# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# Historical MLB step map, not a standalone orchestration entrypoint.
MLB_NARR_ENV = {'NARR_REASONING_PROVIDER': 'codex', 'NARR_INCLUDE_DEEPREAD_V3': '1', 'NARR_INCLUDE_DEEPREAD_V3_LENS': '1'}

def mlb_pregame_steps(ymd, ymdc):
    """MLB pre-game 명령 시퀀스 (순서 = 실행 순서). step 스키마는 run_postgame과 공통.
    predict 만 critical(rc 반영); 나머지는 warn-only(현재 동작 보존)."""
    return [{'cmd': 'py _ingest_days.py', 'critical': False, 'warn': '[WARN] form ingest rc={rc} (narr_context may be stale)', 'extra_env': None}, {'cmd': 'py _mlb_statcast_backfill.py', 'critical': False, 'warn': '[WARN] statcast backfill rc={rc} (predict continues; some games may be partial)', 'extra_env': {'PYTHONIOENCODING': 'utf-8'}}, {'cmd': 'py run_mlb_predict.py --no-notification', 'critical': True, 'warn': None, 'extra_env': None}, {'cmd': 'py src/narr_context.py {date}', 'critical': False, 'warn': '[WARN] narr_context rc={rc} (narrator slate context failed)', 'extra_env': None}, {'cmd': 'py _mlb_reason_master_freshness.py {date}', 'critical': False, 'warn': '[WARN] reason master freshness rc={rc} (narrator retrospective memory may be stale)', 'extra_env': None}, {'cmd': 'py _narr_priors.py {date}', 'critical': False, 'warn': '[WARN] narr_priors rc={rc} (deepread priors/asof may be stale)', 'extra_env': MLB_NARR_ENV}, {'cmd': 'py test_narr_inputs.py {date}', 'critical': False, 'warn': '[WARN] narrator input TDD rc={rc} (coverage/leak/missing check failed)', 'extra_env': MLB_NARR_ENV}, {'cmd': 'py _narr_favconv_track.py --predictions', 'critical': False, 'warn': '[WARN] favconv internal tracking rc={rc} (non-critical, no Notification)', 'extra_env': None}]
