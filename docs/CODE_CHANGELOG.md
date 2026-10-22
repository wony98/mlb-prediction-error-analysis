# 코드 정리 변경표

운영 당시 코드에서 선택한 87개 소스를 정리한 발췌본이다. 원천 데이터·모델 가중치·인증정보·전체 의존 모듈은 포함하지 않는다. 파일명이나 함수가 남아 있다는 이유로 지금 실행 가능한 기능이라고 해석하면 안 된다.

## 보존한 것

- 데이터 수집·결합, 학습, 예측 근거 구성, 정독·사전/사후 분리, 선수 기록과 대표 실험의 구현 자료.
- 학습 코드 두 개의 모델 파라미터와 fit 호출, RUN_TOTAL 예상 총점·bias 차감·방향 계산은 원본 AST와 같은 것을 정적으로 확인했다.
- snapshot 읽기와 과거 replay 입력 경로. 날짜·hash·coverage 검사도 남겼다.
- 구형 v2 모델 로딩은 archive에만 둔다. 초기 누수 노트북의 문제가 된 계산은 역사적 오류 사례로 보존하고 경고를 붙였다.

## 제거하거나 바꾼 것

- 시장가격 사이트 수집기 전체와 다른 파일 안의 live 조회·파싱을 제외했다. 시장가격 입력 열과 시장 확률 비교는 남겼다. 수집 제외 경로를 호출하면 명시적 오류를 내며 빈 성공 결과를 만들지 않는다.
- 메시지 송신기는 CSV/JSON 파일 저장으로 바꿨다. 추론 조율기의 후반 송신 단계는 로컬 reasoning-ready 영수증까지만 남긴다. 이는 외부 전달 성공 영수증이 아니다.
- 금액·수익률 계산과 조합 출력을 제거했다. 측정 단위, 통계 관측 단위, 예측 후보 선택용 과거 Boolean 필드(win_selection/run_total_selection)는 금액 계산이 아니므로 구분했다.
- 네레이터 support에서 금액 단계 2 이상 조건, 모델근거 validator에서 금액 단계 0 초과 조건을 제거했다. 이 두 조건과 금액에 연동된 zone/value 출력은 달라졌다. 원본과 동일한 판단 결과를 보장하지 않는다.
- 호스트 경로는 <DATA_ROOT>, <DRIVE_ROOT>, <USER_HOME>, <WORKSPACE> 자리표시자로 바꿨다. 문자열은 자동 확장되지 않으므로 실제 사용 시 경로 설정이 필요하다. Colab의 일반 /content/drive mount 경로는 개인 식별정보가 아니므로 남겼다.
- 노트북 저장 출력·실행 횟수·사용자 메타데이터·첨부를 제거했다. 재학습 노트북의 원본 index81 Yahoo 수집 셀은 source와 출력 모두 제외했다.
- 초기 collector/updater는 병합 함수·저장 경계만 발췌했다. historical_save_boundary 및 legacy_model_loading_excerpt는 원래 인라인 코드를 담기 위해 새로 이름을 붙인 wrapper다.
- 파이프라인 wrapper는 MLB 단계 지도만 남겼다. 다른 리그 dispatch 및 자동 송신 readiness 단계는 제외했고, 명령 목록을 자동 실행하지 않는다.
- 소스의 오래된 주석·가설·성능 표현은 보고서에서 확인한 최종 성과를 뜻하지 않는다. 대표 v3 파일의 손상된 설명·도움말·진단 표시를 코드에 근거해 다시 썼다. 원문 byte를 완전 복원했다는 뜻은 아니다. 검증된 사실·한계는 연구 보고서가 기준이다.

## 검수 범위

