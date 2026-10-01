# pet_needs_pipeline

## 인스타그램 해시태그 파일럿 (Apify)

인스타의 역할: (1) 로봇청소기에 대한 강아지 반응 유형(robot 층), (2) 펫캠·홈캠 등 대체재 사용 장면·감정(substitute 층).
고민 도출은 네이버·유튜브·스레드가 담당한다.

### 준비
1. `pip install -r requirements.txt`
2. Apify 토큰: https://console.apify.com/settings/integrations → Personal API tokens 복사
3. `pet_needs_pipeline/.env` 파일에 한 줄 (따옴표·공백 없이, 절대 커밋하지 않음 — `.gitignore` 처리됨)
   ```
   APIFY_TOKEN=apify_api_xxxxxxxx
   ```

### 실행
```bash
# 파일럿: "수집" 태그 전체, 태그당 게시물 30 · 게시물당 댓글 30
python main.py --source instagram --pilot

# 일부 태그만 소규모 확인
python main.py --source instagram --pilot --tags 로봇청소기강아지,펫캠

# 본수집: hashtags_pilot_v2.csv 에서 판정 == keep 인 태그만. 예상 건수를 보여주고 y 입력 시 진행
python main.py --source instagram --full --posts 100 --comments 30
```
- 이미 수집한 태그는 `data/raw/checkpoint.json` 기준으로 건너뜀 (다시 받으려면 해당 `instagram_pilot|태그` 항목 삭제).
- 비용 상한(무료 플랜 단가): 게시물 $2.60/1,000건, 댓글 $2.30/1,000건. 무료 플랜은 해시태그당 첫 페이지 결과만 제공.

