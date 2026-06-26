# ballast — 세션 인계 문서 (HANDOFF)

> 목적: 새 세션(새 ephemeral 컨테이너)에서 이 프로젝트를 **끊김 없이 이어받기** 위한 단일 진입 문서.
> 작성 기준일: 2026-06-26 · 브랜치: `claude/lucid-volta-a42se7`
> 이 문서가 최신 상태와 다르면 `git log`와 `.moai/specs/`가 우선이다.

---

## 0. 30초 요약 / 재개 방법

- **무엇**: 레버리지 ETF 전략 봇 (VR 밸류 리밸런싱 + MAB 무한매수법). moai-adk SPEC-First DDD로 개발.
- **어디까지**: **P0(전략 코어 + 백테스트) ✅ + P1(토스 read-only 어댑터) ✅ + P2(Order Manager + dry-run) ✅ + P3(토스 write 어댑터, 실주문 백엔드) ✅ + 라이브 전 전략 정합성(STRATEGY-001) ✅ + State Store + Reconciliation(STATE-001) ✅ + Runner/타입드 Config 진입점(RUNNER-001) ✅**. 이제 dry-run→라이브 제출 경로·영속 상태 기반·"한 사이클" 조립 지점이 모두 존재한다.
- **다음 할 일**: **24/7 운영 경로** — (2) Scheduler(DST/장중 인지) → (3) Notifier → (4) Streamlit 대시보드 → (5) 배포 (아래 §3). Runner/Config 진입점(1)은 RUNNER-001로 **완료**. 또는 `/moai sync`로 문서화 먼저.
- **재개 한 줄**: 새 세션에서 이 브랜치를 받고 → §2로 환경 복구 → §3대로 24/7 경로 진행.

> ⚠️ 새 컨테이너는 ephemeral이다. `.moai/memory/`·`.venv/`는 **gitignore라 매번 사라진다(정상)**. 실제 작업물은 전부 git + PR #3에 있다. 손실 없음.

---

## 1. 현재 상태 (무엇이 끝났나)

### 로드맵

| 단계        | 내용                                                                                        | 상태      |
| ----------- | ------------------------------------------------------------------------------------------- | --------- |
| P0          | CORE (도메인 + instrument registry + 가드레일 + Config)                                     | ✅        |
| P0          | VR 전략 코어 (`next_value`/`rebalance_decision`/`order_from_decision` + `VRStrategy`)       | ✅        |
| P0          | MAB 전략 코어 (LOC 분할매수/익절/쿼터매도 + `MABStrategy`)                                  | ✅        |
| P0          | 백테스트 엔진 + 비용모델(양도세 22%) + 지표 + 베이스라인                                    | ✅        |
| P1          | 토스 어댑터 **read-only** (OAuth2 + 시세 + 계좌/자산 + 사전검증 + 체결조회)                 | ✅        |
| P1+         | 토스 **env 크리덴셜 부트스트랩** (`from_env` + `.env.example` + smoke + `py.typed`)         | ✅        |
| P0+         | BACKTEST-002 (파라미터 스윕 + walk-forward + 레짐분할 + 고원 평가)                          | ✅        |
| **P2**      | **Order Manager + dry-run** (의사결정→주문 변환·멱등·주문안 로깅)                           | ✅        |
| **P3**      | **토스 write 어댑터** (SPEC-ADAPTER-002, `BrokerOrderPort` 구현체 → 실주문 제출 백엔드)     | ✅        |
| **P-live**  | **라이브 전 전략 정합성** (SPEC-STRATEGY-001, MAB 쿼터매도 LOC 가격 + VR `V_n` 다주기 진화) | ✅        |
| **P-state** | **State Store + Reconciliation** (SPEC-STATE-001, 영속 상태 + 멱등 대조, 24/7 기반)         | ✅        |
| 24/7-(1)    | Runner/Config 진입점 (SPEC-RUNNER-001, 타입드 RunnerConfig + 리스 + `run_one_cycle`)        | ✅        |
| 다음        | **24/7 경로**: Scheduler(DST/장중 인지) → Notifier → Streamlit 대시보드 → 배포              | ⬜ ← 다음 |
| 보류        | 데이터 fetcher (yfinance/CSV 실데이터 주입)                                                 | ⬜        |

### 구현된 SPEC (11) — `.moai/specs/`

