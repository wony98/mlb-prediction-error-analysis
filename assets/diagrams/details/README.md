# 상세 구조도 읽는 법

검정 배경과 회색 노드는 공통으로 유지하고 연결선의 역할을 구분했다.

- **청록 실선: 기본 진행.** 정답이나 검사 완료를 뜻하지 않는다.
- **주황 실선: 재처리·실패·보류.** 정상적인 다음 섹션 반복과 구분한다.
- **보라 점선: 참고 · 별도 경로 · 연구.** 실제 저장 데이터 연결과 초기 운영 경로도 포함한다. 전부 연구 전용이라는 뜻은 아니다.

각 도식의 범례에는 실제 쓰이는 색만 표시한다. 세대가 다른 구현과 별도 연구는 구획·노드 문구로 구분했다. 모든 기능을 하나의 실행기에 동시에 적용했다는 의미가 아니다.

| 도식 | 설명 문서 | SVG | PNG | Mermaid |
|---|---|---|---|---|
| 네레이터 전체 | [설명](../../../docs/NARRATOR_ARCHITECTURE.md) | [SVG](narrator-detail.svg) | [PNG](narrator-detail.png) | [MMD](narrator-detail.mmd) |
| 긴 입력 대응 | [설명](../../../docs/NARRATOR_ARCHITECTURE.md#lost-in-the-middle에-대응한-입력기록-구조) | [SVG](narrator-context-detail.svg) | [PNG](narrator-context-detail.png) | [MMD](narrator-context-detail.mmd) |
| 판단기준 추출 | [설명](../../../docs/JUDGMENT_CRITERIA.md) | [SVG](criterion-extraction.svg) | [PNG](criterion-extraction.png) | [MMD](criterion-extraction.mmd) |
| 판단기준 적용 | [설명](../../../docs/JUDGMENT_CRITERIA.md) | [SVG](criterion-application.svg) | [PNG](criterion-application.png) | [MMD](criterion-application.mmd) |
| 머신러닝 | [설명](../../../docs/ML_ARCHITECTURE.md) | [SVG](ml-detail.svg) | [PNG](ml-detail.png) | [MMD](ml-detail.mmd) |

GitHub 화면에서는 큰 PNG가 축소된다. 글자를 자세히 읽을 때는 SVG 원본 또는 각 설명 문서의 단계별 확대본을 연다. 확대본은 구획을 잘라 보여주므로 구획 밖으로 이어지는 연결은 전체 도식에서 확인한다.

MMD는 구조·문구 원본이다. 이미지는 Mermaid CLI 12.0.0과 함께 둔 `mermaid-dark.json` 설정으로 렌더링한 뒤, SVG의 연결선 색·굵기·점선과 하단 범례를 별도로 적용했다. 따라서 MMD를 기본 설정으로 실행한 그림은 게시된 이미지와 색이 다를 수 있다. 구조나 문구를 수정하지 않는다면 게시된 SVG를 원본으로 사용한다.

내용·연결·시각 검토를 거친 자료다. 글자와 노드의 겹침 검사 및 실제 이미지 확인을 수행했지만, 모든 화면 크기에서의 가독성이나 예측 성능을 보증하는 검증은 아니다.