### 필터 v2 (강아지 단어·기간)
- robot 층 강아지 단어는 **해시태그(#...)를 뺀 본문**에서만 검사. 제거된 게시물 샘플: `outputs/tables/ig_dogfilter_removed_sample.csv` (10건, seed=42, 기간 필터와 무관하게 '강아지 단어 없음' 판정 게시물 기록)
- 기간은 `config.yaml` 의 `period_days`(365일). `instagram.expand_tags`(반응 태그 4개)만 365일 적용 후 게시물이 10개 미만이면 730일로 확장하고 `hashtags_pilot_v2.csv` 비고에 기록.
- 원본 JSON 으로 재처리(Apify 호출·체크포인트 변경 없음, 이전 결과는 `_v1` 로 최초 1회만 보존): `python main.py --source instagram --pilot --reprocess` → `ig_filter_change_compare.csv`

### 교집합 방식 (#로봇청소기 → 강아지 필터)
- `config.yaml` 의 `instagram.dog_scope_all_tags`: 검색 태그가 강아지와 무관한 태그. 강아지 판별에 **검색 태그를 뺀 해시태그 전체 + 본문**을 사용, 댓글은 강아지 통과 게시물에만 수집.
- 수량은 `instagram.tag_overrides` (로봇청소기: 게시물 50, 댓글 10). 단가는 `price_per_1000` (스타터 결제 후 수정).
- 실행: `python main.py --source instagram --pilot --tags 로봇청소기`
- 리포트(`ig_pilot_report.csv`): 광고 비율 = 광고/수집, 강아지 적중률 = 강아지 판별 통과/광고 제거 후(기간 무관), 최근 1년 게시물 비율, 강아지 통과 게시물 1개당 비용(추정 비용/최종 사용 게시물). 적중률 10% 이상이면 비고에 `본수집 후보`, 미만이면 `drop 후보`.
- 직접 태그와의 비교표: `outputs/tables/ig_intersection_compare.csv`
- 주의: 공식 액터(apify/instagram-hashtag-scraper)에는 정렬(최신순) 입력 필드가 없다.

### 본수집 (--full)과 산출물 분리
- `python main.py --source instagram --full --posts 200 --comments 10` : `hashtags_pilot_v2.csv` 에서 판정 == keep 인 태그만, 실행 전 예상 비용을 보여주고 y 입력을 받는다.
- 파일럿 결과는 덮어쓰지 않는다. 본수집 결과는 `_full` 파일로 따로 저장 (`ig_funnel_full`, `ig_report_full`, `ig_substitute_emotion_full`, `ig_emotion_rank_change_full` — 파일럿 대비 순위 변화).
- 파일럿과 겹친 게시물은 중복으로 빠지고 댓글도 다시 받지 않는다. 같은 날 원본 JSON 은 덮어쓰지 않고 `instagram_raw/old/` 로 보관.
- `--reprocess` 는 `--pilot` 에서만 동작한다 (full 로 원본 재구성하면 다른 태그 행이 사라짐).
- `outputs/tables/ig_source_decision.csv` : 태그별 최종 판정·사유·최종 문서 수 (소스 선정 근거).
- 판정이 drop 으로 확정된 태그는 `--pilot` 수집에서 제외된다.

### 대체재 감정 데이터 정제 (Apify 호출 없음)
`python main.py --source instagram --emotion-clean` — 본수집 태그 문서 기준, 파일럿 산출물은 건드리지 않음.
1. 고양이 필터: 고양이 단어(`animal.cat_terms` + `집사`)가 있고 강아지 판별(`animal.dog_terms`·이모지)에 안 걸리면 제거
2. 홍보 필터: `config/ig_promo_markers.txt` (일반 줄=강한 표지 → 제거 / `~`=약한 표지 → 플래그만 / `re:`=정규식). 해킹·보안 '우려'를 말하는 글은 강한 표지가 있어도 지우지 않고 `promo_suspect=1`
3. 감정 키워드 문서에 target(dog/security/other) 라벨 — 규칙은 `config.yaml` 의 `instagram.emotion_clean`
4. 산출물: `ig_emotion_clean_funnel`, `ig_emotion_by_target`, `ig_emotion_review_sample`(안심·걱정·불안 최대 100건, 사람이 human_target/human_is_promo 기입), `ig_emotion_docs_clean`

### 스레드(Threads) — 한국어 검색 불가로 drop (2026-10-01)
- 구현: `crawlers/threads_apify.py`(게시물 automation-lab/threads-scraper → 답글 automation-lab/threads-replies-scraper), `analysis/threads_pilot.py`, `config/threads_keywords.csv`(18개), `config/threads_relevance_words.txt`. 실행: `python main.py --source threads --pilot [--keywords "강아지 혼자,펫캠"]`
- 퍼널은 단계별 제거 건수를 기록하고, 0건 단계는 경고와 함께 점검 수치(날짜 파싱 성공 수·강아지 단어 포함률 등)를 `threads_funnel.csv` 의 '점검' 칸에 남긴다.
- 테스트 결과: automation-lab(강아지 혼자 0건 / 펫캠 30건 중 한국어 0건)·sourabhbgp(강아지 혼자 0건 / 펫캠 30건 중 한국어 3건) 모두 기준(한국어 글 비율 50%) 미달 → 스레드 소스 drop. 근거는 `outputs/tables/threads_actor_compare.csv`.
- `config.yaml` 의 `corpus.sources` 에 `threads` 를 추가하면 A 코퍼스에 편입된다 (기본 제외, instagram 도 기본 제외).

### 테스트 (Apify 호출·크레딧 없음)
```bash
python -m unittest discover -s tests -v
```

### 산출물
| 파일 | 내용 |
|---|---|
| `data/raw/instagram_raw/{YYYYMMDD}_{tag}_posts.json`, `_comments.json` | Apify 원본 응답 (작성자 정보 포함 → 커밋 금지) |
| `data/raw/instagram.csv` | 기존 스키마 + `query_group, collected_at` (누적, url_hash 중복 제거) |
| `data/raw/instagram_meta.csv` | url_hash, like_count, comment_count, is_video |
| `outputs/tables/ig_funnel.csv` | 필터 단계별 건수 |
| `outputs/tables/ig_pilot_report.csv` | 태그별 수량·자동 관련도·반응 라벨 비율·판정 제안 |
| `outputs/tables/ig_reaction_{types,summary,label_sample}.csv` | robot 층 강아지 반응 유형 라벨 |
| `outputs/tables/ig_substitute_emotion.csv` | substitute 층 감정 키워드 |
| `outputs/tables/ig_pilot_label_sample.csv` | 수동 관련도 라벨링용 (relevant 0/1 기입) |
| `config/hashtags_pilot_v2.csv` | 파일럿 결과 컬럼 갱신 (원본은 `.csv.bak`), 비고에 `drop/keep 후보` — 최종 판정은 사람이 |

### 사전 수정
- `config/ig_reaction_dict.yaml` 반응 유형 규칙·부정어 처리
- `config/ig_relevance_words.txt` 자동 관련도 사전
- `config/ig_emotion_words.txt` 감정 사전

### 규칙 라벨 정확도 검증
`ig_reaction_label_sample.csv` 의 `human_label` 을 사람이 채운 뒤:
```bash
python -m analysis.ig_label_eval
```

### 필터 순서와 주의
광고 → (댓글) 10자 미만 → 해시태그만 캡션 → 중복 → (robot) 강아지 단어 없음.
걸러진 게시물의 댓글은 크레딧 절약을 위해 **수집 전에 제외**하므로 댓글 "필터 전" 건수는 실제로 받은 댓글 수다.