- **SPEC-CORE-001** — 공유 도메인 레이어 (순수, Decimal, IO 없음)
- **SPEC-VR-001** — VR 밸류 리밸런싱 순수함수
- **SPEC-MAB-001** — MAB 무한매수법 순수함수
- **SPEC-BACKTEST-001** — 백테스트 엔진/체결/비용/지표/베이스라인
- **SPEC-BACKTEST-002** — 파라미터 스윕 + walk-forward + 레짐분할(상승/하락/횡보) + 강건한 고원 평가
- **SPEC-ADAPTER-001** — 토스 read-only 어댑터 (실 OpenAPI `docs/reference/toss-openapi.json` v1.1.5 근거). PR #2 머지로 **env 크리덴셜 부트스트랩**(`from_env` + `.env.example` + `scripts/toss_smoke.py` + `py.typed`)이 추가됨 — 실 토스 Open API로 라이브 검증 완료.
- **SPEC-ORDER-001** — 브로커-중립 Order Manager + dry-run (의사결정→`OrderIntent` 변환·결정적 멱등키·주문안 기록). 패키지 `src/ballast/orders/`(models/ports/mapping/guards/manager/recording).
- **SPEC-ADAPTER-002 (P3)** — 토스 **write 어댑터** `TossOrderAdapter` (`src/ballast/adapters/toss/orders.py`). P2의 `BrokerOrderPort`(`place_order`/`cancel_order`)를 토스 `POST /api/v1/orders`·`/cancel` 위에 구현 — **dry-run 경로에 실제 라이브 제출 백엔드가 생김**. `OrderIntent`→토스 `OrderCreateRequest` 매핑, 응답/에러→`SubmissionResult`. 계약은 ORDER-001에서 그대로 재사용(포크 없음). mock-HTTP 검증만; 실 소크는 사용자 로컬.
- **SPEC-STRATEGY-001 (P-live)** — 라이브 전 전략 정합성. HANDOFF §4 두 follow-up을 **모두 해결**: (1) MAB 쿼터매도 LOC에 결정적 **주입 가격**을 부여(기존 `None`/MOC → 라이브-유효 priced LOC, 백테스트 종가체결은 보존), (2) VR `V_n`이 사이클마다 충실히 진화(엔진 `_Ledger.v_n` 갱신). `Strategy.plan_orders`가 이제 `PlanResult(orders, state_delta)`를 반환(`src/ballast/core/strategy.py`).
- **SPEC-STATE-001 (P-state)** — **State Store + Reconciliation** `src/ballast/state/`. 단일 포트(`StateStorePort`) 뒤에 in-memory + SQLite 백엔드; 순수·멱등 `reconcile`. 전략별 상태/주문 원장/config 스냅샷을 영속(쓰기 리스 + 낙관적 동시성). ADAPTER-002의 교차프로세스 취소 follow-up을 해결하고, **24/7 운영의 영속 기반**을 제공.
- **SPEC-RUNNER-001 (24/7-(1))** — **Runner / 타입드 Config 진입점** `src/ballast/app/{__init__,config,runner}.py`. 워커의 "한 사이클 구동" 코어이자 **모든 라이브 동작의 단일 조립 지점**. 타입드 `RunnerConfig`(ticker / account_capital / 전략별 배분 / `dry_run` / kill-switch)는 STATE-001의 `ConfigSnapshotRecord` 위에 씌운 렌즈(포크 없음) — seed-once 후 State-Store-authoritative. 단일-쓰기 리스를 시작 시 1회 획득해 사이클 간 유지(리스 소유자 = `WORKER_ID` env, `host:pid` 폴백). `run_one_cycle`이 `Strategy.plan_orders`→`OrderManager`→`BrokerOrderPort`(라이브 경로엔 `TossOrderAdapter`)를 시계(`cycle_key`)·장중시간(`Market`) 주입과 함께 조립; ORDER-001의 kill-switch / `max_position_pct` / dry-run 가드를 재사용하고, 결정적 `client_order_id` + 영속 원장 + 낙관적 동시성 상태 델타로 멱등·재진입(re-entrant)을 보장. 네 가지 설계 결정 **모두 옵션 A로 확정**: 사이클당 단일 전략+계좌(업계 관행 — Freqtrade 인스턴스당 단일 전략, NautilusTrader 격리 상태 액터), State-Store-authoritative config(seed-once), `WORKER_ID`+`host:pid` 리스 소유자, acquire-once-hold 리스 수명. 테스트 `tests/unit/app/`.

