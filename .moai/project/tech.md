# ballast - Technology Stack

## Programming Languages

- **Python 3.11+** (단일 언어). 금융 계산은 `Decimal`로 정밀도 보장.

## Constitution (SDD 2025 Standard)

> 모든 SPEC과 구현이 반드시 지켜야 하는 프로젝트 DNA.

### Technology Stack Requirements

- Python `>=3.11`.
- 의존성/패키징: `uv` + `pyproject.toml`.
- 검증/모델: `pydantic` v2 (Config·InstrumentMeta·도메인 타입).
- HTTP: `httpx` (동기 우선, REST only — WebSocket 미사용).
- 수치/데이터: `pandas`, `numpy` (백테스트 전용. **코어 순수 함수는 표준 라이브러리 + Decimal만**).
- 테스트: `pytest`, `pytest-cov`, `hypothesis`(속성 기반 — 금융 불변식 검증 권장).
- 린트/포맷/타입: `ruff`(lint+format), `mypy`(strict).

### Naming Conventions

- 모듈/함수/변수: `snake_case`. 클래스/타입: `PascalCase`. 상수: `UPPER_SNAKE_CASE`.
- 전략 namespace 식별자: `vr`, `mab` (State Store 키·config 블록과 일치).
- 도메인 약어는 영어 유지: `Order`, `Decision`, `Market`, `State`, `pool`, `seed`, `target_pct`.
- 금액·수량 변수는 단위를 이름에 명시(`*_usd`, `*_qty`).

### Forbidden Libraries / Practices

- **코어(`core/`)에서 IO 금지**: 네트워크·파일·시계(`datetime.now`) 호출 금지 → 시각·가격은 인자로 주입.
- 금액·수량에 `float` 사용 금지 → 반드시 `Decimal`(반올림 소수점 둘째 자리, tick-size 준수).
- 전역 가변 상태·싱글톤 금지. 종목·배수 하드코딩 금지(반드시 instrument registry 경유).
- 라이브 경로에서 시장가 기본 사용 금지(현재가 ±3% 밴드 위험).

### Architectural Patterns

- 포트-어댑터(Hexagonal): 코어는 어댑터 인터페이스에만 의존, 구현체는 주입.
- 순수 함수 코어 = 백테스트/라이브 단일 코드 경로.
- 멱등 주문 제출(`requestId`/`X-Request-Id`), 상태는 자체 저장소에 영속(브로커에 의존하지 않음).
- 전략 격리 = **별도 계좌(`account_seq` 분리)** 가 v1 기본.

### Logging Standards

- 구조적 로깅(JSON 또는 key=value). 레벨: 의사결정/주문/체결 = INFO, 클램프·스킵 = WARNING, 인증실패·정지 = ERROR.
- 모든 주문에 `requestId` 로깅. **시크릿(CLIENT_SECRET·토큰) 절대 로깅 금지.**
- 라이브 의사결정은 입력(E·pool·avg·round)과 출력(Order)을 함께 남겨 백테스트와 대조 가능하게.

---

## Framework Choices

### pydantic v2
- Reason: Config·instrument 레지스트리·도메인 타입의 선언적 검증과 직렬화.
- Version: `>=2.6`
- Key Features: strict 타입, `Decimal` 지원, YAML 로딩과 결합.

### httpx
- Reason: 토스/KIS REST 호출(OAuth2 Bearer). WebSocket 불필요(종가/사이클 기반).
- Version: `>=0.27`
- Key Features: 동기/비동기, 타임아웃·재시도 친화, 프록시 지원.

### pytest (+ hypothesis)
- Reason: 순수 함수 코어의 결정적 검증 + 금융 불변식(보존·단조성)의 속성 기반 검증.
- Version: pytest `>=8`, hypothesis `>=6`
- Key Features: 파라메트라이즈, 픽스처, 속성 기반 반례 탐색.

# Quality Gates

## Required for Merge
- Test Coverage: ≥ 85% (코어는 사실상 100% 지향).
- Code Quality: `ruff` 0 errors, `mypy --strict` 0 errors (LSP run-phase: errors/type/lint = 0).
- Security: 시크릿 하드코딩·로깅 없음, 라이브 경로 가드(`dry_run`·`max_position_pct`·킬스위치) 존재.
- Documentation: SPEC↔TAG 추적성 유지, 정의 고정 항목(§5.4) 주석·테스트로 명문화.

## Enforcement Tools
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=src/ballast --cov-report=term-missing
```

## Failure Criteria
- LSP/타입/린트 error 1건 이상 → 머지 차단.
- 커버리지 < 85% → 차단(예외는 정당화 필요, 최대 5%).
- 코어에 IO·`float` 금액·종목 하드코딩 발견 → 차단.

# Security Policy

## Secret Management
- `CLIENT_ID`/`CLIENT_SECRET`/`access_token`은 **서버 환경변수만**. repo·로그·커밋 금지(`.gitignore` 적용).
- 토큰 수명 ~3600s 자동 갱신, 메모리 보관.

## Vulnerability Handling
- 의존성 취약점은 `uv`/`pip-audit`로 점검, 패치 우선.
- 인증 실패·비정상 응답 시 안전 정지 + 알림.

## Incident Response
- 킬스위치(전역 비활성)로 즉시 신규 주문 중단. 부분체결/네트워크 단절 시 멱등키로 중복 제출 차단.
- 사고 시 상태(State Store) 스냅샷 보존 후 수동 대조.

# Deployment Strategy

## Target Environments
- Development: 로컬(WSL) + 원격 ephemeral 컨테이너. 백테스트·dry-run 중심.
- Staging: 실계좌 1주 소액 통합 테스트(토스 샌드박스 없음).
- Production: 운영자 단일 서버(스케줄러 상주). DST-aware 트리거.

## Release Process
```yaml
steps:
  - lint_type_test: ruff + mypy + pytest (gate)
  - backtest_regression: 동일 Config 결과 재현 확인
  - dry_run: 의사결정→주문안 로깅 검증
  - live_smoke: 1주 소액 E2E (수동 승인)
```

## Rollback Procedure
- 코드: 직전 안정 커밋으로 revert. 라이브: 킬스위치 ON → 미체결 예약주문 취소 윈도우 처리.

## Environment Profiles
- `dry_run: true` 기본. 라이브 거래는 config에서 명시적으로 ON + 가드(`max_position_pct`) 통과 시에만.

---

*Last updated: 2026-06-26*
*Version: 0.1.0*
