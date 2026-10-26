# 7차 검수 요청 (Codex)

1~6차 검수 지적은 모두 반영되어 있다(README "검증 구분" 표 참고). 5차 지적(AI 요약 응답의 매장 확인)도 반영·푸시됨(`6eb590b`). 이번 변경은 그 위에 얹은 것이므로 **이번 변경분 중심으로** 봐 달라.

**7차 범위**: 6차 지적 2건의 수정 확인(아래 표) + 보관·매장 이름 변경·수정 이력 변경분 전체. 이전 범위: `6eb590b`(푸시됨) 이후의 변경 — ① 품목 수정 이력(`item_changes`), ② **품목 보관·복원**, ③ **매장 이름 변경**. 앞선 변경은 이미 검수했으니 위 세 가지를 중심으로 봐 달라.
작업 트리 기준이다(커밋 전): `git diff`로 `6eb590b` 이후 변경을 볼 수 있다.

## 이번 변경 요약
| 변경 | 파일 |
|---|---|
| 품목 수정 이력 — 이름·발주 기준이 바뀔 때 이전·이후·작업자를 `item_changes`에 기록 | `inventory.update_item`, `item_changes`, `GET /api/item-changes/<id>`, `index.html` |
| **품목 보관·복원**(삭제 대신 숨김, `items.archived`) | `inventory.set_archived`, `list_items(archived=)`, `_need_active_item`, `POST /api/items/archive`, `GET /api/items?archived=1`, `index.html` |
| **매장 이름 변경** | `inventory.update_store`, `POST /api/stores/rename`, `index.html` |
| DB 이전: `archived` 컬럼 추가(매장은 있고 보관은 없던 v0.3 DB 포함) | `inventory._needs_migration`, `_migrate_locked` |
| 테스트 | `test_stores.py`(Archive 클래스·이름 변경·v0.3 이전·라우트 추가) — 전체 83개 |

## 실행
```bash
python -m pip install -r requirements.txt   # openai (없어도 테스트 대부분 통과, RealSdk 테스트만 건너뜀)
python -m unittest -v                        # 83개
node --test test_client.mjs                  # 8개
```
실제 OpenAI 서버는 호출하지 않는다(API 키 없음). `RealSdk` 테스트는 진짜 `openai` SDK + `httpx.MockTransport`.

## 6차 수정 재확인
| 6차 지적 | 수정 | 확인 |
|---|---|---|
| P1 보관 후 정정으로 재고가 생겨 품목이 숨겨진 채 남음 | `correct()`가 보관된 품목의 기록 정정을 거부(복원 후 가능) | `test_stores.Archive.test_correction_blocked_while_archived`(지적 재현 시나리오 그대로) |
| P2 매장 전환 직후 이전 화면의 보관·복원 버튼으로 다른 매장 품목 변경 | 매장 전환·추가 시 `#view`를 즉시 비움(`loading()`) + 화면 안 모든 버튼을 `actIn(st, …)`로 감싸 그린 매장이 현재 매장일 때만 동작 | 브라우저에서 새 매장 조회를 지연시킨 채 옛 버튼 클릭 → 화면이 즉시 "불러오는 중…"이고 본점 품목은 그대로(다만 재고 있는 품목이라 서버도 거부하는 경우여서 이 확인만으로는 가드를 구분하지 못함 — **`actIn` 가드가 실제로 모든 핸들러를 덮는지 코드로 봐 달라**) |

## 특히 봐 줄 부분
1. **보관 규칙** (`set_archived`): 재고 0·입고 예정 0일 때만 보관 가능. 이 검사가 트랜잭션 안에서 이뤄져 보관 직전에 끼어드는 입고·발주(동시 요청)를 막는지, `get_po`로 잔량을 보는 방식이 취소·정정된 발주에서도 맞는지.
2. **보관된 품목이 새어 나가는 경로**: `create_po`·`stocktake`는 `_need_active_item`으로 막았다. 그 밖(`record_out`, `receive`, `correct`, `update_item`, `cancel_remaining`, 이력·수정 이력 조회)에서 보관 품목이 어떻게 동작하는지, 막아야 하는데 열려 있는 곳이 있는지. (의도: 재고가 0이고 입고 예정이 없으므로 사용·입고는 실질적으로 불가, 정정·수정·조회는 허용)
3. **이름 유일성**: `UNIQUE(store_id, name)`이 보관된 품목도 포함한다(같은 이름의 새 품목은 만들 수 없고 복원하라는 메시지). 복원 시 중복 검사(`set_archived`의 복원 분기)가 사실상 방어용인데 필요한지·틀린 곳은 없는지.
4. **DB 이전**: `archived` 컬럼을 `ALTER TABLE ... ADD COLUMN ... CHECK`로 추가하는 경로와, 이전 `items` 재구성 경로(CREATE TABLE에 `archived` 포함)가 모두 올바른지. 동시에 여러 연결이 열릴 때(`BEGIN IMMEDIATE` 재확인). `test_v03_db_without_archived_column`이 실제 v0.3 DB를 충분히 흉내 내는지.
5. **수정 이력** (`item_changes`): `field` CHECK 제약에 `archived` 포함(이 테이블은 이번에 처음 만들어져 기존 DB에 영향 없음), 같은 값 재전송 시 이력이 안 남는지, 보관·복원 이력의 `old_value/new_value`("0"/"1") 표현.
6. **매장 이름 변경** (`update_store`): 이름 중복(자기 자신 제외)·검증, 이력을 남기지 않는 것이 허용 가능한지, 이름이 바뀐 뒤 화면의 매장 선택기·저장된 선택값 동작.
7. **화면** (`index.html`): 보관 `confirm`·복원 흐름, 보관 직후 목록 갱신, 보관된 품목이 변동 이력 화면의 품목 선택기에 없어 그 이력을 볼 수 없는 점(복원해야 보임 — 의도한 한계), 매장 전환 중 지연 응답 처리가 새 보관 조회(`Promise.all`)에도 유지되는지.

## 알려진 한계 (지적 불필요 — 의도한 범위)
- 로그인·비밀번호 없음. 작업자는 입력한 이름을 그대로 기록(구분용, 보안 아님).
- 품목 완전 삭제(보관만 가능), 매장 삭제, 매장 이름 변경 이력, 매장 간 이동. 품목 수정은 이름·발주 기준만.
- 보관된 품목의 변동 이력은 복원해야 화면에서 볼 수 있다(API로는 조회 가능).
- 실제 OpenAI 서버 호출 미검증. 기본 모델명(`gpt-4o-mini`)은 임의 기본값이며 `OPENAI_MODEL`로 변경.
- 가정한 업무 규칙(일주일분 발주 등)은 `virtual-interview.md` 기준이며 실제 운영자 확인 전.

## 요청 형식
이전 검수와 같게, 지적마다 **재현 방법(가능하면 실패하는 테스트)** · **영향** · **제안**을 적어 주세요. 심각도(데이터 손상 > 잘못된 결과 > 사용성) 순이면 좋겠다.