### 코드/품질 현황 (마지막 검증 시점)

- 소스: `src/ballast/{core,backtest,adapters,orders,state,app}/` · 테스트: `tests/unit/{core,backtest,adapters,orders,state,app}/`
- **테스트 519 passed, 커버리지 100%**, `ruff` 0, `ruff format` clean, `mypy --strict` 0 (44 source files)
- 마지막 커밋: `4c98fa6 feat(app): implement SPEC-RUNNER-001 Runner / typed Config entry point (TDD)` (직전: `48de9a2 docs(spec): add SPEC-RUNNER-001`, `f7363cb feat(state): STATE-001`)
- **Draft PR #3**: https://github.com/ccy5123/ballast/pull/3 (누적 트렁크. **PR #1·#2는 이제 CLOSED — #3로 대체됨**; 본문에 동일한 상태 요약 있음)

---

## 2. 환경 복구 (새 컨테이너에서 제일 먼저)

`.venv/`·`.moai/memory/`는 gitignore라 새 컨테이너엔 없다. `pyproject.toml`+`uv.lock`이 의존성을 전부 선언하므로 복원만 하면 된다.

```bash
cd /home/user/ballast            # 또는 클론 위치
# 의존성 설치 (코어+백테스트+어댑터 전부)
pip install -e ".[dev,backtest]"   # uv가 있으면 uv sync 도 가능
```

> 참고: 직전 세션에선 서브에이전트가 **system python**에 설치했다(.venv 아님). 어느 쪽이든 무방 — 게이트 실행 시 deps가 있는 인터프리터로 돌리면 된다.

### 품질 게이트 (다음 작업 시작 전 베이스라인 그린 확인)

```bash
ruff check .
ruff format --check .
mypy --strict src
python3 -m pytest --cov=src/ballast --cov-report=term-missing
# 기대: All checks passed / Success / 519 passed, 100% (현 시점 기준)
```

---

## 3. 다음 작업 — 24/7 운영 경로 (~~Runner~~ ✅ → Scheduler → Notifier → 대시보드 → 배포)

**현재 위치**: 전략 코어·백테스트·토스 read/write 어댑터·dry-run-first Order Manager·State Store + Reconciliation·Runner/타입드 Config 진입점(RUNNER-001)이 모두 있다. 즉 **순수 코어·IO 경계 부품·"한 사이클" 단일 조립 지점까지 다 갖췄다**. 남은 건 이 부품들을 **무인(unattended) 24/7 워커**로 엮고(이제 Runner를 _언제_ 돌릴지가 다음 과제), 그 워커를 관측·제어할 **대시보드**를 붙이고, **배포**하는 일이다.

> **핵심 아키텍처 원칙**: **워커가 항상 켜져 있는 엔진이고, Streamlit은 대시보드일 뿐이다 — 둘은 State Store를 통해 분리(decoupled)된다.** Streamlit 자체는 무인 백그라운드 실행을 보장하지 못한다(요청 단위로 깨어나는 런타임). 따라서 스케줄·주문 제출은 **반드시 별도의 always-on 워커**가 책임지고, Streamlit은 State Store를 읽고(상태/원장/체결) 쓰는(config/kill-switch) **워커의 read/write 동료(peer)** 역할만 한다.

다음 순서와 근거:

### (1) Runner / Config 진입점 — **DONE ✅ (SPEC-RUNNER-001)**

- **타입드 config ✅**: ticker + 계좌 자본(account capital) + 전략별 배분(per-strategy allocation) + `dry_run` + **kill-switch**. STATE-001 `ConfigSnapshotRecord` 위의 렌즈(포크 없음) — seed-once 후 State-Store-authoritative.
- **State Store와 연동 ✅**: config 스냅샷을 읽고/쓰며(STATE-001의 `ConfigSnapshotRecord`), **writer 리스(lease)를 시작 시 1회 획득**해 사이클 간 유지(단일 쓰기 보장; 소유자 = `WORKER_ID` env, `host:pid` 폴백)한 뒤 `run_one_cycle`로 한 사이클을 구동한다(strategy core → Order Manager → 토스 어댑터; 시계 `cycle_key`·장중시간 `Market` 주입, 결정적 `client_order_id`로 멱등·재진입).
- _결과_: 모든 라이브 동작의 단일 조립 지점이 생겼다 → 이제 스케줄러·대시보드가 붙을 대상이 존재한다.

