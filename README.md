# choonsimi-premium

Korean equity quant signal engine — **v10.0** (단기 5일 스윙)

## 빠른 시작

### 1. GitHub Secrets 등록 (Settings → Secrets)
- `ECOS_KEY` — 한국은행 ECOS API key (선택, 매크로 강화)
- `DART_API_KEY` — DART 재무 API key (선택, 종목별 ROE/debt 표시용)
- `TELEGRAM_BOT_TOKEN` — 텔레그램 봇 토큰 (선택)
- `TELEGRAM_CHAT_ID` — 수신 채팅 ID (선택)

**중요**: 모든 secrets 누락해도 작동. 단 텔레그램 알림 X, ROE/debt 표시 X.

### 2. 초기 데이터 backfill (1회만)
GitHub Actions → **Daily Signal** workflow → "Run workflow" → `backfill_data: 250`

이걸로 250 영업일치 데이터 수집. 이후 매일 08:30 자동 실행.

### 3. 매일 자동 실행
- **08:30 KST** — Daily Macro Fetch (KOSPI200, V-KOSPI, SOX, VIX, USD-KRW, ECOS)
- **08:50 KST** — Daily Signal (종목 OHLCV/수급 수집 → 5축 점수 → Top20/Top5 → 텔레그램)

## 시스템 개요

### 5축 가중합 (M / F / Q / R / L)
| 축 | 의미 | 구성 |
|---|---|---|
| **M** | 모멘텀 | RS(r20×0.6 + r60×0.4) + 20일 신고가 + 강제컷(직전5d +0.3~+7%) |
| **F** | 수급 | 외국인 5d×0.45 + 기관 5d×0.40 + 지분율 변화×0.15 |
| **Q** | 품질 | 시총 ≥ 500억 + 좀비 종목 50% 페널티 |
| **R** | 레짐 정합 | KOSPI200 > 200MA AND V-KOSPI < 25 |
| **L** | 유동성 | 거래대금 60d ≥ 100억 + Volume surge ≥ 1.5x |

### 진입·청산 룰 (Phase 2 v3 검증)
- 진입: T+1 시가 (갭업 +1% 이상 회피)
- TP +10% / SL -4% / 시간청산 28일
- Position sizing: Top1 30% / Top2-3 20% / Top4-5 15%
- DOWNTREND 진입 차단 (옵션)

### 백테스트 결과 (Phase 2 5년치 walk-forward)
| 구간 | 거래수 | 적중률 | CAGR | 샤프 | MDD |
|---|---|---|---|---|---|
| Training (2021-23) | 369 | 38.75% | +3.68% | 0.29 | -26.81% |
| Validation (2024) | 159 | 41.51% | +40.75% | 1.79 | -12.08% |
| **Test (2025-26.5)** | **247** | **42.91%** | **+64.30%** | **1.95** | -16.16% |
| 5년 누적 | - | - | +204% | - | - |

**B&H KOSPI200 5년 누적 +190% 대비 +13.47%p 우위.**

## 파일 구조

```
choonsimi-premium/
├── engine.py             # 5축 가중합 + Top20/Top5 + Forward Test
├── fetch_data.py         # pykrx로 OHLCV + 수급 + 시총 수집
├── fetch_macro.py        # pykrx + FDR + ECOS로 매크로 수집
├── fetch_dart.py         # DART 분기 재무 (ROE/debt, 표시용 — 점수 영향 X)
├── requirements.txt
├── data/
│   ├── stocks_meta.json      # 55종목 메타
│   ├── history.csv           # 종목 일별 OHLCV (자동 갱신)
│   ├── market_flow.json      # 종목별 수급 (자동 갱신)
│   ├── macro.json            # 매크로 (자동 갱신)
│   ├── fundamental.json      # 시총·발행주식수 + 재무 (자동 갱신)
│   ├── corp_map_cache.json   # DART corp_code 매핑 캐시
│   ├── signal_history.csv    # Top20 누적 (engine.py 출력)
│   ├── result.json           # 오늘 결과 (engine.py 출력)
│   └── forward_test.csv      # T+1/T+3/T+5 추적 (engine.py 출력)
└── .github/workflows/
    ├── daily_macro.yml       # 08:30 KST
    └── daily_signal.yml      # 08:50 KST (fetch_data → fetch_dart → engine)
```

## 실전 운용 가이드 (외부 검토자 권장)

### 자금 관리
- **총자산 20% 이하만 투입** (-26% MDD 맞아도 총자산 -5%)
- 1종목 최대 7%
- 손절 -4% 칼같이

### 레짐 인식
- KOSPI 120일선 위 + 월봉 3개 양봉 = 시스템 OFF 검토 (KODEX200 매수)
- 폭등장에서 알파 -165%. 시스템은 약세장 방패 + 횡보장 단검

### 검증 트리거 (1개월 후)
- Forward T+5 승률 < 48% → 폐기 검토
- 2개월 누적 CAGR 환산 < +10% → 폐기 확정
- MDD > -25% (1구간) → 즉시 중단

## 한계 (정직)
1. **알파 -165%** — 폭등장(KOSPI200 +265%) 매수후보유 못 이김. 구조적.
2. **Training MDD -26.81%** — 약세장 손실 가능. 자금관리 필수.
3. **수익보장 X** — 백테스트는 과거. 1개월 forward 검증 필수.
4. **종목 풀 55개** — 코스닥 중소형 폭등주 미포함.

## 라이센스
Private. 무단 배포 금지.
