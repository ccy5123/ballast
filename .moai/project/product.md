# ballast - Mission & Strategy

> 본 문서는 전략 구현용 기술 기반 문서이며 투자 자문이 아니다.
> 레버리지 ETF는 테일 리스크가 크며, 전략은 위험을 완화할 뿐 제거하지 못한다.

## What problem do we solve?

라오어의 두 가지 투자 전략 — **VR(밸류 리밸런싱, 장기)** 과 **MAB(무한매수법, 단기/중기)** — 을
**임의의 레버리지 ETF**에 대해 규칙대로 집행하는 자동화 봇을 만든다.

- 사람이 매일/매 사이클 수동으로 주문을 계산·제출하는 부담을 제거한다.
- 전략 코어를 **순수 함수**로 구현해 **백테스트와 라이브가 동일한 코드**를 쓰게 한다.
- 종목과 레버리지 배수를 **하드코딩하지 않고 1급 파라미터(instrument registry)** 로 주입한다.

## Who are our users?

- Primary users: 라오어식 레버리지 ETF 전략(VR·MAB)을 직접 운용하는 **개인 투자자(봇 운영자 본인)**.
- Secondary users: 전략 파라미터를 백테스트로 검증하려는 **전략 연구자/튜너**.
- User personas:
  - "장투+단투 병행러" — VR로 장기 코어를, MAB로 스윙을 동시에 굴린다(별도 계좌).
  - "검증 우선러" — 라이브 전에 배수별·버전별 백테스트로 강건한 고원을 찾는다.

## Value proposition

- **하나의 repo, 두 전략 모듈**: 브로커 어댑터·시세·상태 저장·알림·백테스트 골격을 공유한다.
- **순수 함수 코어**: IO 없는 전략 로직 → 백테스트 결과와 라이브 거동이 일치한다.
- **종목·배수 파라미터화**: TQQQ(3x)·SOXL(3x)·QLD(2x) 등 instrument 레지스트리로 교체.
  코어가 `leverage`를 인지해 적정 target%·밴드·그라디언트를 조정한다.
- **오용 방지 가드레일**: 비레버리지(1x)·개별주 투입 시 전략 가정이 깨짐을 경고/차단한다.
- **세후 현실성**: 백테스트에 수수료·양도소득세 22%·환율·슬리피지를 반영한다.

# Success Metrics

## Key Performance Indicators

- 전략 코어 정확성: 순수 함수 단위 테스트 통과율 100%, 커버리지 ≥ 85%.
- 백테스트 신뢰성: 동일 Config로 백테스트↔라이브 의사결정이 1:1 재현 가능.
- 라이브 안전성: `dry_run` 기본 ON, 1주 소액 통합 테스트에서 체결·상태 갱신 무결성.
- 강건성: 파라미터 스윕에서 단일 최적값이 아닌 **강건한 고원**을 walk-forward·레짐 분할로 확인.

## Measurement frequency

- Daily: MAB 일일 주문 생성·체결 대조 정합성(라이브 운영 시).
- Weekly/Cycle: VR 사이클 리밸런싱 정합성, 상태(V_n·pool·qty) 갱신 검증.
- Monthly: 백테스트 vs 라이브 드리프트 점검, 세금 드래그·회전율 리뷰.

## Success examples

- 같은 전략을 2x vs 3x로 백테스트해 배수에 따른 CAGR/MDD/세금 드래그 차이를 정량 비교표로 산출.
- VR(TQQQ) + MAB(SOXL)를 **별도 계좌**로 병행 운용하면서 전략별 회계가 충돌 없이 분리 유지.

# Next Features (SPEC Backlog)

## High Priority (P0 — 인프라 + 백테스트, 라이브 불필요)

- Instrument 레지스트리: 종목별 `leverage`·`underlying`·기본 `target%`/`band` 주입 + 가드레일.
- 공용 도메인 모델 & Strategy 프로토콜: `plan_orders(market, state, cfg) -> [Order]`.
- VR 코어(순수 함수): `next_value`, `rebalance_decision`, `order_from_decision`.
- MAB 코어(순수 함수): `mab_daily_orders`, `mab_on_seed_exhausted`.
- 백테스트 엔진 + 비용 모델(수수료·양도세 22%·환율·슬리피지) + 배수별 베이스라인.
- 파라미터 스윕(walk-forward·레짐 분할).

## Medium Priority (P1–P2 — 어댑터 + dry-run)

- 토스 Open API 어댑터 read-only(인증·잔고·시세 조회).
- Order Manager + dry-run(의사결정 → 주문안 로깅, 멱등).
- State Store(전략별 namespace: vr / mab), Reconciliation.

## Future Considerations (P3–P4 — 라이브)

- VR 라이브 소액(예약지정가, LOC 불필요).
- MAB 라이브 — **선결: 토스 Open API의 LOC 노출 여부 확인(T8)**. 미지원 시 KIS 어댑터.
- 병행 운용·모니터링 안정화, 종목 확장 검증(SOXL 등).
- (보류) 한 전략의 멀티 종목 바스켓, 절세 매도 타이밍 최적화, WebSocket 스트리밍.

---

*Last updated: 2026-06-26*
*Version: 0.1.0*