### (2) Scheduler — _가장 먼저 / 다음_

- **DST/장중 인지(market-hours-aware) 트리거**: MAB 매일 / VR 사이클 / **LOC는 종가에 맞춰 타이밍**. 미국장 + 서머타임 전환을 정확히 다뤄야 한다.
- _왜 다음_: Runner가 이제 “한 사이클”을 알므로, 스케줄러는 “언제 그 사이클을 돌릴지”를 안다.

### (3) Notifier (ntfy / Discord)

- **무인 안전(unattended-safety) 알림**: 체결(fill)·에러·kill-switch 발동 시 알림. 사람이 안 보고 있을 때 바로 알아채는 안전망.
- _왜 셋째_: 라이브 제출이 도는 순간부터 관측 가능성이 안전의 필수 조건이 된다.

### (4) Streamlit 대시보드

- State Store 위의 **컨트롤 패널**: 상태/원장/체결을 읽고, config/kill-switch를 쓴다 — 워커의 read/write 동료.
- _왜 넷째_: 워커가 먼저 돌아야 보여줄 상태가 생긴다. 대시보드는 엔진이 아니라 창(窓)이다.

### (5) 배포

- **Railway always-on 워커 + 영속 볼륨**(SQLite용) — 또는 대시보드를 워커와 다른 호스트에 둘 경우 **Neon/Supabase Postgres**.
- 대시보드는 **Streamlit Cloud** 또는 워커와 **co-host**. 시크릿은 플랫폼 env var로.
- **SQLite vs Postgres 선택의 분기점**: _대시보드가 워커와 같은 호스트에 있나?_ 같이 있으면 볼륨 위 SQLite로 충분(가장 단순), 떨어져 있으면 매니지드 Postgres. **State Store 포트(STATE-001) 덕분에 이 교체는 저렴하다** — 백엔드만 갈아끼우면 된다.

### 라이브 전 점검표 (go-live 직전, 간단판)

1. 클라우드에서 **`dry_run` 소크**부터 — 실제 스케줄로 며칠 무인 구동.
2. **Notifier/대시보드로 관측** — 체결/에러/상태 전이가 기대대로 흐르는지 확인.
3. **아주 작은 라이브 배분**으로 전환 — 1주 단위 실주문부터.
4. 안전망은 **kill-switch + `max_position_pct`** (이미 P2 가드에 있음). 이상 시 kill-switch로 즉시 정지.

### 진행 방식 (moai)

