---
id: happicare-comparison
title: HappiCare 두 가지 바이브코딩 방식 비교
sidebar_label: HappiCare 방식 비교
description: happicare(React 전환)와 happicare_front2(통합 문의 관리 시스템)를 기획자·웹퍼블리셔·개발자 관점에서 비교한 팀 공유 자료
tags: [happicare, 바이브코딩, react, nestjs, 팀공유]
---

# HappiCare 두 가지 바이브코딩 방식 비교

> 같은 HappiCare 문의 서비스를 **서로 다른 방식**으로 AI와 함께 만든 두 저장소를 비교합니다.
> 기준 시점은 2026-10-25이며, 각 저장소의 기본 브랜치를 읽고 정리했습니다. 앱을 직접 실행하거나 빌드해 보지는 않았습니다.

## 1. 한 장 요약

| 구분 | `happicare` | `happicare_front2` |
|---|---|---|
| 한 줄 정의 | 기존 퍼블리싱 HTML을 React로 옮긴 **고객 화면 프로토타입** | 문의 접수부터 답변까지 만든 **풀스택 문의 관리 시스템** |
| 출발점 | 이미 있는 HTML 소스(`public/legacy`) | 기획 문서 3종(`Concept.md`, `Design.md`, `ToDo.md`)과 화면 시안 PNG |
| 범위 | 고객 화면 3개 경로 | 병원 접수 화면 + 관리자 웹 3개 탭 + API + DB + 배포 |
| 구조 | 프론트 단일 앱 | 모노레포(`apps/web`, `apps/api`, `packages`) |
| 데이터 | 브라우저 `sessionStorage` mock | MariaDB + Prisma, 파일은 S3 호환 저장소 |
| 작업 기간 | 2026-08-25 ~ 2026-10-13, 커밋 12개 | 2026-09-30 ~ 2026-10-11, 커밋 293개 |
| 코드 규모 | JS 약 7천 줄, 파일 약 100개 | JS/TS 약 5만 1천 줄, 파일 약 640개 |
| 테스트 파일 | 14개 | 277개 |

:::tip 핵심 메시지
`happicare`는 **"있는 것을 옮기는"** 방식, `happicare_front2`는 **"문서로 정의하고 단계별로 쌓는"** 방식입니다.
어느 쪽이 더 낫다기보다 **목적이 다릅니다.** 화면 이식과 학습에는 앞쪽이, 실제 서비스를 만드는 데는 뒤쪽이 맞습니다.
:::

## 2. 두 저장소는 무엇인가

### 2.1 `happicare` — React 전환 프로토타입

- 기존 HappiCare 고객지원 포털(HTML/JS)을 **React 19 + Vite 7**로 전환했습니다.
- 경로는 `/support`(고객지원 센터), `/inquiries/new`(새 문의), `/inquiries/:id`(문의 상세) 세 개입니다.
- 1:1 문의 작성을 4단계 위저드(문의 유형 → 상세 입력 → 연락처·원격 지원 → 최종 확인)로 나누었습니다.
- 셀프 해결 아코디언, 탭형 상세 가이드, 이미지 확대, FAQ 광고 표기 가이드를 복원했습니다.
- 저장은 `sessionStorage`가 기본이고, `http` 모드로 바꾸면 `docs/API_CONTRACT.md`에 정의한 API를 호출하도록 **준비만** 되어 있습니다.
- 발표 대본, 팀 보고서, React 학습 가이드, Git 가이드, 작업 요청 프롬프트 모음 같은 **사람을 위한 문서**가 많습니다.

### 2.2 `happicare_front2` — 통합 문의 관리 시스템