| 확인 | 결과 |
|---|---|
| 원본과 선정 수 | 87개 일치, 원본 해시 재확인 |
| Python | 78개 AST 파싱 통과. import·실행·테스트 실행은 하지 않음 |
| 노트북 | 8개 JSON 구조 확인, 저장 출력 0. 셀 실행은 하지 않음 |
| 프롬프트 | 1개 역사적 지침. 독립적으로 검증된 예측 법칙이라는 주장은 하지 않음 |
| 제외 경계 | 송신/비공개 시장 수집/금액 계산에 대한 지정 패턴 검사 통과 |
| 실행·재현 | 미검증. 누락 의존 모듈·입력·가중치 때문에 전체 실행 불가 |

패턴 검사만으로 모든 비밀정보나 의미 오류가 없다고 증명할 수는 없다. 독립 검토와 소유자의 최종 승인은 별도다.

## 공개 전 후속 정리

- 내부 호칭·검토 번호를 중립 표현으로 바꾸고, 컨텍스트·지침 실행 경로를 내보낸 위치에 맞췄다. 컨텍스트 기준 디렉터리는 src, 원격 포장 참고 코드의 기준 디렉터리는 저장소 루트다.
- v3의 손상된 docstring·주석·도움말·콘솔 표시를 수정했다. 확인한 백업 목록에서 정상 문구 판본을 찾지 못해, 현재 코드에 근거한 새 설명으로 대체했다.
- 화면의 선택 표시를 먼저 중립화했고, 이후 용어 정리에서 변수·함수·키·비교값과 prompt 출력 라벨도 중립 이름으로 바꿨다.
- 텍스트는 LF로 통일하고 이미지 bytes는 유지했다. 소스 해시는 Git에 저장되는 LF bytes 기준으로 검증한다. 이전 줄바꿈 해시 불일치와 검사 실패 후 push 제어 오류 기록은 별도 감사 자료에 보존했다.
- 용어 정리: 운영 당시 예측 선택·선택 표시·메시지 송신을 뜻하던 이름을 prediction·selection·notification 계열 중립 이름으로 일괄 변경했다. 변수·함수·파일·키·명령행 옵션·환경변수·prompt 라벨과 노트북·주석을 포함한다. 변경 전후를 토큰 단위로 대조해 연산자·숫자·제어 흐름이 같고, 이름 대응이 일대일이며, 의미가 다른 기존 이름과 겹치지 않음을 확인했다. 원본 데이터 파일의 열 이름과는 직접 호환되지 않는다. prompt 문장의 조사 오류 3곳도 바로잡았다.
- 시장 용어 정리: 총득점 기준선 대비 방향, 기준선, 점수차, 시장가격, 시장 수수료 등을 run_total·upper/lower·baseline·run_margin·market_price·market_fee 계열 중립 이름으로 바꿨다. 같은 파일에 이미 있던 total 변수·키, 신뢰도 등급, 기준점 위치 표시와 겹치지 않는 이름을 골랐다. 조각을 이어 붙여 만드는 열 이름과도 짝이 맞도록 모든 규칙을 단어 조각 단위로 적용했고, 정규식 이스케이프 뒤에 붙은 이름도 함께 바꿨다. 변경 전후 토큰 대조로 연산자·숫자·제어 흐름이 같고 이름 대응이 일대일임을 확인했다. 출처 이름은 유지했다. 총득점 기준점 값 앞의 o·u 접두 글자를 지우는 정규식 문자 집합은 이름이 아니라 글자 의미이므로 원래대로 두었다.
- 승패 축 이름 정리: 승패 축을 가리키던 약어를 코드·prompt에서는 WIN_LOSS·win_loss로, 문서에서는 "승패"로 바꿨다. 원래 모델 설정에서 승패 과제를 win_loss로 부르던 이름과 맞췄다. 승패 시장가격 열도 같은 조각 규칙에 따라 home_WIN_LOSS·away_WIN_LOSS가 된다. 신호 라벨 접두사는 WIN-LOSS-로 맞췄다. 변경 전후 토큰 대조로 이름 대응이 일대일이고 기존 이름·리터럴과 겹치지 않음을 확인했다.
- 노트북의 다운로드 중복 표시를 파일명에서 제거하고 링크·명세를 갱신했다.
- 원격 포장 코드의 숨김 인증 디렉터리 제외 규칙은 보안 경계라 삭제하지 않았다. 남은 해당 문자열은 옛 실행 경로가 아니다.
- 이 단계에서도 프로젝트 코드·노트북·회귀 테스트 실행은 하지 않았다. 구문과 허용된 표현/경로 변경 외 AST 차이를 정적으로 검사한다.

