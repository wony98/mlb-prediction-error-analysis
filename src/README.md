# 소스 지도

이 폴더와 `experiments/`, `archive/`, `notebooks/`, `prompts/`, `tests/`에는 기존 작업에서 고른 **87개 소스 발췌본**을 담았다. 운영 원본 그대로의 배포본은 아니다. 원본 데이터·모델·인증과 일부 의존 코드가 빠져 있어 전체 파이프라인을 실행할 수 없다.

[연구 보고서](../docs/RESEARCH_REPORT.md) · [파일별 변경표](../docs/CODE_CHANGELOG.md) · [입력·의존성 경계](../docs/CODE_DEPENDENCIES.md)

## 처리 순서와 읽을 파일

| 순서 | 구현 자료 | 볼 내용 |
|---|---|---|
| 1. 수집 | [_mlb_raw_pbp_refresh.py](_mlb_raw_pbp_refresh.py), [_mlb_statcast_backfill.py](_mlb_statcast_backfill.py) | 완료 경기 PBP와 투구·타구 자료 수집 |
| 2. 학습 입력 | [_mlb_build_train.py](_mlb_build_train.py), [_mlb_sc_features.py](_mlb_sc_features.py), [_mlb_sp_asof_clean.py](_mlb_sp_asof_clean.py) | 경기·선수 연결, rolling 및 경기일 이전 기록 |
| 3. 모델 | [_mlb_train_deploy.py](_mlb_train_deploy.py), [_mlb_train_deploy_run_total.py](_mlb_train_deploy_run_total.py) | 승패 분류와 총득점 회귀를 따로 구성 |
| 4. 경기 전 준비 | [run_pregame.py](run_pregame.py), [_mlb_pregame_freeze_inputs.py](_mlb_pregame_freeze_inputs.py), [run_mlb_v3.py](run_mlb_v3.py) | MLB 단계 지도, 입력 고정, 모델 예측과 근거 저장 |
| 5. 네레이터 | [_narr_codex_run.py](_narr_codex_run.py), [_mlb_narr_c3_per_game_run.py](_mlb_narr_c3_per_game_run.py), [narr_context.py](narr_context.py), [_narr_priors.py](_narr_priors.py) | 경기별 입력·추론 분리, 선수 사실과 누적 회고 연결 |
| 6. 계약 검사 | [_mlb_narr_c3_crosscheck.py](_mlb_narr_c3_crosscheck.py), [_mlb_narr_c3_assemble.py](_mlb_narr_c3_assemble.py) | 경기 ID·입력 hash·응답 구조를 검사하고 조립 |
| 7. 결과·회고 | [run_postgame.py](run_postgame.py), [run_mlb_update.py](run_mlb_update.py), [_mlb_deepread_cli.py](_mlb_deepread_cli.py), [_mlb_synth_cli.py](_mlb_synth_cli.py) | 실제 결과 갱신, 정독, 경기 전 알 수 있었던 정보와 사후 사건 분리 |
| 8. 누적 활용 | [_narr_synth_lessons.py](_narr_synth_lessons.py), [_narr_miss_log.py](_narr_miss_log.py), [_mlb_reason_master_freshness.py](_mlb_reason_master_freshness.py) | 교훈·미스 기록, 회고 입력 최신성 확인 |

`run_pregame.py`와 `run_postgame.py`는 실행 순서를 보여주는 함수 발췌본이다. 명령 목록을 실제로 실행하는 완성 runner로 제공하지 않는다. C3의 `crosscheck`는 출력 계약 검사이고, Claude를 호출하는 독립 검토는 [_claude_crosscheck.py](_claude_crosscheck.py)에 따로 있다.

## 선수별 자료 연결

- [불펜 사용량 원장](_mlb_fullread_v4_bullpen_ledger.py)과 [예상 등판 후보](_mlb_fullread_v4_bullpen_appearance_projection.py): 최근 등판·휴식·역할을 구조화한다. 실제 등판을 확정하는 모델은 아니다.
- [개인 세부 지표](_mlb_player_detail_stats.py), [필드 계약](tools/mlb_evidence_field_contract.py), [능력·맞대결 자료](tools/mlb_ability_inputs.py): 투구·타구·스윙 자료를 개인 ID와 기간별로 연결한다.
- [공식 선수 기록](tools/mlb_official_player_history.py), [사전 출처 packet](tools/mlb_pregame_source_packet.py), [맞대결 검토](tools/mlb_matchup_review.py): 물리적 관측과 공식 기록, 확정 타순과 예상 불펜을 나눠 다룬다.

후기 직접 검토 경로와 앞선 운영 C3 경로는 다른 세대의 구현이다. 모든 기능이 하나의 운영 경로에 동시에 적용됐다는 뜻은 아니다.

## 연구·초기 구현·테스트

| 폴더 | 포함 내용 | 해석 경계 |
|---|---|---|
| [experiments](../experiments/) | 판단기준 추출·검색, 단계별 판정, 반복 일관성, 정독 입력 축약, Colab·Drive 처리 | 보류·실패한 실험도 포함. 검증된 예측 개선 방법이라는 뜻이 아님 |
| [archive](../archive/) | 구형 v2 로딩, 초기 CSV 병합·시장가격 보존 수정 전후 | v2는 과거 모델이며 후속 입력 준비에 재사용된 부분과 분리해 읽음 |
| [notebooks/archive](../notebooks/archive/) | 초기 수집·학습·EDA·결과 기록 | 저장 출력 제거. 알려진 누수와 당시 오류를 경고와 함께 보존 |
| [prompts](../prompts/) | 네레이터·정독·합성 지침 | 역사적 가설과 지침이며 현재 확립된 법칙이 아님 |
| [tests](../tests/) | 당시 입력·이닝 계산·파서 회귀검사 | 이번 정리에서 실행하지 않음. 실제 파일·일부 네트워크·미포함 fixture/모듈 필요 |

## 실행에 관한 제한

시장가격 사이트 수집 구현과 메시지 송신·금액 계산은 제외했다. 분석 결과는 파일로 저장하도록 바꿨다. `<DATA_ROOT>` 등 경로 자리표시자는 자동으로 확장되지 않는다. 사용자 환경에 맞춰 경로와 미포함 의존성을 연결해야 하며, 현재 저장소만으로 운영 당시 결과를 재현할 수는 없다.

검수한 것은 Python 구문, 노트북 구조, 특정 계산·입력 경계의 정적 비교다. 학습·예측·회고·테스트·노트북 셀을 다시 실행한 결과가 아니다.

운영 당시 코드에서 예측 선택·선택 표시·메시지 송신을 뜻하던 이름은 공개본에서 prediction·selection·notification 계열 중립 이름으로 바꿨다. 계산·조건·제어 흐름은 바꾸지 않았고 변경 전후를 토큰 단위로 대조했다. 그래서 변수·키·파일 이름이 운영 당시 원본 및 원본 데이터의 열 이름과 다르다.