- 병원 담당자가 문의를 접수하고, 고객지원 담당자가 배정·답변하며, 관리자가 공지와 담당자를 관리하는 **서비스 전체**입니다.
- 관리자 웹은 `문의 내역 관리`, `공지사항 관리`, `설정` 세 탭으로 구성됩니다.
- 문의 상태는 `확인 전 → 확인 중 → 답변 완료`(DB 값 `PENDING`, `IN_PROGRESS`, `DONE`)로 관리합니다.
- 백엔드는 NestJS이고, 인증·세션, 첨부파일, 이메일 알림, 감사 로그, 상태 확인(health) 엔드포인트가 있습니다.
- DB 모델이 22개이고(문의, 답변, 내부 메모, 담당자 배정 이력, 상태 이력, 자주 쓰는 답변, 공지, 알림, 첨부 등), 마이그레이션이 14개입니다.
- 운영 배포용 Caddy 설정, `docker-compose`, 운영 CLI(`ops`), 운영 절차서가 있습니다.

## 3. 어떻게 달라졌나

### 3.1 작업 방식의 차이

| | `happicare` | `happicare_front2` |
|---|---|---|
| 기준 문서 | 원본 HTML과 소스 폴더 | `Concept.md`(무엇을), `Design.md`(어떻게 보이게), `ToDo.md`(어떤 순서로) |
| AI에게 주는 요청 | "원본과 React를 비교해서 다른 점을 우선순위별로 알려줘" 같은 **검수·수정 반복** | 단계별 **설계서(spec) → 구현 계획(plan) → 구현 → 검증** |
| 단계 관리 | 계획서 1개, 설계서 2개 | phase 1~12D까지 설계서와 계획서가 쌍으로 존재(약 16쌍) |
| 변경 단위 | 소수의 큰 커밋 | PR 단위의 작은 작업 브랜치(예: `worktree-phase12d-ops`) |
| 문서의 독자 | 팀원, 발표 청중, 학습자 | 다음 작업을 이어받는 사람과 AI, 운영자 |

### 3.2 기술 스택 비교

| 영역 | `happicare` | `happicare_front2` |
|---|---|---|
| 프론트 | React 19, Vite 7, JSX | React 19, Vite 8, React Router 7, Tiptap(리치 에디터), CSS Modules |
| 백엔드 | 없음 | Node 22, NestJS, Prisma |
| DB·저장소 | 없음(브라우저 저장) | MariaDB, Garage(S3 호환) |
| 테스트 | Node 내장 테스트, Playwright e2e(PC·모바일) | Vitest, Testing Library, NestJS 테스트, Playwright, axe 접근성 검사 |
| 품질 도구 | `npm run check`(문법 검사) | ESLint, Prettier, GitHub Actions CI |
| 배포 | 없음 | Docker Compose, Caddy, systemd, 운영 CLI |

### 3.3 완성도

| 항목 | `happicare` | `happicare_front2` |
|---|---|---|
| 화면 | 고객 화면 구현 | 관리자 17개 상태 + 병원 12개 상태, 총 29개 상태를 점검 |
| 인증 | 없음 | 세션 기반, 관리자 권한 구분(`SUPPORT`, `ADMIN`) |
| 파일 첨부 | 파일 이름과 크기만 저장(업로드 미구현) | 문의·답변·내부 메모·공지 첨부 및 정리 작업 |
| 알림 | 없음 | 문의 접수와 답변 등록 시 이메일(개발은 Mailpit) |
| 운영 | 해당 없음 | 런북, 백업, 점검 명령 |
| 남은 일 | 백엔드, 인증, 실제 업로드 | 단계 0(CRM 연동 계약, 개인정보 정책)과 첫 실서버 배포 |

:::caution 알아 둘 점
- `happicare_front2`의 README는 "단계 3까지 진행"이라고 적혀 있지만, `ToDo.md`와 `docs/superpowers`를 보면 실제로는 12D까지 구현되어 있습니다. README가 오래된 내용입니다.
- `happicare_front2`는 **단계 0이 확정되기 전에는 운영 환경에서 실제 사용자가 로그인할 수 없습니다.** CRM 토큰 검증이 개발용 검증기로 대체되어 있기 때문입니다(`docs/ops/runbook.md` 첫 줄).
:::