## 파일별 변경

| 파일 | 역할 | 정리 내용 |
|---|---|---|
| [src/_mlb_raw_pbp_refresh.py](../src/_mlb_raw_pbp_refresh.py) | Final 경기 PBP 수집 | Source retained with archive header; no model execution. |
| [src/_mlb_statcast_backfill.py](../src/_mlb_statcast_backfill.py) | Statcast 일별 수집·집계 | Source retained with archive header; no model execution. |
| [src/_mlb_lineup_backfill_5seasons.py](../src/_mlb_lineup_backfill_5seasons.py) | 과거 라인업 보강 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_weather_backfill_5seasons.py](../src/_mlb_weather_backfill_5seasons.py) | 과거 경기 환경 수집 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_ump_backfill.py](../src/_mlb_ump_backfill.py) | 주심 결측 보강 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_build_train.py](../src/_mlb_build_train.py) | 학습 테이블 결합·행수 검사 | Source retained with archive header; no model execution. |
| [src/_mlb_sc_features.py](../src/_mlb_sc_features.py) | 투구·타구 rolling 특성 | Source retained with archive header; no model execution. |
| [src/_mlb_live_features.py](../src/_mlb_live_features.py) | 예측용 특성 조립 | Source retained with archive header; no model execution. |
| [src/_mlb_train_deploy.py](../src/_mlb_train_deploy.py) | 승패 모델 학습·메타데이터 | Metadata prose neutralized; model parameters, feature selection and fit computations retained. |
| [src/_mlb_train_deploy_run_total.py](../src/_mlb_train_deploy_run_total.py) | 총득점 회귀·bias 저장 | Amount-tier table and metadata removed; regression, feature selection, bias and signal threshold retained. Formatting normalized. |
| [src/_mlb_sp_asof_clean.py](../src/_mlb_sp_asof_clean.py) | 선발 누적 성적 as-of 재구성 | Source retained with archive header; no model execution. |
| [archive/legacy_v2/run_mlb_predict.py](../archive/legacy_v2/run_mlb_predict.py) | 구형 v2 모델 추론 및 v3 재사용 입력 생성 이력 | Legacy v2: snapshot/identity helpers and original four-model loading block only; collection, execution, amount sizing and delivery omitted. New named wrapper encloses the historical loading statements. |
| [src/run_mlb_v3.py](../src/run_mlb_v3.py) | 일일 슬레이트·분석 연결 | Live market branch removed; frozen and strict replay branches retained. Missing inputs raise an explicit error. / Amount sizing, zones, combination outputs and related freeze/output columns removed; RUN_TOTAL predicted total, bias subtraction and direction formula retained. / Message formatter/sender replaced with CSV/JSON file output. Former sending layout and delivery accounting are not reproduced. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_pregame_freeze_inputs.py](../src/_mlb_pregame_freeze_inputs.py) | 입력 snapshot 고정 | Live collection entry fails explicitly; no empty-success market fallback. |
| [src/_narr_codex_run.py](../src/_narr_codex_run.py) | Codex 추론 단계 조율 | Hard-stop notification replaced by a local JSON receipt, retaining completeness audit call. / Post-reasoning delivery/receipt phase replaced with explicit local reasoning-ready JSON; no successful external-delivery claim is produced. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_narr_c3_per_game_run.py](../src/_mlb_narr_c3_per_game_run.py) | C3 경기별 격리 prompt·실행 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_narr_c3_assemble.py](../src/_mlb_narr_c3_assemble.py) | 경기별 응답 조립 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_narr_c3_crosscheck.py](../src/_mlb_narr_c3_crosscheck.py) | C3 hash·출력 계약 검사 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_narr_reasoning.py](../src/_narr_reasoning.py) | 근거·과거 회고 prompt 조립 | Removed an amount-derived support shortcut (amount tier at least 2); explicit zone/edge evidence remains. This changes one prompt-support condition. / Default context/rule paths reconnected to the exported src and prompts locations. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. / LLM 지시문의 수익률 관련 용어 금지 표현을 '수익률 논의 대신 주어진 사실로 경기 내용을 설명'하는 문장으로 바꿨다. Prompt 내용 변경이며 동일한 LLM 응답을 보장하지 않는다. |
| [src/narr_context.py](../src/narr_context.py) | 선수 사실 컨텍스트 생성 | Source retained with archive header; no model execution. |
| [prompts/_narr_rules.md](../prompts/_narr_rules.md) | 네레이터 판단 지침 원문 | Historical prompt preserved with warning; delivery/amount instructions and private identifiers removed. |
| [src/_narr_priors.py](../src/_narr_priors.py) | 선수별 누적 회고 사전 조회 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/run_postgame.py](../src/run_postgame.py) | 결과·회고 단계 조율 | MLB stage map only; other-league dispatch/main execution and messaging/readiness auto-send steps omitted. This map does not execute commands. |
| [src/_mlb_deepread_cli.py](../src/_mlb_deepread_cli.py) | 정독 LLM 호출·증분 처리 | Source retained with archive header; no model execution. |
| [src/_mlb_synth_cli.py](../src/_mlb_synth_cli.py) | 사전·사후 분리 합성 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_reason_bundles.py](../src/_reason_bundles.py) | PBP·캐시 회고 입력 조립 | Source retained with archive header; no model execution. |
| [src/_synth_input.py](../src/_synth_input.py) | 과거 폼·시즌 시퀀스 조립 | Source retained with archive header; no model execution. |
| [src/run_mlb_update.py](../src/run_mlb_update.py) | 공식 결과 갱신 | Amount and return reporting and automatic downstream backfill omitted; result matching and CSV writes retained; simple accuracy summary added. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_prediction_delta_review.py](../src/_mlb_prediction_delta_review.py) | 모델·네레이터 결과 차이 회고 | Source retained with archive header; no model execution. |
| [src/_recap_to_master.py](../src/_recap_to_master.py) | 정독 결과 병합·후속 갱신 | Source retained with archive header; no model execution. |
| [src/_mlb_fullread_v4_bullpen_ledger.py](../src/_mlb_fullread_v4_bullpen_ledger.py) | 불펜 stint·사전 사용량 원장 | Source retained with archive header; no model execution. |
| [src/_mlb_fullread_v4_bullpen_appearance_projection.py](../src/_mlb_fullread_v4_bullpen_appearance_projection.py) | 불펜 등판 후보 구성 | Source retained with archive header; no model execution. |
| [src/_mlb_player_detail_stats.py](../src/_mlb_player_detail_stats.py) | 선수별 투구·타구 상세 집계 | Source retained with archive header; no model execution. |
| [src/tools/mlb_ability_inputs.py](../src/tools/mlb_ability_inputs.py) | 개인 능력·맞대결 셀 구성 | Source retained with archive header; no model execution. |
| [src/tools/mlb_evidence_field_contract.py](../src/tools/mlb_evidence_field_contract.py) | 필드 용도·미등록 필드 계약 | Source retained with archive header; no model execution. |
| [src/tools/mlb_pregame_source_packet.py](../src/tools/mlb_pregame_source_packet.py) | 사전 source family 연결 | Source retained with archive header; no model execution. |
| [src/tools/mlb_official_player_history.py](../src/tools/mlb_official_player_history.py) | 공식 선수 gameLog·부담 집계 | Source retained with archive header; no model execution. |
| [src/_mlb_bvp_matchup_ledger.py](../src/_mlb_bvp_matchup_ledger.py) | 선수 ID 기반 직접 맞대결 원장 | Source retained with archive header; no model execution. |
| [experiments/criteria/_mlb_direction_criterion_season_extract.py](../experiments/criteria/_mlb_direction_criterion_season_extract.py) | PBP 기반 자연어 판단기준 추출 | Source retained with archive header; no model execution. |
| [experiments/criteria/_mlb_direction_criterion_retrieval.py](../experiments/criteria/_mlb_direction_criterion_retrieval.py) | 기준 날짜 제한·조건 검색 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [experiments/adjudication/_mlb_layered_adjudicator_research.py](../experiments/adjudication/_mlb_layered_adjudicator_research.py) | 단계별 근거·충돌·최종 판정 | Source retained with archive header; no model execution. |
| [experiments/direct_review/mlb_direct_review_run.py](../experiments/direct_review/mlb_direct_review_run.py) | 직접 정독 완료물·방향 검증 | Source retained with archive header; no model execution. |
| [experiments/token_budget/mlb_inline_read_input.py](../experiments/token_budget/mlb_inline_read_input.py) | 정독 중복입력 축약·참조 복원 | Source retained with archive header; no model execution. |
| [experiments/remote/_colab_offload.py](../experiments/remote/_colab_offload.py) | Colab 단계별 패키징·동일성 비교 | Shared-memory/thread documentation removed from remote payload file lists. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [experiments/remote/_colab_drive_pipeline.py](../experiments/remote/_colab_drive_pipeline.py) | Drive API 원격 원장 처리 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [experiments/stability/repeated_reasoning_metrics.py](../experiments/stability/repeated_reasoning_metrics.py) | 반복 추론 방향·문턱 일관성 집계 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [experiments/replay/_mlb_replay_asof.py](../experiments/replay/_mlb_replay_asof.py) | 과거 날짜 as-of 사전 입력 | Source retained with archive header; no model execution. |
| [experiments/leakage/_mlb_baseline_clean.py](../experiments/leakage/_mlb_baseline_clean.py) | clean baseline 비교 | Source retained with archive header; no model execution. |
| [experiments/remote/mlb_daily_history_refresh.py](../experiments/remote/mlb_daily_history_refresh.py) | 일일 원격 history 갱신·반영 보호 | Source retained with archive header; no model execution. |
| [src/_mlb_train_v3.py](../src/_mlb_train_v3.py) | 공통 학습·특성 선택 함수 | Source retained with archive header; no model execution. |
| [src/_mlb_open_validate.py](../src/_mlb_open_validate.py) | 학습 자료 load 함수 | Only data-loader/closing-market classifier and declarations retained; financial-return evaluation omitted. |
| [src/_mlb_run_total_validate_clean.py](../src/_mlb_run_total_validate_clean.py) | closing 시장 열 판별 | Only data-loader/closing-market classifier and declarations retained; financial-return evaluation omitted. |
| [src/_codex_exec_bridge.py](../src/_codex_exec_bridge.py) | Codex CLI timeout·결과 처리 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_claude_bridge.py](../src/_claude_bridge.py) | Claude CLI 호출 경계 | Source retained with archive header; no model execution. |
| [src/_claude_crosscheck.py](../src/_claude_crosscheck.py) | Claude 독립 교차검토 요청 | Crosscheck CLI league selection restricted to MLB. |
| [prompts/_make_batch_wf.py](../prompts/_make_batch_wf.py) | 정독 프롬프트 계약 | Only the historical prompt constant retained; workflow launch/generation omitted. |
| [prompts/_make_synth_wf.py](../prompts/_make_synth_wf.py) | 사전·사후 합성 프롬프트 | Only the historical prompt constant retained; workflow launch/generation omitted. |
| [src/_sp_form.py](../src/_sp_form.py) | 선발 과거 폼 표 | Source retained with archive header; no model execution. |
| [src/_mlb_narr_contract_requirements.py](../src/_mlb_narr_contract_requirements.py) | 네레이터 출력 필수항목 계약 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_narr_model_defense_validate.py](../src/_narr_model_defense_validate.py) | 모델근거·네레이터 행 검사 | Amount-derived model-active shortcut removed; other explicit model-evidence checks retained. This changes that eligibility branch. / Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_mlb_result_status.py](../src/_mlb_result_status.py) | 최종·중단 경기 상태 분류 | Source retained with archive header; no model execution. |
| [src/_mlb_name_list.py](../src/_mlb_name_list.py) | 이름·선수 ID 연결 보조 | Source retained with archive header; no model execution. |
| [src/tools/mlb_matchup_review.py](../src/tools/mlb_matchup_review.py) | 선발·타자·불펜 검토 계약 | Source retained with archive header; no model execution. |
| [src/tools/mlb_full_review_views.py](../src/tools/mlb_full_review_views.py) | 선수 세부 원문 비교 뷰 | Source retained with archive header; no model execution. |
| [src/tools/mlb_news_recovery_audit.py](../src/tools/mlb_news_recovery_audit.py) | 사전 뉴스 출처·시점 검사 | Source retained with archive header; no model execution. |
| [experiments/remote/_colab_drive_api.py](../experiments/remote/_colab_drive_api.py) | Drive API 입출력 | Source retained with archive header; no model execution. |
| [notebooks/archive/mlb_v2_final.ipynb](../notebooks/archive/mlb_v2_final.ipynb) | 초기 홈승 모델·누수 사례 | Removed 0 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/mlb_backtest_v11.ipynb](../notebooks/archive/mlb_backtest_v11.ipynb) | 초기 날짜별 GPT prompt 생성 | Removed 0 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/모델재학습.ipynb](../notebooks/archive/%EB%AA%A8%EB%8D%B8%EC%9E%AC%ED%95%99%EC%8A%B5.ipynb) | 모델 재학습·정합성 조사 | Removed original zero-based cell 81 (position 82), Yahoo request/parser code and its one output. / Removed 85 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/mlb머신러닝.ipynb](../notebooks/archive/mlb%EB%A8%B8%EC%8B%A0%EB%9F%AC%EB%8B%9D.ipynb) | 초기 EDA·모델 비교 | Removed 23 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/mlb_년도별_정규데이터_크롤링_노트북.ipynb](../notebooks/archive/mlb_%EB%85%84%EB%8F%84%EB%B3%84_%EC%A0%95%EA%B7%9C%EB%8D%B0%EC%9D%B4%ED%84%B0_%ED%81%AC%EB%A1%A4%EB%A7%81_%EB%85%B8%ED%8A%B8%EB%B6%81.ipynb) | 정규시즌 공식 원자료 수집 | Removed 37 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/mlb_backtest_v11_파라미터화.ipynb](../notebooks/archive/mlb_backtest_v11_%ED%8C%8C%EB%9D%BC%EB%AF%B8%ED%84%B0%ED%99%94.ipynb) | 연도 파라미터화 prompt 생성 | Removed 6 saved output objects, execution counts and user/session metadata; added archive warning. |
| [src/run_pregame.py](../src/run_pregame.py) | 경기 전 갱신·입력 준비 단계 순서 | MLB stage map only; other-league dispatch/main execution and messaging/readiness auto-send steps omitted. This map does not execute commands. |
| [src/_form_label.py](../src/_form_label.py) | 선수 폼 라벨·타임라인 | Source retained with archive header; no model execution. |
| [src/_bp_form.py](../src/_bp_form.py) | 불펜 직전 폼 | Source retained with archive header; no model execution. |
| [src/_ingest_days.py](../src/_ingest_days.py) | 완료 경기 원장 ingest | Source retained with archive header; no model execution. |
| [src/_narr_synth_lessons.py](../src/_narr_synth_lessons.py) | 사전 교훈 누적·조회 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [src/_narr_miss_log.py](../src/_narr_miss_log.py) | 빗나간 판단·사전 근거 기록 | Source retained with archive header; no model execution. |
| [src/_mlb_reason_master_freshness.py](../src/_mlb_reason_master_freshness.py) | 회고 입력 최신성 점검 | Private host/account roots and internal work-note identifiers replaced with documented placeholders. |
| [tests/test_narr_inputs.py](../tests/test_narr_inputs.py) | 사전 회고·coverage·freeze 입력 회귀검사 | Source retained with archive header; no model execution. |
| [tests/test_career_era.py](../tests/test_career_era.py) | ERA 합산·이닝 환산·비율 재계산 검사 | Source retained with archive header; no model execution. |
| [tests/test_narr_context_lineup_parsing.py](../tests/test_narr_context_lineup_parsing.py) | 라인업 문자열·불펜 as-of 회귀검사 | Test module path points to exported src/narr_context.py; original CSV fixture remains excluded and required for its fixture-dependent test. |
| [archive/early_pipeline/collector/daily_collector.py](../archive/early_pipeline/collector/daily_collector.py) | 초기 사전 수집 CSV 병합 함수 발췌 | Only historical CSV merge/save boundary retained; requests and market parsing omitted. New historical_save_boundary wrapper names the formerly inline statements. |
| [archive/early_pipeline/before/daily_updater.py](../archive/early_pipeline/before/daily_updater.py) | 사후 시장가격 덮어쓰기 가능 판본의 병합·저장 경계 | Only historical CSV merge/save boundary retained; requests and market parsing omitted. New historical_save_boundary wrapper names the formerly inline statements. |
| [archive/early_pipeline/after/daily_updater.py](../archive/early_pipeline/after/daily_updater.py) | 사전 시장가격 보존을 위해 사후 저장 제거한 판본 | Only historical CSV merge/save boundary retained; requests and market parsing omitted. New historical_save_boundary wrapper names the formerly inline statements. |
| [notebooks/archive/daily_predictor.ipynb](../notebooks/archive/daily_predictor.ipynb) | 초기 로컬 일일 예측 노트북 | Removed 0 saved output objects, execution counts and user/session metadata; added archive warning. |
| [notebooks/archive/result_tracker.ipynb](../notebooks/archive/result_tracker.ipynb) | 초기 결과 누적 노트북 | Removed 0 saved output objects, execution counts and user/session metadata; added archive warning. |

