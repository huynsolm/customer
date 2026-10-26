# 5차 검수 요청 (Codex)

1~4차 검수 지적은 모두 반영되어 있다(README "검증 구분" 표 참고). 이번 변경은 그 위에 얹은 것이므로 **이번 변경분 중심으로** 봐 달라.

**5차 범위**: ① 4차 지적 3건의 수정이 제대로 됐는지(아래 "4차 수정 재확인"), ② 새로 추가한 **품목 수정** 기능, ③ 앞서 요청한 변경분(아래 표) 중 아직 못 본 부분.
커밋 전 작업 트리 기준이다: `git diff` 와 새 파일 `test_stores.py`.

## 이번 변경 요약
| 변경 | 파일 |
|---|---|
| **품목 수정**(이름·발주 기준) — 5차 신규 | `inventory.update_item`, `POST /api/items/update`, `index.html`(수정 버튼) |
| AI 요약을 Claude → ChatGPT(OpenAI Chat Completions, Structured Outputs) | `summary.py`, `test_summary.py`, `requirements.txt` |
| 다중 매장(stores 테이블, items.store_id, 매장별 조회) | `inventory.py`, `server.py`, `index.html` |
| 품목 추가(멱등) | `inventory.add_item`, `POST /api/items`, `index.html` |
| 작업자 이름 기록(발주 등록·잔량 취소 포함) | `inventory.py`, `server.py`, `client.js`, `index.html` |
| 이전 DB 자동 이전(마이그레이션), WAL 전환 재시도 | `inventory._migrate*`, `_enable_wal` |
| 테스트 | `test_stores.py`(신규 12개), `test_summary.py`, `test_client.mjs`, `test_review2.py`(모킹 시그니처만) |

## 실행
```bash
python -m pip install -r requirements.txt   # openai (없어도 테스트 대부분 통과, RealSdk 테스트만 건너뜀)
python -m unittest -v                        # 75개
node --test test_client.mjs                  # 8개
```
실제 OpenAI 서버는 호출하지 않는다(API 키 없음). `RealSdk` 테스트는 진짜 `openai` SDK + `httpx.MockTransport`.

## 4차 수정 재확인 (지적 → 수정 → 확인 방법)
| 4차 지적 | 수정 | 확인 |
|---|---|---|
| P1 매장 전환 중 지연 응답으로 다른 매장 품목에 기록 | `render()`가 시작 시 매장을 캡처하고 모든 조회에 전달, DOM 반영 전 `st !== store`이면 폐기. `vStock/vPo/vHist(st)` | 브라우저에서 `/api/items?store=1` 응답을 1.5초 지연 후 매장 전환 → 최종 화면에 강남점 행만 남음. 자동 테스트는 없음(JS 화면 로직) — **자동화할 수 있는 구조인지, 빠진 await 지점이 없는지** 봐 달라 |
| P2 기초 재고 키 `opening-<id>` 충돌 500 | 키를 `"opening-" + uuid4().hex`로 변경 | `test_stores.test_opening_key_cannot_collide_with_user_keys` |
| P2 없는 매장 조회가 200 빈 결과 | `store_of(conn, query)`가 `inv.need_store`(이전 `_need_store`)로 검증 → 400 | `test_stores.ServerRoutes.test_flow` (items·pos·shortage·shortage/ai) |

## 특히 봐 줄 부분
0. **품목 수정** (`inventory.update_item`, `index.html`의 수정 버튼): 같은 매장 안 이름 중복 검사(자기 자신 제외), 단위는 의도적으로 수정 불가, `items.request`(추가 요청 지문)를 수정 뒤에도 유지해 추가 요청 재전송이 처음 결과를 돌려주는 동작이 맞는지, 이름·발주 기준 수정이 이전 부족 판단·권장 발주량 계산에 일관되게 반영되는지, 프롬프트 취소·잘못된 숫자 입력 처리, 수정 이력이 남지 않는 점이 문제인지.
1. **마이그레이션** (`inventory._migrate`, `_migrate_locked`)
   - 이전 `items`는 `name UNIQUE`라 매장별 같은 이름을 쓰려고 테이블을 다시 만든다(CREATE → INSERT SELECT → DROP → RENAME, `PRAGMA foreign_keys=OFF`).
   - 데이터 유실 가능성, 외래키가 새 `items`를 올바르게 가리키는지, 중간 실패 시 롤백, 연결이 동시에 열릴 때(`BEGIN IMMEDIATE` 후 재확인) 경쟁 상태.
   - 이전 DB 픽스처는 `test_stores.py`의 `OLD_SCHEMA`(HEAD의 스키마를 옮긴 것). 실제 이전 버전 DB와 다른 점이 있는지.
2. **매장 범위(scoping) 누락**: 매장 필터가 `list_items`, `list_pos`, `shortage_report`에만 있다. 품목 ID를 직접 받는 API(`/api/pos`, `/api/out`, `/api/history/<id>` 등)는 다른 매장 품목도 허용한다(의도: 화면이 선택 매장만 보여 줌). 이게 허용 가능한 설계인지, 다른 매장 품목으로 발주·사용 기록이 섞이는 경로가 있는지.
3. **멱등성**: `add_item`·`add_store`의 요청 키 재생(같은 키·같은 내용 → 같은 결과, 다른 내용 → 거부). `items.request` 지문에 `store_id`가 포함되는지, 기초 재고 기록 키(`opening-<id>`)가 충돌하지 않는지.
4. **서버 입력 검증**: `/api/items`의 `store_id`·이름 길이·타입, `?store=` 쿼리 파싱(`store_of`), URL에 쿼리가 붙은 정적 파일 요청(`urlsplit` 변경 영향).
5. **작업자(actor)**: 클라이언트가 작업자 없이 요청을 만들지 않는지(`client.js`의 `createSender`), 작업자 이름이 재전송 키·지문에 영향을 주는지(의도: 영향 없음), XSS(`esc` 처리) 누락 여부.
6. **AI 요약** (`summary.py`): `response_format` json_schema strict 사용이 맞는지, `finish_reason`·`refusal` 처리, 예외 계층(`AuthenticationError`/`RateLimitError`는 `APIStatusError` 하위) 순서, `max_completion_tokens=2000`이 추론 모델에서도 충분한지, 프롬프트 주입(품목 이름) 방어가 이전과 동일하게 유지되는지.
7. **WAL 재시도** (`_enable_wal`): 100회×0.05초 재시도가 적절한지, 무한 대기·오류 은폐 위험.
8. **화면** (`index.html`): 빈 매장(품목 0개)에서 이력·재고 화면 예외, 매장 전환 중 늦게 도착한 응답이 다른 매장 화면을 덮는 경우, `prompt()` 취소 처리.

## 알려진 한계 (지적 불필요 — 의도한 범위)
- 로그인·비밀번호 없음. 작업자는 입력한 이름을 그대로 기록(구분용, 보안 아님).
- 품목 삭제, 매장 삭제·이름 변경, 매장 간 이동, 품목 수정 이력 없음(수정은 이름·발주 기준만).
- 실제 OpenAI 서버 호출 미검증. 기본 모델명(`gpt-4o-mini`)은 임의 기본값이며 `OPENAI_MODEL`로 변경.
- 가정한 업무 규칙(일주일분 발주 등)은 `virtual-interview.md` 기준이며 실제 운영자 확인 전.

## 요청 형식
이전 검수와 같게, 지적마다 **재현 방법(가능하면 실패하는 테스트)** · **영향** · **제안**을 적어 주세요. 심각도(데이터 손상 > 잘못된 결과 > 사용성) 순이면 좋겠다.
