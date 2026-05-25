"""
select_200_stocks.py — choonsimi-data-backup 200종목 자동 선정 + 검증
═══════════════════════════════════════════════════════════════════════════
설계 (3번 검토 후 확정):
  1. CSV (data_3715_*.csv) → 매핑·상장일·보통주·상장주식수 (호출 0)
  2. fdr.StockListing('KRX') → 섹터 정보
  3. 코어 100 (55+45, 형 지정) → 분야 매핑 hardcoded
  4. 자동 +100 → 분야별 부족분을 시총 큰 순으로 자동 채움
  5. pykrx → 시총 / 거래대금 60d 검증

산출물:
  data/stocks_meta_extended.json  — 200종목 검증된 메타
  data/select_report.txt          — 검증 리포트
  data/sector_summary.txt         — 분야별 종목 수 요약

실행:
  pip install pykrx finance-datareader
  python select_200_stocks.py

KRX_ID/KRX_PW 환경변수 필요 (pykrx 시총·거래대금 조회용)
예상 실행시간: 약 5~7분
═══════════════════════════════════════════════════════════════════════════
"""

import os, sys, json, time, re
from datetime import datetime, timedelta
from collections import defaultdict

try:
    import pandas as pd
    from pykrx import stock as krx
    import FinanceDataReader as fdr