## 4. 직군별로 알아 둘 점

### 4.1 기획자

**볼 것**

- `happicare_front2/Concept.md`: 서비스 목적, 사용자 3종(병원 담당자, 고객지원 담당자, 관리자), 화면 구성.
- `happicare_front2/ToDo.md` 단계 0: 개발 전에 확정해야 할 정책 목록.
- `happicare_front2/design/`: 화면 시안 PNG 약 35장.

**배운 점**

- 문서를 먼저 쓰면 AI가 만드는 결과의 방향이 흔들리지 않습니다. `happicare_front2`는 기획서가 곧 구현의 기준입니다.
- `happicare`는 원본 HTML이 기준이라, 원본에 없던 요구(담당자 배정, 답변 관리 등)는 다루지 못합니다.

**결정이 필요한 항목** (아직 정해지지 않아 개발이 기다리고 있습니다)

1. CRM에서 문의 페이지로 넘어올 때의 자동 로그인 방식
2. 관리자 인증과 역할별 권한
3. 문의 유형별 필수 입력 항목
4. 첨부파일과 개인정보 정책(보관·파기 기간, 바이러스 검사 등)
5. 이메일 발송 서비스, 문자 발송 시점
6. 메디세이 관리자 계정 동기화 방식

### 4.2 웹퍼블리셔

**볼 것**

- `happicare_front2/apps/web/src/styles/tokens.css`: 색, 글꼴, 간격이 CSS 변수로 모여 있습니다.
- `happicare_front2/docs/quality/12c-screen-audit.md`: 화면 품질 점검 보고서.
- `happicare/public/legacy`: 전환 전 원본 퍼블리싱 결과물.

**배운 점**

- `happicare`는 스타일이 `src/styles/app.css` 한 파일(약 970줄)입니다. 원본 HTML의 모양을 유지하기 쉽지만 규모가 커지면 관리가 어렵습니다.
- `happicare_front2`는 **디자인 토큰 + CSS Modules**입니다. 브랜드 색(`--color-primary: #00B80C`), 상태 색(확인 전 보라, 확인 중 주황, 답변 완료 초록), 글꼴(Pretendard)이 변수로 정의되어 있어 한 곳을 고치면 전체가 바뀝니다.
- 접근성 자동 검사(axe)와 키보드 이동, 반응형 검사가 테스트로 들어가 있습니다.

**퍼블리셔가 결정해야 할 항목** (12C 점검 보고서의 열린 항목)

| 번호 | 내용 |
|---|---|
| D1 | 색 대비 기준(WCAG AA 4.5:1)에 못 미치는 색 10쌍이 예외로 남아 있습니다. 기본 버튼의 흰 글자(2.67:1)와 회색 안내 글자(2.87:1)가 가장 급합니다. 브랜드 원본 색이 확정되면 한꺼번에 조정합니다. |
| D2 | 문의 상세에서 Tab 키로 "내부 관리" 패널에 닿는 순서가 본문 뒤입니다. 화면 위치를 유지한 채 문서 순서만 앞당길지 결정해야 합니다. |
| D3 | 설정 화면에서 담당자가 많아지면 저장 버튼이 화면 밖으로 밀립니다. 시안 메모대로 목록 영역만 스크롤되게 해야 합니다. |

병원 쪽 화면 12개 상태는 시안이 없어서 `Design.md` 규칙으로 점검했습니다. 시안 보강이 필요합니다.

### 4.3 개발자

**볼 것**

- 구조: `happicare_front2/apps/api/src/domain/*`(admin-settings, attachments, audit, hospital-inquiries, inquiries, notices, notifications), `apps/web/src/features/*`.
- DB: `happicare_front2/prisma/schema.prisma`.
- 운영: `docs/ops/runbook.md`, `ops/ops.mjs`, `deploy/`.
- API 계약 초안: `happicare/docs/API_CONTRACT.md`.

**배운 점**

