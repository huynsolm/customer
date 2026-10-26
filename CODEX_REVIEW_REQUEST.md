# 11차 검수 요청 (Codex)

1~7차 검수는 끝났고 8·9차 지적은 수정했고 10차 지적 1건(샘플 초기화 원자성)도 수정했다(README "검증 구분" 표 참고). 5차 지적(AI 요약 응답의 매장 확인)도 반영·푸시됨(`6eb590b`). 이번 변경은 그 위에 얹은 것이므로 **이번 변경분 중심으로** 봐 달라.

**11차 범위**: 10차 검수에서 샘플 부활 방지(일반 경로)는 확인받았다. 이번에는 **10차 신규 지적 1건(샘플 초기화가 원자적이지 않음)의 수정**만 봐 달라.
작업 트리 기준이다(커밋 전): `git diff`로 `5ed872a` 이후 변경을 볼 수 있다.

## 이번 변경 요약
| 변경 | 파일 |
|---|---|
| **품목 완전 삭제** — 기록 없는(기초 재고만 있는) 품목만 삭제, 삭제 사실은 `item_deletions`에 기록, 요청 키로 멱등 | `inventory.delete_item`, `item_deletions` 테이블, `POST /api/items/delete`, `index.html`(행·보관 목록의 삭제 버튼) |
| 테스트 | `test_stores.py`(DeleteItem 클래스 5개 + 라우트) — 전체 91개 |

## 실행
```bash
python -m pip install -r requirements.txt   # openai (없어도 테스트 대부분 통과, RealSdk 테스트만 건너뜀)
python -m unittest -v                        # 96개
node --test test_client.mjs                  # 8개
```
실제 OpenAI 서버는 호출하지 않는다(API 키 없음). `RealSdk` 테스트는 진짜 `openai` SDK + `httpx.MockTransport`.

## 10차 수정 재확인
| 10차 지적 | 수정 | 확인 |
|---|---|---|
| P2 샘플 초기화가 원자적이지 않음(중간 실패 시 불완전한 구성이 영구 확정, 동시 시작 시 한쪽 실패) | `add_item`의 본문을 트랜잭션 안에서 쓰는 `_add_item_locked`로 분리. `inventory.seed_once`가 **하나의 `BEGIN IMMEDIATE`** 안에서 `seeded` 확인 → 비어 있는지 확인 → 샘플·기초 재고 추가 → 표시 저장. `server.seed()`는 이를 호출만 함. 쓰이지 않게 된 `get_meta`/`set_meta`는 삭제 | `test_stores.Seed.test_failure_midway_leaves_nothing_and_retry_gives_exactly_five`(두 번째 삽입에서 예외 → 품목 0개·표시 없음·연결이 트랜잭션 밖 → 재시도하면 정확히 5개), `test_concurrent_first_start`(스레드 4개가 동시에 `seed()` → 모두 성공·5개·`seeded=1`) |

## 특히 봐 줄 부분
1. **`_add_item_locked` 분리**: `add_item`의 동작(검증 순서, 멱등 재생, 삭제 기록 확인, 번호 할당, 기초 재고 기록)이 분리 전과 같은지. 들여쓰기만 바뀐 부분에서 빠진 줄이 없는지.
2. **`seed_once`**: 트랜잭션 안에서 예외가 나면 `tx.__exit__`이 ROLLBACK하는지(표시도 함께 롤백), 이미 표시가 있으면 `False`를 돌려주고 아무것도 안 하는지, 샘플 입력 검증을 트랜잭션 밖에서 하는 것이 맞는지.
3. **기존 DB**: 이 변경 전 DB(`meta` 테이블 없음)는 `connect()`가 만든 뒤 표시만 남기는지(품목·삭제 기록이 있을 때).

## 알려진 한계 (지적 불필요 — 의도한 범위)
- 로그인·비밀번호 없음. 작업자는 입력한 이름을 그대로 기록(구분용, 보안 아님).
- 기록 있는 품목의 삭제(보관만 가능), 삭제 복구, 매장 삭제, 매장 이름 변경 이력, 매장 간 이동. 품목 수정은 이름·발주 기준만.
- 보관된 품목의 변동 이력은 복원해야 화면에서 볼 수 있다(API로는 조회 가능).
- 실제 OpenAI 서버 호출 미검증. 기본 모델명(`gpt-4o-mini`)은 임의 기본값이며 `OPENAI_MODEL`로 변경.
- 가정한 업무 규칙(일주일분 발주 등)은 `virtual-interview.md` 기준이며 실제 운영자 확인 전.

## 요청 형식
이전 검수와 같게, 지적마다 **재현 방법(가능하면 실패하는 테스트)** · **영향** · **제안**을 적어 주세요. 심각도(데이터 손상 > 잘못된 결과 > 사용성) 순이면 좋겠다.