1. `manager-spec` 위임 → 각 단계마다 `.moai/specs/SPEC-XXX/{spec,plan,acceptance}.md` (영어, EARS, frontmatter)
2. 검토 후 `manager-ddd` 위임 → TDD(RED→GREEN→REFACTOR). 시각/시장시간은 인자 주입으로 순수성 유지, 외부 IO는 mock.
3. 게이트 재검증 → 커밋 → 푸시 (PR #3에 반영) → 클라우드 dry-run 소크·라이브 소액 검증은 사용자 로컬/클라우드

---

## 4. 라이브 전 정리할 follow-up 2건 — **둘 다 SPEC-STRATEGY-001로 RESOLVED ✅**

> 아래 두 건은 **SPEC-STRATEGY-001(P-live)** 에서 모두 해결됨. 이력 보존을 위해 삭제하지 않고 해결 내용만 표기한다.

- ✅ **VR `V_n` 다주기 미진화** → **RESOLVED**: `Strategy.plan_orders`가 이제 `PlanResult(orders, state_delta)`를 반환한다. VR이 재계산된 `V_n`을 `state_delta`로 노출하고, 백테스트 엔진(`_Ledger.v_n`)이 사이클마다 이를 반영 → **다주기 VR 재생이 충실해짐**. MAB는 그대로 무관(상태가 체결에서 파생, 빈 델타).
- ✅ **MAB 쿼터매도 LOC 가격** → **RESOLVED**: `mab_on_seed_exhausted`의 쿼터매도 LOC에 **결정적 주입 가격**을 부여 → 라이브 토스 경로에서 유효한 priced LOC(`LIMIT`+`CLS`)가 됨. 백테스트의 종가 체결 거동은 보존.

---

## 5. 보류 백로그

> **State Store + Reconciliation는 이제 DONE**(SPEC-STATE-001). 남은 백로그는 §3의 **24/7 경로**와 데이터 fetcher다.

- **24/7 경로 (다음 작업, 상세는 §3)** — ✅ ~~(1) Runner/Config 진입점~~ **DONE (SPEC-RUNNER-001)** → **(2) Scheduler(DST/장중 인지) ← 다음** → (3) Notifier(ntfy/Discord) → (4) Streamlit 대시보드 → (5) 배포(Railway always-on 워커 + 볼륨/Postgres). **워커=엔진, Streamlit=대시보드, State Store로 분리**.
- ✅ ~~**State Store**~~ — **DONE** (SPEC-STATE-001): 전략별 namespace 상태 + 주문 원장 + config 스냅샷 영속(in-memory + SQLite, 단일 포트).
- ✅ ~~**Reconciliation**~~ — **DONE** (SPEC-STATE-001): 체결 대조 → 상태 갱신, 순수·멱등 `reconcile`.
- **데이터 fetcher** — yfinance/CSV 어댑터로 실데이터 OHLC+FX 주입 → 실제 비교표(CAGR/MDD/세금드래그) 산출.

---

## 6. moai 워크플로우로 재개하는 법

- 이 브랜치를 클론하면 `.claude/`(스킬·에이전트)·`CLAUDE.md`가 자동 로드된다.
- 권한: `.claude/settings.local.json`에 `defaultMode: acceptEdits` 설정됨(편집 자동승인; 위험 bash는 계속 확인).
- 모드: moai personal / git `manual`(현 브랜치에 직접 커밋, 강제 PR 없음).
- 흐름: `/moai plan "..."`(manager-spec) → 검토 → `/moai run SPEC-XXX`(manager-ddd, TDD) → `/moai sync`(manager-docs).
- 게이트: 모든 구현은 `ruff` 0 · `mypy --strict` 0 · 커버리지 ≥85%(코어는 사실상 100%).

---

## 7. 핵심 파일/레퍼런스 위치

- 프로젝트 DNA: `.moai/project/{product,structure,tech}.md` (tech.md에 Constitution)
- SPEC: `.moai/specs/SPEC-{CORE,VR,MAB}-001/`, `SPEC-BACKTEST-{001,002}/`, `SPEC-ADAPTER-{001,002}/`, `SPEC-ORDER-001/`, `SPEC-STRATEGY-001/`, `SPEC-STATE-001/`, `SPEC-RUNNER-001/`
- 소스: `src/ballast/core/` (순수; `strategy.py`에 `PlanResult`/`Strategy`), `src/ballast/backtest/` (pandas/numpy 허용), `src/ballast/adapters/` (IO 경계; `toss/orders.py`에 write 어댑터), `src/ballast/orders/` (Order Manager, dry-run-first), `src/ballast/state/` (State Store + Reconciliation; in-memory + SQLite, 순수 `reconcile`), `src/ballast/app/` (Runner + 타입드 RunnerConfig; `run_one_cycle` 단일 조립 지점 + 리스)
- **토스 API 진실의 원천**: `docs/reference/toss-openapi.json` (OpenAPI 3.1.0 v1.1.5)
- 설정: `.moai/config/sections/*.yaml`, `pyproject.toml`(deps·ruff·mypy·pytest)

---

## 8. 결정·관례 메모 (Constitution 요약)

- 코어(`src/ballast/core/`)는 **순수**: IO·네트워크·시계(`datetime.now`) 금지 → 시각·가격은 인자로 주입.
- 금액·수량은 **`Decimal`만**(절대 `float` 금지), 반올림 소수점 2자리. 백테스트의 곡선/비율 계산만 numpy/float 허용(머니 장부와 분리).
- 종목·레버리지 하드코딩 금지 → instrument registry 경유. 비레버리지/1x는 가드레일이 경고/차단.
- 전략 격리 = **별도 계좌**(`account_seq`)가 v1 기본.
- 시크릿(`TOSS_CLIENT_ID/SECRET`)은 **env var만**, 로그·repr 마스킹. 토큰은 메모리만.
- **실계좌 검증(1주 조회/주문)은 사용자 로컬에서** 수행 — 원격 컨테이너엔 키 없음, 샌드박스 없음.

---

_이 문서는 인계용 스냅샷이다. 작업이 진행되면 갱신하거나, PR #3 본문/`.moai/specs/`를 최신 출처로 삼을 것._