- `happicare`의 서비스 계층(`inquiryDataSource.js`, `inquiryApiMapper.js`, `inquiryRepository.js`)은 저장소를 갈아 끼울 수 있게 나뉘어 있습니다. 백엔드 연동 준비의 좋은 예입니다.
- `happicare_front2`는 **모듈형 모놀리스**입니다. 서비스를 쪼개지 않고 기능별 모듈 경계만 지킵니다.
- 상태 전이, 담당자 변경, 감사 로그, 알림 대기열을 서버가 책임집니다.
- 코드량 대비 테스트가 많습니다. 웹 테스트 파일 64개, API spec 104개, e2e와 배포 검증 스크립트가 별도로 있습니다.
- CI는 웹과 API를 나누어 lint, test, build를 돌립니다.

**기술 부채와 위험**

- 웹이 TypeScript가 아니라 JSX입니다. `ToDo.md`의 계획(React + TypeScript)과 다릅니다.
- 첨부파일 운영 항목(정책 승인, 바이러스 검사, 보관·파기, 다운로드 감사 로그, 운영 키 권한 분리)이 `docs/api/README.md`에 미결로 남아 있습니다.
- 두 저장소의 API 규격이 서로 다릅니다. `happicare`는 `/api` 아래의 `data`/`meta`/`error` 응답 포맷을 가정하고, `happicare_front2`는 자체 구현을 따릅니다. 합칠 때 맞춰야 합니다.

## 5. 어떤 방식을 언제 쓸까

| 상황 | 추천 |
|---|---|
| 이미 있는 화면을 React로 옮기고 싶다 | `happicare` 방식: 원본을 기준으로 두고, 비교 검수를 반복 |
| 새 서비스를 처음부터 만든다 | `happicare_front2` 방식: 문서 → 설계 → 계획 → 구현 → 검증 |
| 팀원에게 개념을 가르치고 싶다 | `happicare`의 학습 가이드와 발표 자료 구성을 참고 |
| 운영까지 고려한다 | `happicare_front2`의 CI, 런북, 점검 스크립트 구성을 참고 |
| 둘을 합친다 | 고객 화면의 UX(위저드, 셀프 해결 가이드)는 `happicare`에서, 데이터와 업무 로직은 `happicare_front2`에서 가져오는 방향 |

## 6. 합친다면 (제안)

1. `happicare`의 셀프 해결 가이드, FAQ, 4단계 문의 위저드를 `happicare_front2`의 병원 화면으로 이식합니다.
2. `happicare/docs/API_CONTRACT.md`와 `happicare_front2`의 실제 API 응답 형식을 비교해 한쪽으로 맞춥니다.
3. 두 저장소의 문의 유형 정의(`wizardSchemas.js` 대 `InquiryCategory` 모델)를 하나로 통합합니다.
4. 단계 0 정책이 확정되면 CRM 연동과 첫 배포를 진행합니다.

## 7. 팀에 요청할 결정

- **기획**: 단계 0의 여섯 가지 정책 확정 일정.
- **퍼블리싱**: 브랜드 원본 색 확정(D1), 병원 화면 시안 보강.
- **개발**: 두 저장소를 합칠지 별도로 유지할지, 웹을 TypeScript로 옮길지.

## 8. 참고 위치

| 자료 | 위치 |
|---|---|
| 서비스 컨셉 | `happicare_front2/Concept.md` |
| UI/UX 기준 | `happicare_front2/Design.md` |
| 구현 순서 | `happicare_front2/ToDo.md` |
| 단계별 설계·계획 | `happicare_front2/docs/superpowers/` |
| 운영 절차서 | `happicare_front2/docs/ops/runbook.md` |
| 화면 품질 점검 | `happicare_front2/docs/quality/12c-screen-audit.md` |
| API 계약 초안 | `happicare/docs/API_CONTRACT.md` |
| 원본 대응표 | `happicare/docs/inquiry-original-mapping.md` |
| 기존 팀 보고서 | `happicare/docs/happicare-migration-team-report.md` |
