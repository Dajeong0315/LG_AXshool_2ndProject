"""반려견 보호자 니즈 파이프라인: python main.py 한 번으로 수집 → 전처리 → 분석 → 그래프.

옵션:  --skip-collect   이미 모은 data/raw 로 분석만 다시 돌릴 때
       --config PATH    다른 설정 파일 사용
       --source instagram --pilot | --full [--posts N --comments M] [--tags a,b] [--reprocess]
                        인스타 해시태그 파일럿/본수집 (Apify). 나머지 파이프라인은 실행하지 않음
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                      # 어디서 실행하든 상대경로가 프로젝트 폴더 기준이 되게
sys.path.insert(0, str(ROOT))


def setup_logging(log_dir: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("pipeline")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(Path(log_dir) / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--skip-collect", action="store_true")
    ap.add_argument("--source", choices=["instagram", "threads"], help="지정하면 해당 소스만 수집·분석")
    ap.add_argument("--pilot", action="store_true", help="태그당 게시물 30, 게시물당 댓글 30")
    ap.add_argument("--full", action="store_true", help="hashtags_pilot_v2.csv 에서 판정==keep 인 태그만")
    ap.add_argument("--posts", type=int, help="태그(키워드)당 게시물 수 (기본: [instagram] config tag_overrides > pilot 30 / full 100)")
    ap.add_argument("--comments", type=int, help="게시물당 댓글 수 (기본 30)")
    ap.add_argument("--reprocess", action="store_true", help="Apify 호출 없이 data/raw/instagram_raw 원본 JSON 으로 변환·필터·분석만 재실행 (기존 결과는 _v1 로 보존)")
    ap.add_argument("--recollect", help="쉼표로 구분한 태그를 초기화(원본 보관·CSV 행·체크포인트 항목 제거)하고 다시 수집 (--pilot/--full 과 함께)")
    ap.add_argument("--emotion-clean", action="store_true", help="대체재 감정 데이터 정제(고양이·홍보 필터, target 분류, 검토 샘플). Apify 호출 없음")
    ap.add_argument("--keywords", help="[threads] 쉼표로 구분한 키워드만 실행 (예: 강아지 혼자,펫캠)")
    ap.add_argument("--replies", type=int, help="[threads] 게시물당 답글 수 (기본 20)")
    ap.add_argument("--tags", help="쉼표로 구분한 태그만 실행 (예: 로봇청소기강아지,펫캠)")
    ap.add_argument("--yes", action="store_true", help="--full 확인 질문 생략 (비권장)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    log = setup_logging(cfg["paths"]["logs"])
    log.info("시작 | test_mode=%s scale=%s 기간=최근 %s일", cfg.get("test_mode"), cfg.get("scale"), cfg["period_days"])

    if args.source == "threads":
        if args.pilot == args.full:
            ap.error("--source threads 는 --pilot 또는 --full 중 하나를 지정하세요")
        from analysis.threads_pilot import run_threads_analysis
        from crawlers.threads_apify import reset_keywords, run_threads
        mode = "pilot" if args.pilot else "full"
        if args.recollect:
            reset_keywords(cfg, mode, args.recollect.split(","))
        run_threads(cfg, mode, args.posts, args.replies, args.keywords.split(",") if args.keywords else None, args.yes)
        try:
            run_threads_analysis(cfg, mode)
        except FileNotFoundError:
            log.error("분석할 스레드 데이터가 없습니다. logs/failures.csv 를 확인하세요.")
        return

    if args.source == "instagram":
        if args.emotion_clean:
            from analysis.ig_emotion_clean import run_emotion_clean
            run_emotion_clean(cfg)
            return
        if args.pilot == args.full:
            ap.error("--source instagram 은 --pilot 또는 --full 중 하나를 지정하세요")
        from analysis.ig_pilot import run_ig_analysis
        from crawlers.instagram_apify import reprocess_instagram, reset_tags, run_instagram
        mode = "pilot" if args.pilot else "full"
        posts, comments = args.posts, args.comments      # None 이면 태그별 config·모드 기본값
        if args.reprocess:
            if not reprocess_instagram(cfg, mode):
                return
        else:
            if args.recollect:
                reset_tags(cfg, mode, args.recollect.split(","))
            run_instagram(cfg, mode, posts, comments, args.tags.split(",") if args.tags else None, args.yes)
        try:
            run_ig_analysis(cfg, mode)
        except FileNotFoundError:
            log.error("분석할 인스타 데이터가 없습니다. logs/failures.csv 를 확인하세요.")
        return

    from analysis.run import run_analysis          # noqa: E402
    from crawlers.runner import load_raw, run_collection  # noqa: E402
    from preprocess.clean import run_preprocess    # noqa: E402
    from visualize.plots import run_visualize      # noqa: E402

    if not args.skip_collect:
        run_collection(cfg)
    raw = load_raw(cfg)
    if raw.empty:
        log.error("수집된 데이터가 없습니다. logs/failures.csv 와 API 키 환경변수를 확인하세요.")
        return
    docs, funnel = run_preprocess(raw, cfg)
    if docs.empty:
        log.error("필터 후 남은 문서가 없습니다. outputs/tables/funnel.csv 를 확인하세요.")
        return
    res = run_analysis(docs, funnel, cfg)
    run_visualize(res, cfg)
    log.info("완료 → outputs/figures/, outputs/tables/")


if __name__ == "__main__":
    main()