except ImportError as e:
    print(f"[FATAL] 의존성 설치 필요: pip install pykrx finance-datareader pandas")
    print(f"        {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════
# 경로
# ═══════════════════════════════════════════════════════════════════════════
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# KRX 종목 기본정보 CSV (data.krx.co.kr [12005] 다운로드본)
KRX_BASE_CSV = os.path.join(DATA_DIR, "krx_stocks_base_info.csv")

OUT_META    = os.path.join(DATA_DIR, "stocks_meta_extended.json")
OUT_REPORT  = os.path.join(DATA_DIR, "select_report.txt")
OUT_SECTOR  = os.path.join(DATA_DIR, "sector_summary.txt")

# ═══════════════════════════════════════════════════════════════════════════
# 필터 기준
# ═══════════════════════════════════════════════════════════════════════════
MIN_MARKET_CAP   = 2_000 * 1e8        # 시총 2,000억
MIN_TRADING_VAL  = 50 * 1e8           # 거래대금 60d 평균 50억
IPO_CUTOFF       = "2021-05-25"       # 5년 상장 기준
TRADING_DAYS_60D = 60
RATE_LIMIT_SEC   = 0.12

# ═══════════════════════════════════════════════════════════════════════════
# 13개 분야 + 200종목 목표 분배
# ═══════════════════════════════════════════════════════════════════════════
SECTOR_TARGETS = {
    "반도체/IT/가전":      30,
    "2차전지/소재":        20,
    "바이오/제약":         24,
    "자동차/모빌리티":     16,
    "금융":                16,
    "소비재/유통":         20,
    "에너지/화학":         16,
    "통신/미디어":         10,
    "건설/인프라/조선":    12,
    "유틸리티/방산":       10,
    "로봇/자동화":         10,
    "헬스케어/의료기기":    8,
    "AI/SW/게임":           8,
}
assert sum(SECTOR_TARGETS.values()) == 200

# ═══════════════════════════════════════════════════════════════════════════
# 코어 55종목 (형 제공, 5년 룰 면제, 무조건 포함)
# ═══════════════════════════════════════════════════════════════════════════
CORE_55 = [
    {"code":"005930","name":"삼성전자","market":"KOSPI","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"000660","name":"SK하이닉스","market":"KOSPI","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"373220","name":"LG에너지솔루션","market":"KOSPI","sector":"2차전지/소재","ca_zero":False,"ipo_date":None},
    {"code":"207940","name":"삼성바이오로직스","market":"KOSPI","sector":"바이오/제약","ca_zero":False,"ipo_date":None},
    {"code":"005380","name":"현대차","market":"KOSPI","sector":"자동차/모빌리티","ca_zero":False,"ipo_date":None},
    {"code":"000270","name":"기아","market":"KOSPI","sector":"자동차/모빌리티","ca_zero":False,"ipo_date":None},
    {"code":"068270","name":"셀트리온","market":"KOSDAQ","sector":"바이오/제약","ca_zero":False,"ipo_date":None},
    {"code":"035420","name":"NAVER","market":"KOSPI","sector":"AI/SW/게임","ca_zero":False,"ipo_date":None},
    {"code":"035720","name":"카카오","market":"KOSPI","sector":"AI/SW/게임","ca_zero":False,"ipo_date":None},
    {"code":"005490","name":"POSCO홀딩스","market":"KOSPI","sector":"에너지/화학","ca_zero":False,"ipo_date":None},
    {"code":"006400","name":"삼성SDI","market":"KOSPI","sector":"2차전지/소재","ca_zero":False,"ipo_date":None},
    {"code":"051910","name":"LG화학","market":"KOSPI","sector":"2차전지/소재","ca_zero":False,"ipo_date":None},
    {"code":"012330","name":"현대모비스","market":"KOSPI","sector":"자동차/모빌리티","ca_zero":False,"ipo_date":None},
    {"code":"055550","name":"신한지주","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"105560","name":"KB금융","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"017670","name":"SK텔레콤","market":"KOSPI","sector":"통신/미디어","ca_zero":False,"ipo_date":None},
    {"code":"030200","name":"KT","market":"KOSPI","sector":"통신/미디어","ca_zero":False,"ipo_date":None},
    {"code":"015760","name":"한국전력","market":"KOSPI","sector":"유틸리티/방산","ca_zero":False,"ipo_date":None},
    {"code":"033780","name":"KT&G","market":"KOSPI","sector":"소비재/유통","ca_zero":False,"ipo_date":None},
    {"code":"036570","name":"엔씨소프트","market":"KOSPI","sector":"AI/SW/게임","ca_zero":False,"ipo_date":None},
    {"code":"051900","name":"LG생활건강","market":"KOSPI","sector":"소비재/유통","ca_zero":False,"ipo_date":None},
    {"code":"090430","name":"아모레퍼시픽","market":"KOSPI","sector":"소비재/유통","ca_zero":False,"ipo_date":None},
    {"code":"096770","name":"SK이노베이션","market":"KOSPI","sector":"에너지/화학","ca_zero":False,"ipo_date":None},
    {"code":"010140","name":"삼성중공업","market":"KOSPI","sector":"건설/인프라/조선","ca_zero":False,"ipo_date":None},
    {"code":"329180","name":"HD현대중공업","market":"KOSPI","sector":"건설/인프라/조선","ca_zero":False,"ipo_date":None},
    {"code":"011200","name":"HMM","market":"KOSPI","sector":"건설/인프라/조선","ca_zero":False,"ipo_date":None},
    {"code":"010950","name":"S-Oil","market":"KOSPI","sector":"에너지/화학","ca_zero":False,"ipo_date":None},
    {"code":"259960","name":"크래프톤","market":"KOSPI","sector":"AI/SW/게임","ca_zero":False,"ipo_date":None},
    {"code":"012450","name":"한화에어로스페이스","market":"KOSPI","sector":"유틸리티/방산","ca_zero":False,"ipo_date":None},
    {"code":"316140","name":"우리금융지주","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"042700","name":"한미반도체","market":"KOSDAQ","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"247540","name":"에코프로비엠","market":"KOSDAQ","sector":"2차전지/소재","ca_zero":False,"ipo_date":None},
    {"code":"003670","name":"포스코퓨처엠","market":"KOSPI","sector":"2차전지/소재","ca_zero":False,"ipo_date":None},
    {"code":"034020","name":"두산에너빌리티","market":"KOSPI","sector":"유틸리티/방산","ca_zero":False,"ipo_date":None},
    {"code":"009830","name":"한화솔루션","market":"KOSPI","sector":"에너지/화학","ca_zero":False,"ipo_date":None},
    {"code":"066570","name":"LG전자","market":"KOSPI","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"009150","name":"삼성전기","market":"KOSPI","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"018880","name":"한온시스템","market":"KOSPI","sector":"자동차/모빌리티","ca_zero":False,"ipo_date":None},
    {"code":"097950","name":"CJ제일제당","market":"KOSPI","sector":"소비재/유통","ca_zero":False,"ipo_date":None},
    {"code":"271560","name":"오리온","market":"KOSPI","sector":"소비재/유통","ca_zero":False,"ipo_date":None},
    {"code":"138040","name":"메리츠금융지주","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"086790","name":"하나금융지주","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"032830","name":"삼성생명","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":None},
    {"code":"009540","name":"HD한국조선해양","market":"KOSPI","sector":"건설/인프라/조선","ca_zero":False,"ipo_date":None},
    {"code":"241560","name":"두산밥캣","market":"KOSPI","sector":"로봇/자동화","ca_zero":False,"ipo_date":None},
    {"code":"196170","name":"알테오젠","market":"KOSDAQ","sector":"바이오/제약","ca_zero":False,"ipo_date":None},
    {"code":"277810","name":"레인보우로보틱스","market":"KOSDAQ","sector":"로봇/자동화","ca_zero":False,"ipo_date":"2021-02-03"},
    {"code":"323410","name":"카카오뱅크","market":"KOSPI","sector":"금융","ca_zero":False,"ipo_date":"2021-08-06"},
    {"code":"039030","name":"이오테크닉스","market":"KOSDAQ","sector":"반도체/IT/가전","ca_zero":True,"ipo_date":None},
    {"code":"000100","name":"유한양행","market":"KOSPI","sector":"바이오/제약","ca_zero":False,"ipo_date":None},
    {"code":"079550","name":"LIG넥스원","market":"KOSPI","sector":"유틸리티/방산","ca_zero":False,"ipo_date":None},
    {"code":"064350","name":"현대로템","market":"KOSPI","sector":"유틸리티/방산","ca_zero":True,"ipo_date":None},
    {"code":"058470","name":"리노공업","market":"KOSDAQ","sector":"반도체/IT/가전","ca_zero":False,"ipo_date":None},
    {"code":"454910","name":"두산로보틱스","market":"KOSDAQ","sector":"로봇/자동화","ca_zero":True,"ipo_date":"2023-10-05"},
    {"code":"326030","name":"SK바이오팜","market":"KOSPI","sector":"바이오/제약","ca_zero":True,"ipo_date":None},
]

# ═══════════════════════════════════════════════════════════════════════════
# 추가 45종목 (형 제공 + 분야 확정)
# ═══════════════════════════════════════════════════════════════════════════
EXTRA_45 = [
    ("034220","LG디스플레이","반도체/IT/가전"),
    ("011070","LG이노텍","반도체/IT/가전"),
    ("240810","원익IPS","반도체/IT/가전"),
    ("357780","솔브레인","반도체/IT/가전"),
    ("095340","ISC","반도체/IT/가전"),
    ("005290","동진쎄미켐","반도체/IT/가전"),
    ("089030","테크윙","반도체/IT/가전"),
    ("222800","심텍","반도체/IT/가전"),
    ("086520","에코프로","2차전지/소재"),
    ("066970","엘앤에프","2차전지/소재"),
    ("020150","롯데에너지머티리얼즈","2차전지/소재"),
    ("361610","SK아이이테크놀로지","2차전지/소재"),
    ("128940","한미약품","바이오/제약"),
    ("185750","종근당","바이오/제약"),
    ("006280","녹십자","바이오/제약"),
    ("069620","대웅제약","바이오/제약"),
    ("302440","SK바이오사이언스","바이오/제약"),
    ("170900","동아에스티","바이오/제약"),
    ("145020","휴젤","바이오/제약"),
    ("086280","현대글로비스","자동차/모빌리티"),
    ("161390","한국타이어앤테크놀로지","자동차/모빌리티"),
    ("204320","HL만도","자동차/모빌리티"),
    ("011210","현대위아","자동차/모빌리티"),
    ("024110","기업은행","금융"),
    ("139480","이마트","소비재/유통"),
    ("004370","농심","소비재/유통"),
    ("008770","호텔신라","소비재/유통"),
    ("383220","F&F","소비재/유통"),
    ("282330","BGF리테일","소비재/유통"),
    ("011780","금호석유화학","에너지/화학"),
    ("011170","롯데케미칼","에너지/화학"),
    ("010060","OCI홀딩스","에너지/화학"),
    ("298050","HS효성첨단소재","에너지/화학"),
    ("032640","LG유플러스","통신/미디어"),
    ("000720","현대건설","건설/인프라/조선"),
    ("042660","한화오션","건설/인프라/조선"),
    ("047810","한국항공우주","유틸리티/방산"),
    ("058610","에스피지","로봇/자동화"),
    ("065350","신성델타테크","로봇/자동화"),
    ("214150","클래시스","헬스케어/의료기기"),
    ("041830","인바디","헬스케어/의료기기"),
    ("145720","덴티움","헬스케어/의료기기"),
    ("200670","휴메딕스","헬스케어/의료기기"),
    ("251270","넷마블","AI/SW/게임"),
    ("293490","카카오게임즈","AI/SW/게임"),
]

# ═══════════════════════════════════════════════════════════════════════════
# fdr Sector → 13 카테고리 매핑
# ═══════════════════════════════════════════════════════════════════════════
SECTOR_MAP_RULES = [
    # (키워드 패턴, 매핑 분야)  ※ 순서 중요 (위가 우선)
    (r"반도체|디스플레이|전자부품|영상기기|컴퓨터|통신장비", "반도체/IT/가전"),
    (r"2차전지|배터리|양극재|음극재|분리막|전해질", "2차전지/소재"),
    (r"제약|의약품|바이오|생명공학|진단", "바이오/제약"),
    (r"자동차|타이어|운수장비|자전거", "자동차/모빌리티"),
    (r"은행|증권|보험|금융|지주회사|투자", "금융"),
    (r"식료품|음료|섬유|의복|가방|신발|화장품|유통|백화점|편의점|호텔|레저|광고|출판|교육", "소비재/유통"),
    (r"화학|석유|정유|가스|에너지", "에너지/화학"),
    (r"통신|방송|미디어|콘텐츠", "통신/미디어"),
    (r"건설|건축|토목|조선|기자재", "건설/인프라/조선"),
    (r"전력|수도|방위|항공우주", "유틸리티/방산"),
    (r"기계|로봇|자동화|정밀", "로봇/자동화"),
    (r"의료|의료기기|미용", "헬스케어/의료기기"),
    (r"소프트웨어|게임|인터넷|IT서비스|포털", "AI/SW/게임"),
    # 1차 산업 / 기타 (제외)
    (r"금속|철강|광업|운송|창고", "기타"),
    (r"농업|어업|임업|광물", "기타"),
]

def map_fdr_sector(sector: str) -> str:
    if not sector or pd.isna(sector):
        return "기타"
    for pattern, cat in SECTOR_MAP_RULES:
        if re.search(pattern, str(sector)):
            return cat
    return "기타"

# ═══════════════════════════════════════════════════════════════════════════
# 종목명 기반 Fallback 분야 분류 (fdr 실패 시)
# ═══════════════════════════════════════════════════════════════════════════
NAME_FALLBACK_RULES = [
    # (키워드 패턴, 분야)  ※ 순서 중요
    (r"반도체|디스플레이|OLED|메모리|로직|반도", "반도체/IT/가전"),
    (r"전자(?!.*증권)|이노텍|전기(?!.*자동차)", "반도체/IT/가전"),
    (r"PCB|회로|패키지|기판", "반도체/IT/가전"),
    # 2차전지/소재 — 회사명 다양해서 키워드 확장
    (r"양극재|음극재|배터리|2차전지|이차전지|전해질|분리막|동박|에코프로|엘앤에프", "2차전지/소재"),
    (r"신소재|소재(?!공정)|첨단소재|머티리얼|케미스트리|엔켐|코스모|WCP|더블유씨피|성호전자|나노신소재|일진|솔루스", "2차전지/소재"),
    (r"바이오|제약|약품|백신|진단|항체|셀트리|유전체|메디|팜", "바이오/제약"),
    # 자동차/모빌리티 — 부품사 키워드 확장
    (r"자동차|타이어|모비스|글로비스|모빌리티|만도|위아|차부품", "자동차/모빌리티"),
    (r"에스엘|성우하이텍|화신|평화홀딩스|평화정공|S&T|에스앤티|덕양|일지|코다코|디아이씨|상신|동원금속|모토닉|HL홀딩스", "자동차/모빌리티"),
    (r"금융지주|은행|증권|보험|생명|화재|캐피탈|손해", "금융"),
    (r"식품|제과|음료|주류|화장품|뷰티|이마트|롯데쇼핑|호텔|면세|레저|콘", "소비재/유통"),
    (r"리테일|마트|백화점|편의점|패션|의류", "소비재/유통"),
    (r"화학|케미칼|석유|정유|에너지(?!.*솔루션)|가스|LPG|LNG", "에너지/화학"),
    (r"텔레콤|통신|유플러스|미디어|방송|콘텐츠|엔터", "통신/미디어"),
    (r"건설|건축|토목|시멘트|레미콘|아스콘", "건설/인프라/조선"),
    (r"조선|중공업|해양|선박", "건설/인프라/조선"),
    (r"전력|한전|가스공사|수자원|항공우주|방산|디펜스|넥스원|에어로|로템|풍산", "유틸리티/방산"),
    (r"로보틱스|로봇|자동화|밥캣|HD현대인프라|두산밥캣", "로봇/자동화"),
    # 헬스케어/의료기기 — 키워드 확장
    (r"의료기기|임플란트|덴티움|인바디|보툴리|미용|클래시스|휴젤|휴메딕", "헬스케어/의료기기"),
    (r"오스템|메디|뷰노|루닛|딥노이드|뷰웍스|아이센스|루트로닉|레이|원텍|이루다", "헬스케어/의료기기"),
    (r"소프트웨어|게임즈|게임|넷마블|크래프톤|엔씨|넥슨|위메이드|컴투스", "AI/SW/게임"),
    (r"NAVER|카카오(?!뱅크)|네이버|IT서비스|클라우드|솔루션(?!.*에너지)", "AI/SW/게임"),
]

def fallback_sector_by_name(name: str) -> str:
    """종목명 기반 분야 추정 (fdr 실패 시 백업)"""
    if not name or pd.isna(name):
        return "기타"
    for pattern, cat in NAME_FALLBACK_RULES:
        if re.search(pattern, str(name)):
            return cat
    return "기타"

# ═══════════════════════════════════════════════════════════════════════════
# Step 1. CSV 로드 + 보통주 + 5년 상장 필터
# ═══════════════════════════════════════════════════════════════════════════
def load_base_universe() -> pd.DataFrame:
    if not os.path.exists(KRX_BASE_CSV):
        print(f"[FATAL] KRX 기본정보 CSV 없음: {KRX_BASE_CSV}")
        print(f"        data.krx.co.kr [12005] 다운로드 후 data/krx_stocks_base_info.csv로 저장")
        sys.exit(1)
    
    df = pd.read_csv(KRX_BASE_CSV, encoding='euc-kr', dtype={'단축코드': str})
    df['단축코드'] = df['단축코드'].str.zfill(6)
    df['상장일'] = pd.to_datetime(df['상장일'], format='%Y/%m/%d', errors='coerce')
    
    # 보통주만, KOSPI/KOSDAQ만
    df = df[df['주식종류']=='보통주']
    df = df[df['시장구분'].isin(['KOSPI','KOSDAQ','KOSDAQ GLOBAL'])]
    # KOSDAQ GLOBAL → KOSDAQ 통합
    df['시장구분'] = df['시장구분'].replace('KOSDAQ GLOBAL','KOSDAQ')
    
    print(f"  CSV 로드: {len(df)}개 보통주 (KOSPI+KOSDAQ)")
    return df

# ═══════════════════════════════════════════════════════════════════════════
# Step 2. fdr로 섹터 정보 추가
# ═══════════════════════════════════════════════════════════════════════════
def add_sector_info(df: pd.DataFrame) -> pd.DataFrame:
    """fdr.StockListing은 시장별로 분리 호출해야 Sector 컬럼 나옴 (v0.9.x 기준)"""
    print("  fdr StockListing 호출 중 (KOSPI + KOSDAQ 분리)...")
    
    sector_map = {}
    fdr_failed = False
    
    for market in ['KOSPI', 'KOSDAQ']:
        try:
            fdr_df = fdr.StockListing(market)
            # 종목코드 컬럼명 자동 탐지
            code_col = None
            for c in ['Code', 'Symbol', '종목코드', 'code']:
                if c in fdr_df.columns:
                    code_col = c
                    break
            if code_col is None:
                print(f"  [WARN] fdr {market}: 종목코드 컬럼 없음. 컬럼 목록: {list(fdr_df.columns)}")
                fdr_failed = True
                continue
            
            # 섹터 컬럼명 자동 탐지
            sector_col = None
            for c in ['Sector', 'Industry', 'industry', '업종', 'IndustryCode', 'sector']:
                if c in fdr_df.columns:
                    sector_col = c
                    break
            if sector_col is None:
                print(f"  [WARN] fdr {market}: Sector 컬럼 없음. 실제 컬럼: {list(fdr_df.columns)[:15]}")
                fdr_failed = True
                continue
            
            print(f"  ✓ fdr {market}: {len(fdr_df)}종목 ({code_col} + {sector_col})")
            
            # 첫 3행 샘플 출력 (디버그)
            sample = fdr_df[[code_col, sector_col]].head(3)
            for _, row in sample.iterrows():
                print(f"    sample: {row[code_col]} → {row[sector_col]}")
            
            fdr_df[code_col] = fdr_df[code_col].astype(str).str.zfill(6)
            sector_map.update(dict(zip(fdr_df[code_col], fdr_df[sector_col])))
            
        except Exception as e:
            print(f"  [WARN] fdr {market} 실패: {type(e).__name__}: {str(e)[:100]}")
            fdr_failed = True
    
    # fdr 결과 적용
    df['fdr_sector'] = df['단축코드'].map(sector_map).fillna('')
    df['cat_fdr']    = df['fdr_sector'].apply(map_fdr_sector)
    
    # Fallback: fdr 매핑 실패 또는 "기타"로 분류된 종목 → 종목명 기반
    df['cat_name']   = df['한글 종목약명'].apply(fallback_sector_by_name)
    
    # 최종 cat: fdr 우선, 실패 시 종목명 매핑 사용
    def pick_cat(row):
        if row['cat_fdr'] != '기타':
            return row['cat_fdr']
        return row['cat_name']  # 종목명 fallback
    
    df['cat'] = df.apply(pick_cat, axis=1)
    
    print(f"\n  분야 분류 결과:")
    print(f"    fdr 매핑 성공:    {(df['cat_fdr'] != '기타').sum()}")
    print(f"    name fallback:    {((df['cat_fdr'] == '기타') & (df['cat_name'] != '기타')).sum()}")
    print(f"    최종 '기타' (제외): {(df['cat'] == '기타').sum()}")
    print(f"\n  최종 분야 분포 (상위 15):")
    print(df['cat'].value_counts().head(15).to_string())
    return df

# ═══════════════════════════════════════════════════════════════════════════
# Step 3. pykrx로 시총·거래대금 받기 (전 종목 1회)
# ═══════════════════════════════════════════════════════════════════════════
def fetch_market_metrics(codes: list) -> dict:
    """codes 리스트 종목의 시총·거래대금60d 받음."""
    print(f"  pykrx 호출 중 ({len(codes)}종목, 60일치 OHLCV)...")
    
    today = krx.get_nearest_business_day_in_a_week(datetime.now().strftime("%Y%m%d"))
    start_60d = (datetime.strptime(today, "%Y%m%d") - timedelta(days=95)).strftime("%Y%m%d")
    
    # 시총: 전 종목 1회 호출 (KOSPI/KOSDAQ 각 1번)
    cap_dict = {}
    for market in ["KOSPI","KOSDAQ"]:
        try:
            cap_df = krx.get_market_cap_by_ticker(today, market=market)
            for code in cap_df.index:
                cap_dict[code] = {
                    'market_cap': int(cap_df.loc[code,'시가총액']),
                    'shares':     int(cap_df.loc[code,'상장주식수']),
                }
            time.sleep(RATE_LIMIT_SEC)
        except Exception as e:
            print(f"  [WARN] {market} 시총 조회 실패: {e}")
    
    # 거래대금 60d: 종목별 호출
    metrics = {}
    debug_printed = False
    for i, code in enumerate(codes):
        m = cap_dict.get(code, {'market_cap': 0, 'shares': 0})
        try:
            ohlcv = krx.get_market_ohlcv(start_60d, today, code)
            if ohlcv is not None and len(ohlcv) > 0:
                # 첫 종목 컬럼 1회 출력 (디버그용)
                if not debug_printed:
                    print(f"  [DEBUG] {code} ohlcv 컬럼: {list(ohlcv.columns)}")
                    debug_printed = True
                
                # 거래대금 계산: 3중 안전망
                # 1순위: 직접 거래대금 컬럼 (있을 경우)
                # 2순위: 종가 × 거래량 직접 계산 (KRX_ID 인증 모드는 거래대금 컬럼 없음)
                trdval_col = None
                for c in ['거래대금','Amount','trading_value','TradingValue']:
                    if c in ohlcv.columns:
                        trdval_col = c
                        break
                
                if trdval_col:
                    # 1순위: 직접 컬럼 사용
                    m['avg_trdval_60d'] = int(ohlcv[trdval_col].tail(TRADING_DAYS_60D).mean())
                else:
                    # 2순위: 종가 × 거래량 계산
                    close_col = next((c for c in ['종가','Close','close'] if c in ohlcv.columns), None)
                    vol_col   = next((c for c in ['거래량','Volume','volume'] if c in ohlcv.columns), None)
                    if close_col and vol_col:
                        recent = ohlcv.tail(TRADING_DAYS_60D)
                        # 거래대금 = 종가 × 거래량 (일자별 곱하고 평균)
                        daily_trdval = recent[close_col] * recent[vol_col]
                        m['avg_trdval_60d'] = int(daily_trdval.mean())
                        if i == 0:
                            print(f"  [INFO] 거래대금 컬럼 없음 → 종가×거래량 계산 방식 사용")
                    else:
                        m['avg_trdval_60d'] = 0
                        m['_cols'] = list(ohlcv.columns)
                
                m['days'] = len(ohlcv.tail(TRADING_DAYS_60D))
            else:
                m['avg_trdval_60d'] = 0
                m['days'] = 0
        except Exception as e:
            m['avg_trdval_60d'] = 0
            m['days'] = 0
            m['_err'] = str(e)[:50]
        
        metrics[code] = m
        if (i+1) % 20 == 0:
            print(f"    진행: {i+1}/{len(codes)}")
        time.sleep(RATE_LIMIT_SEC)
    
    # 통계 출력
    passed_trdval = sum(1 for m in metrics.values() if m.get('avg_trdval_60d', 0) >= MIN_TRADING_VAL)
    print(f"\n  거래대금 ≥ 50억 통과: {passed_trdval}/{len(codes)}")
    if passed_trdval == 0:
        # 샘플 5개 거래대금 값 출력 (진단용)
        sample = list(metrics.items())[:5]
        print(f"  [WARN] 거래대금 통과 0건. 샘플:")
        for code, m in sample:
            print(f"    {code}: avg_trdval={m.get('avg_trdval_60d',0):,}, market_cap={m.get('market_cap',0):,}")
    
    return metrics

# ═══════════════════════════════════════════════════════════════════════════
# Step 4. 자동 추가 +100 선정
# ═══════════════════════════════════════════════════════════════════════════
def select_extra_100(base_df: pd.DataFrame, core_codes: set, metrics: dict) -> list:
    """분야별 부족분을 시총 큰 순으로 자동 채움."""
    # 코어 100 분야 분포
    core_dist = defaultdict(int)
    for s in CORE_55:
        core_dist[s['sector']] += 1
    for _, _, sec in EXTRA_45:
        core_dist[sec] += 1
    
    print("\n  코어 100 분야 분포:")
    for k, v in core_dist.items():
        target = SECTOR_TARGETS.get(k, 0)
        print(f"    {k:20s} {v:3}/{target:3}  (부족 {target-v})")
    
    # 후보 풀: 코어 제외 + 5년 상장 + 시총/거래대금 통과
    cutoff = pd.Timestamp(IPO_CUTOFF)
    candidates = []
    for _, row in base_df.iterrows():
        code = row['단축코드']
        if code in core_codes:
            continue
        if pd.isna(row['상장일']) or row['상장일'] > cutoff:
            continue
        m = metrics.get(code, {})
        if m.get('market_cap', 0) < MIN_MARKET_CAP:
            continue
        if m.get('avg_trdval_60d', 0) < MIN_TRADING_VAL:
            continue
        if row['cat'] == '기타':
            continue
        
        candidates.append({
            'code':   code,
            'name':   row['한글 종목약명'],
            'market': row['시장구분'],
            'sector': row['cat'],
            'market_cap':     m['market_cap'],
            'avg_trdval_60d': m.get('avg_trdval_60d', 0),
            'shares':         m.get('shares', 0),
            'ipo_date':       row['상장일'].strftime('%Y-%m-%d') if pd.notna(row['상장일']) else None,
        })
    
    # 분야 + 시총 정렬
    candidates.sort(key=lambda x: -x['market_cap'])
    print(f"\n  후보 풀: {len(candidates)}개 (검증 통과)")
    
    # 1단계: 분야별 부족분 채움
    selected = []
    selected_codes = set()
    sector_count = dict(core_dist)
    for c in candidates:
        sec = c['sector']
        target = SECTOR_TARGETS.get(sec, 0)
        cur = sector_count.get(sec, 0)
        if cur < target:
            selected.append({**c, 'ca_zero': False, 'source': 'auto_sector'})
            selected_codes.add(c['code'])
            sector_count[sec] = cur + 1
            if sum(sector_count.values()) >= 200:
                break
    
    print(f"\n  1단계 (분야별 채움): {len(selected)}개 선정")
    
    # 2단계: 200 미달 시 시총 큰 순으로 채움 (분야 무관)
    total = sum(sector_count.values())
    if total < 200:
        shortage = 200 - total
        print(f"  2단계 (분야 무관 시총 채움): {shortage}개 추가 필요")
        for c in candidates:
            if c['code'] in selected_codes:
                continue
            if total >= 200:
                break
            selected.append({**c, 'ca_zero': False, 'source': 'auto_fill'})
            selected_codes.add(c['code'])
            sector_count[c['sector']] = sector_count.get(c['sector'], 0) + 1
            total += 1
        print(f"  2단계 완료: 자동 추가 총 {len(selected)}개")
    
    print(f"\n  최종 분야 분포:")
    for k in SECTOR_TARGETS:
        v = sector_count.get(k, 0)
        target = SECTOR_TARGETS[k]
        mark = "✓" if v >= target else f"부족 {target-v}"
        print(f"    {k:20s} {v:3}/{target:3}  {mark}")
    
    return selected

# ═══════════════════════════════════════════════════════════════════════════
# Step 5. 결과 저장
# ═══════════════════════════════════════════════════════════════════════════
def save_results(final_200: list, sector_dist: dict, missing: dict):
    # 메타 저장
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump(final_200, f, ensure_ascii=False, indent=2)
    print(f"\n  ✓ {OUT_META} ({len(final_200)}종목)")
    
    # 리포트
    lines = ["="*70, f"choonsimi-data-backup 200종목 선정 리포트", f"실행일시: {datetime.now():%Y-%m-%d %H:%M:%S}", "="*70, ""]
    lines.append(f"[요약]  최종 {len(final_200)}/200 종목")
    lines.append("")
    lines.append("[분야별 분포]")
    for k in SECTOR_TARGETS:
        v = sector_dist.get(k, 0)
        t = SECTOR_TARGETS[k]
        mark = "✓" if v >= t else f"부족 {t-v}"
        lines.append(f"  {k:20s}  {v:3}/{t:3}  {mark}")
    
    if missing:
        lines.append("")
        lines.append("[분야 부족 종목 정보]")
        for k, v in missing.items():
            lines.append(f"  {k}: 부족 {v}개 (후보 풀 부족)")
    
    lines.append("")
    lines.append("[종목 리스트]")
    for s in final_200:
        lines.append(f"  {s['code']} {s['name']:25s} {s['market']:6s} {s['sector']}")
    
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  ✓ {OUT_REPORT}")

# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    print("="*70)
    print("choonsimi-data-backup: 200종목 자동 선정")
    print("="*70)
    
    print("\n[STEP 1] CSV 로드")
    base = load_base_universe()
    
    print("\n[STEP 2] fdr 섹터 정보 추가")
    base = add_sector_info(base)
    
    # 코어 100 코드 집합
    core_codes = set(s['code'] for s in CORE_55) | set(e[0] for e in EXTRA_45)
    
    print("\n[STEP 3] pykrx 시총·거래대금 조회")
    # 효율: 전 종목 후보가 아닌 5년 상장 보통주 풀(~2,100개)만
    # 시총 5,000억+ 만 사전 필터하면 ~500개. 거기서 검증.
    cutoff = pd.Timestamp(IPO_CUTOFF)
    base_5yr = base[base['상장일'] <= cutoff].copy()
    print(f"  5년 상장 보통주: {len(base_5yr)}개")
    
    # 검증 대상: 코어 100 + 5년 상장 추가 후보 (시총 큰 순으로 ~500개만 우선 조회)
    # 실제로는 전 종목 조회가 더 단순. 시총은 전 종목 1회 호출이라 거의 무료.
    # 거래대금은 종목별이라 비싸지만, 시총 미달 종목은 자동 제외되므로 OK.
    
    # 시총 먼저: 전 종목 조회
    today = krx.get_nearest_business_day_in_a_week(datetime.now().strftime("%Y%m%d"))
    cap_all = {}
    for market in ["KOSPI","KOSDAQ"]:
        cap_df = krx.get_market_cap_by_ticker(today, market=market)
        for code in cap_df.index:
            cap_all[code] = int(cap_df.loc[code,'시가총액'])
        time.sleep(RATE_LIMIT_SEC)
    
    # 시총 2,000억 이상만 후보 (속도 향상)
    base_5yr['market_cap'] = base_5yr['단축코드'].map(cap_all).fillna(0).astype(int)
    qualified = base_5yr[base_5yr['market_cap'] >= MIN_MARKET_CAP]
    print(f"  시총 ≥ 2,000억: {len(qualified)}개")
    
    # 거래대금 조회 대상: 코어 100 + 시총 통과 후보
    target_codes = list(core_codes | set(qualified['단축코드']))
    print(f"  거래대금 조회 대상: {len(target_codes)}종목")
    
    metrics = fetch_market_metrics(target_codes)
    
    # base에 metrics 머지
    base['market_cap']     = base['단축코드'].map(lambda c: metrics.get(c,{}).get('market_cap', 0))
    base['avg_trdval_60d'] = base['단축코드'].map(lambda c: metrics.get(c,{}).get('avg_trdval_60d', 0))
    base['shares']         = base['단축코드'].map(lambda c: metrics.get(c,{}).get('shares', 0))
    
    print("\n[STEP 4] 자동 +100 선정")
    extras_auto = select_extra_100(base, core_codes, metrics)
    
    # 코어 100 정리 (CSV 정보로 보강)
    print("\n[STEP 5] 최종 200 통합")
    final = []
    code_to_csv = {row['단축코드']: row for _, row in base.iterrows()}
    
    for s in CORE_55:
        csv_row = code_to_csv.get(s['code'])
        item = dict(s)
        item['source'] = 'core'
        if csv_row is not None:
            item['market_cap']     = int(csv_row['market_cap']) if pd.notna(csv_row['market_cap']) else 0
            item['avg_trdval_60d'] = int(metrics.get(s['code'],{}).get('avg_trdval_60d', 0))
            item['shares']         = int(csv_row['shares']) if pd.notna(csv_row.get('shares', 0)) else 0
            if csv_row['상장일'] is not pd.NaT and pd.notna(csv_row['상장일']):
                if not item['ipo_date']:
                    item['ipo_date'] = csv_row['상장일'].strftime('%Y-%m-%d')
        final.append(item)
    
    for code, name, sec in EXTRA_45:
        csv_row = code_to_csv.get(code)
        item = {
            'code': code, 'name': name, 'sector': sec,
            'market': csv_row['시장구분'] if csv_row is not None else '?',
            'ca_zero': False, 'ipo_date': None, 'source': 'extra45',
        }
        if csv_row is not None:
            item['market_cap']     = int(csv_row.get('market_cap', 0)) if pd.notna(csv_row.get('market_cap', 0)) else 0
            item['avg_trdval_60d'] = int(metrics.get(code,{}).get('avg_trdval_60d', 0))
            item['shares']         = int(csv_row.get('shares', 0)) if pd.notna(csv_row.get('shares', 0)) else 0
            if pd.notna(csv_row['상장일']):
                item['ipo_date'] = csv_row['상장일'].strftime('%Y-%m-%d')
        final.append(item)
    
    final.extend(extras_auto)
    
    # 분야별 분포
    sector_dist = defaultdict(int)
    for s in final:
        sector_dist[s['sector']] += 1
    
    missing = {k: SECTOR_TARGETS[k] - sector_dist[k] for k in SECTOR_TARGETS if sector_dist[k] < SECTOR_TARGETS[k]}
    
    save_results(final, dict(sector_dist), missing)
    
    print("\n" + "="*70)
    print(f"완료. 최종 {len(final)}/200 종목")
    if missing:
        print(f"⚠ 분야 부족: {missing}")
    print(f"메타: {OUT_META}")
    print(f"리포트: {OUT_REPORT}")
    print("="*70)


if __name__ == "__main__":
    main()
