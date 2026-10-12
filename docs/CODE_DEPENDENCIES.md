# 의존성과 입력 경계

이 저장소는 전체 실행 패키지가 아니다. 데이터만 넣으면 원래 운영 파이프라인이 작동한다는 뜻도 아니다. 아래는 정적 import에서 확인한 경계이며 동적 호출·파일 참조까지 완결된 의존성 명세는 아니다.

## 필요한 입력과 미포함 자료

- 공식 PBP/boxscore/Statcast와 역사적 학습 테이블, 모델 가중치, 누적 회고·선수 원장 및 해당 날짜 snapshot은 포함하지 않는다.
- 시장가격 수집 구현은 비공개다. 외부에서 준비한 경기 ID·날짜·양 팀·시장 확률·총득점 기준점 및 메타데이터가 필요하다. run_mlb_v3의 SNAPSHOT_MARKET_PRICE_COLUMNS와 snapshot parser를 입력 계약의 출발점으로 본다.
- frozen snapshot과 strict replay cached 입력을 구분한다. 날짜·hash·coverage·기준점 검증을 무시해서 실행하면 안 된다.
- 외부 Codex/Claude CLI 및 Colab/Drive 코드는 인증·권한·별도 설치를 필요로 한다. 인증 파일과 실제 token은 저장소에 없다. 이 정리 작업에서는 외부 추론·원격 실행을 하지 않았다.
- src, src/tools, prompts, experiments로 원본이 나뉘어 있다. 일부 모듈은 경로/검색 경로 재연결이 더 필요하다. 이 저장소를 pip 패키지 또는 즉시 실행 가능한 CLI로 제공하지 않는다.
- 기존 테스트의 실제 자료·일부 네트워크·미포함 모듈/CSV fixture 의존은 남는다. test_era_asof_blend는 구현 부재로 편입하지 않았다. 새 fixture를 만들거나 테스트를 실행하지 않았다.

## 미포함 로컬 모듈

정적 import에서 36개가 확인됐다. 소스 범위를 임의로 넓히지 않기 위해 복사하지 않았다. 이 목록 외에 subprocess 명령·동적 import·데이터 경로 의존도 있다.

| 모듈 | 참조 예 |
|---|---|
| `_deepread_v2` | `src/_narr_priors.py` |
| `_deepread_v3` | `src/_narr_priors.py` |
| `_deepread_v3_lens` | `src/_narr_priors.py` |
| `_loss_score_lib` | `src/run_mlb_v3.py` |
| `_mlb_bb_adv` | `src/run_mlb_v3.py` |
| `_mlb_bt_may` | `src/run_mlb_v3.py` |
| `_mlb_bvp_narrator_context` | `src/_narr_reasoning.py` |
| `_mlb_career_era` | `src/run_mlb_v3.py` |
| `_mlb_direction_contrast_call_ledger` | `src/_mlb_narr_c3_per_game_run.py` |
| `_mlb_direction_contrast_contract` | `src/_mlb_narr_c3_per_game_run.py` |
| `_mlb_direction_criterion_calibration_source` | `experiments/criteria/_mlb_direction_criterion_season_extract.py` |
| `_mlb_direction_criterion_pilot_prepare` | `experiments/criteria/_mlb_direction_criterion_season_extract.py` |
| `_mlb_direction_criterion_quality_parity_5` | `experiments/criteria/_mlb_direction_criterion_season_extract.py` |
| `_mlb_direction_criterion_symmetric_v3` | `experiments/criteria/_mlb_direction_criterion_season_extract.py` |
| `_mlb_env_guard` | `src/run_mlb_v3.py` |
| `_mlb_final_adjudicator_six_game_pilot` | `experiments/adjudication/_mlb_layered_adjudicator_research.py` |
| `_mlb_injuries` | `src/narr_context.py` |
| `_mlb_judgment_routing_pilot` | `experiments/criteria/_mlb_direction_criterion_season_extract.py` |
| `_mlb_knowable_compliance` | `src/_mlb_narr_c3_per_game_run.py` |
| `_mlb_narrator_guard_context` | `src/_narr_reasoning.py` |
| `_mlb_narrator_sent_ledger` | `src/_mlb_prediction_delta_review.py` |
| `_mlb_run_total_focused_narrator_context` | `src/_narr_reasoning.py` |
| `_mlb_run_total_retro_dominance_context` | `src/_narr_reasoning.py` |
| `_mlb_pregame_evidence_matrix` | `src/_narr_reasoning.py` |
| `_mlb_pregame_signal_cards` | `src/_narr_reasoning.py` |
| `_mlb_raw_pbp_event_ledger_build` | `experiments/remote/mlb_daily_history_refresh.py` |
| `_mlb_recent_lineups` | `src/_narr_reasoning.py` |
| `_mlb_recent_sc` | `experiments/remote/mlb_daily_history_refresh.py` |
| `_mlb_schedule` | `src/_narr_codex_run.py` |
| `_mlb_send_owner_lock` | `src/run_mlb_v3.py` |
| `_narr_delta_lessons` | `src/_narr_priors.py` |
| `_narr_factor_label_normalizer` | `src/_narr_synth_lessons.py` |
| `_narr_filter_lib` | `src/run_mlb_v3.py` |
| `_narr_model_defense_context` | `src/_narr_reasoning.py` |
| `_narr_run_total_synth_lessons` | `src/_narr_priors.py` |
| `_narr_pbp` | `src/_ingest_days.py` |

## 외부 라이브러리 참조

`bs4`, `joblib`, `jsonschema`, `lightgbm`, `numpy`, `pandas`, `pyarrow`, `requests`, `sklearn`, `source_helpers`

위 목록은 소스에서 발견한 import 루트다. 설치 검증이나 버전 고정을 완료한 requirements가 아니다.