[소스 지도](../src/README.md) · [의존성 및 입력 경계](CODE_DEPENDENCIES.md) · [해시 명세](CODE_MANIFEST.json)

## 2026-10-23 추가: MLflow 재실험 (발췌본 아님, 새로 작성)

- `experiments/mlflow/`: 규칙 탐지 누수 열이 있는 피처 세트와 없는 세트를 같은 분할에서 다시 학습하고 MLflow로 기록하는 러너(`train_clean_vs_leaky.py`), 병합 반례 테스트(`test_merge_spa.py`), 고정 버전 `requirements.txt`, 설계·실행법 `README.md`, 실행 요약 `outputs/summary.csv`·`runs_export.csv`·`data_checks.json`·`code_snapshot/code_manifest.json`. 저장소의 기존 정의(`src/_mlb_train_v3.py`, `experiments/leakage/_mlb_baseline_clean.py`)를 import해 재사용한다.
- 원본 데이터 열 이름은 저장소의 용어 정리와 호환되지 않으므로 로컬 매핑 파일(저장소 미포함)로 중립 이름에 연결한 뒤 실행한다. 매핑 파일·추적 DB·아티팩트·모델·데이터는 저장소에 포함하지 않는다.
- `docs/mlflow_leakage_experiment.md`: 2026-10-23 로컬 CPU 실행 결과·해석·결정·한계. 같은 날 공개 전 작업 사본에서의 첫 실행은 병합 결함으로 무효, 두 번째 실행은 수정 후 결과이며 이번 실행과 지표를 대조해 기록했다.
- 과거 운영 당시 MLflow를 쓴 것이 아니라 2026-10-23에 새로 돌린 실행이다. 87개 발췌본 목록(`CODE_MANIFEST.json`)에는 포함하지 않는다.
