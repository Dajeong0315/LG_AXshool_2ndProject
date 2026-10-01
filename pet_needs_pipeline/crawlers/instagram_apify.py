"""인스타그램 해시태그 게시물·댓글 수집 (Apify 액터 2개).

1단계 apify/instagram-hashtag-scraper → 게시물, 2단계 apify/instagram-comment-scraper → 댓글.
[개인정보] 작성자 아이디·이름·프로필 이미지·멘션은 CSV에 저장하지 않는다.
           (Apify 원본 JSON에는 남아 있으므로 data/raw/instagram_raw/ 는 .gitignore 처리)
[크레딧]   필터로 걸러질 게시물(광고·중복·비강아지)은 댓글 수집 전에 제외한다.
"""
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from crawlers.runner import Checkpoint
from crawlers.utils import FailureLog, parse_date, sha
from preprocess.filters import in_period

log = logging.getLogger("pipeline")

POST_ACTOR = "apify/instagram-hashtag-scraper"
COMMENT_ACTOR = "apify/instagram-comment-scraper"
SITE = "instagram"
COLUMNS = ["source", "url", "url_hash", "title", "body", "date", "keyword", "feed_type", "rating",
           "body_from", "query_group", "collected_at"]
META_COLUMNS = ["url_hash", "like_count", "comment_count", "is_video"]
STAT_COLUMNS = ["tag", "query_group", "posts_raw", "old_posts", "ad_posts", "hashonly_posts", "dup_posts", "nodog_posts",
                "posts_final", "comments_raw", "ad_comments", "short_comments", "comments_final", "period_days",
                "ad_all", "nonad_dog", "recent_all", "cost_usd"]
REMOVED_COLUMNS = ["url_hash", "tag", "url", "in_period", "caption"]

# config.yaml 의 ad_patterns 에 더해 인스타에서 자주 보이는 표현을 확장
EXTRA_AD = ["협찬", "광고", "체험단", "공구", "제공받아", "원고료", "소정의"]
DOG_RE = re.compile(r"강아지|댕댕|멍멍|반려견|우리\s?애|(?<![가-힣])(?:견|개)(?:[가는도를랑와의]|한테|에게)?(?![가-힣])")  # '개'·'견'은 단독 어절만(몇 개, 개선 오탐 방지)
QTY_RE = re.compile(r"(?:\d+|몇|여러|한|두|세|네)\s*개")     # '3개', '몇 개' 는 강아지 '개'가 아님
TAG_RE, MENTION_RE = re.compile(r"#\S+"), re.compile(r"@[\w.]+")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
SHORTCODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)")


# ---------------------------------------------------------------- 공통 유틸
def mask_secrets(msg) -> str:
    """에러 메시지 속 token/key 쿼리값과 Apify 토큰을 *** 로 가린다."""
    s = re.sub(r"((?:token|key|apikey)=)[^&\s]+", r"\1***", str(msg), flags=re.I)
    s = re.sub(r"apify_api_\w+", "***", s)
    tok = os.getenv("APIFY_TOKEN")
    return s.replace(tok, "***") if tok else s


def read_words(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text("utf-8").splitlines() if l.strip() and not l.startswith("#")]


def scrub(text) -> str:
    """저장용 본문: URL·이메일·전화번호·@멘션(타인 아이디) 제거. 해시태그·이모지는 유지."""
    t = str(text or "")
    for rx in (URL_RE, EMAIL_RE, PHONE_RE, MENTION_RE):
        t = rx.sub(" ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def letters_only(text: str) -> str:
    """이모지·해시태그·멘션·문장부호·공백을 지운 글자(한글/영문/숫자)만 남긴다."""
    t = MENTION_RE.sub(" ", TAG_RE.sub(" ", str(text or "")))
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t)


def has_dog(text: str, terms=()) -> bool:
    """강아지 언급 여부: 기본 단어(DOG_RE) 또는 terms(config animal.dog_terms: 견종·애칭·견주 등, 부분 문자열 일치)."""
    t = QTY_RE.sub(" ", str(text or ""))
    return bool(DOG_RE.search(t)) or any(w in t for w in terms)


def load_restore(cfg: dict) -> set:
    """강아지 단어 필터에 걸렸지만 사람이 검수해 살리기로 한 게시물 url_hash (config instagram.restore_file)."""
    f = Path(cfg.get("instagram", {}).get("restore_file", "config/ig_dogfilter_restore.csv"))
    if not f.exists():
        return set()
    return set(pd.read_csv(f, encoding="utf-8-sig", dtype=str)["url_hash"].dropna().str.strip())


def shortcode(url: str) -> str:
    m = SHORTCODE_RE.search(url or "")
    return m.group(1) if m else ""


def _get(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


# ---------------------------------------------------------------- Apify 호출
def run_actor(client, actor: str, run_input: dict) -> list[dict]:
    run = client.actor(actor).call(run_input=run_input)
    ds = _get(run, "default_dataset_id") or _get(run, "defaultDatasetId")
    items = list(client.dataset(ds).iterate_items())
    usd, rid = _get(run, "usage_total_usd"), _get(run, "id")
    if not usd and rid and hasattr(client, "run"):           # 종료 직후엔 비어 있을 수 있어 한 번 더 조회
        try:
            usd = _get(client.run(rid).get(), "usage_total_usd")
        except Exception:
            usd = None
    if usd and items:                                            # 청구액이 아직 집계 전(0)이면 로그를 남기지 않는다 (틀린 값 방지)
        log.info("  [Apify 실제 비용] %s: %d건 $%.4f (건당 $%.5f = $%.2f/1,000건)",
                 actor.split("/")[-1], len(items), usd, usd / len(items), usd / len(items) * 1000)
    return items


def save_raw(raw_dir: Path, tag: str, kind: str, items: list) -> None:
    d = raw_dir / "instagram_raw"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{datetime.now():%Y%m%d}_{tag}_{kind}.json"
    if f.exists():                                   # 같은 날 재수집(파일럿 → 본수집): 기존 원본은 삭제하지 않고 old/ 로 보관
        (d / "old").mkdir(exist_ok=True)
        dst = d / "old" / f.name
        if dst.exists():
            dst = d / "old" / f"{f.stem}_{datetime.now():%H%M%S}{f.suffix}"
        shutil.move(f, dst)
        log.info("기존 원본 보관: %s → %s", f.name, dst)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=1, default=str), "utf-8")


# ---------------------------------------------------------------- 변환·필터
def post_url(item: dict) -> str:
    return item.get("url") or (f"https://www.instagram.com/p/{item['shortCode']}/" if item.get("shortCode") else "")


def norm_tag(t: str) -> str:
    return re.sub(r"[\s_]", "", t).lower()


def query_tags(cfg: dict) -> set:
    """검색 태그와 변형: 태그 CSV 의 robot 층 태그 전체(정규화). 검색어 자체라서 강아지 근거로 쓰면 순환이 된다."""
    ig = cfg.setdefault("instagram", {})
    if "_query_tags" not in ig:
        try:
            df = pd.read_csv(Path(cfg["paths"].get("ig_tags", "config/hashtags_pilot_v2.csv")), encoding="utf-8-sig", dtype=str)
            ig["_query_tags"] = {norm_tag(t) for t in df.loc[df["query_group"] == "robot", "tag"]}
        except (OSError, KeyError, ValueError):
            ig["_query_tags"] = set()
    return ig["_query_tags"]


def dog_text(cap: str, tag: str, cfg: dict) -> str:
    """강아지 판별에 쓰는 텍스트 (모든 robot 태그 공통): 검색 태그와 그 변형 해시태그만 지우고, 나머지 해시태그 + 본문 전체."""
    excl = query_tags(cfg) | {norm_tag(tag)}
    return re.sub(r"#([^\s#]+)", lambda m: " " if norm_tag(m.group(1)) in excl else m.group(0), cap)


def add_cost(stat: dict, cfg: dict) -> None:
    """추정 비용($): 받은 게시물·댓글 수 × 단가(config instagram.price_per_1000). 스타터 결제 후 단가를 고칠 것."""
    pr = cfg.get("instagram", {}).get("price_per_1000", {"posts": 2.6, "comments": 2.3})
    stat["cost_usd"] = round(stat["posts_raw"] * pr["posts"] / 1000 + stat["comments_raw"] * pr["comments"] / 1000, 4)


def limits(cfg: dict, mode: str, tag: str, posts: int | None, comments: int | None) -> tuple[int, int]:
    """태그별 (게시물 수, 게시물당 댓글 수): CLI 지정값 > config instagram.tag_overrides > 모드 기본값."""
    ov = cfg.get("instagram", {}).get("tag_overrides", {}).get(tag, {})
    return posts or ov.get("posts") or (30 if mode == "pilot" else 100), comments or ov.get("comments") or 30


def filter_posts(items: list[dict], tag: str, group: str, ad_re, seen: set, stat: dict, cfg: dict, days: int,
                 removed: list | None = None) -> list[dict]:
    """게시물 필터: 기간 → 광고 → 해시태그만 캡션 → 중복 → (robot) 강아지 단어. 통과한 게시물 dict 목록.
    강아지 단어 검사 범위는 dog_text 참조(태그 자체에 '강아지'가 들어 있어 항상 통과하는 것을 방지)."""
    out, per, restore = [], {**cfg, "period_days": days}, load_restore(cfg)
    terms = cfg.get("animal", {}).get("dog_terms", []) if cfg.get("instagram", {}).get("use_animal_dog_terms", True) else []
    for k in STAT_COLUMNS[2:]:
        stat[k] = 0
    stat["posts_raw"], stat["period_days"] = len(items), days
    for it in items:
        url, cap = post_url(it), str(it.get("caption") or "")
        if not url:
            continue
        h = sha(url)
        d = parse_date(it.get("timestamp"))
        fresh, is_ad = in_period(d, per), bool(ad_re.search(cap))
        dog_ok = group != "robot" or h in restore or has_dog(dog_text(cap, tag, cfg), terms)
        stat["ad_all"] += is_ad                                  # 아래 세 집계는 필터 순서·기간과 무관한 리포트용 수치
        stat["recent_all"] += in_period(d, cfg)
        stat["nonad_dog"] += (not is_ad) and dog_ok
        if removed is not None and not dog_ok and not is_ad and len(letters_only(cap)) >= 10:
            # 눈으로 검수할 샘플용: 기간 필터에 먼저 걸렸더라도 '강아지 단어 없음' 판정 게시물은 모두 기록 (건수 집계와 무관)
            removed.append(dict(url_hash=h, tag=tag, url=url, in_period=fresh, caption=scrub(cap)))
        if not fresh:
            stat["old_posts"] += 1
        elif is_ad:
            stat["ad_posts"] += 1
        elif len(letters_only(cap)) < 10:
            stat["hashonly_posts"] += 1
        elif h in seen:
            stat["dup_posts"] += 1
            log.info("  [중복] %s 는 이미 다른 태그에서 수집됨 (%s)", h, tag)
        elif not dog_ok:
            stat["nodog_posts"] += 1
        else:
            seen.add(h)
            out.append({"item": it, "url": url, "hash": h, "caption": cap})
    stat["posts_final"] = len(out)
    return out


def select_posts(items, tag, group, cfg, ad_re, seen: set, stat: dict, removed: list) -> list[dict]:
    """기본 기간(period_days=365) 적용. 반응 태그(expand_tags)는 결과가 expand_min_posts 미만이면 expand_days(730)로 확장."""
    ig = cfg.get("instagram", {})
    days = ig.get("period_days", cfg["period_days"])      # 인스타 전용 기간 (없으면 전역 period_days)
    trial, rm = set(seen), []
    posts = filter_posts(items, tag, group, ad_re, trial, stat, cfg, days, rm)
    if group == "robot" and tag in ig.get("expand_tags", []) and len(posts) < ig.get("expand_min_posts", 10):
        days = ig.get("expand_days", 730)
        trial, rm = set(seen), []
        posts = filter_posts(items, tag, group, ad_re, trial, stat, cfg, days, rm)
    seen.update(trial)
    removed.extend(rm)
    return posts


def comment_targets(posts: list[dict], prow: list[dict]) -> dict:
    """댓글이 1개 이상인 게시물만: shortCode → 게시물 행."""
    return {shortcode(p["url"]): r for p, r in zip(posts, prow) if (p["item"].get("commentsCount") or 0) >= 1}


def post_rows(posts: list[dict], tag: str, group: str, now: str) -> tuple[list[dict], list[dict]]:
    rows, metas = [], []
    for p in posts:
        it = p["item"]
        d = parse_date(it.get("timestamp"))
        rows.append(dict(source=SITE, url=p["url"], url_hash=p["hash"], title="", body=scrub(p["caption"]),
                         date=d.date().isoformat() if d is not None else "", keyword=tag, feed_type="post",
                         rating="", body_from="caption", query_group=group, collected_at=now))
        metas.append(dict(url_hash=p["hash"], like_count=it.get("likesCount"), comment_count=it.get("commentsCount"),
                          is_video=str(it.get("type", "")).lower() == "video"))
    return rows, metas


def comment_rows(items: list[dict], parents: dict, ad_re, seen: set, stat: dict, now: str) -> tuple[list[dict], list[dict]]:
    """댓글 필터: 광고 → 정제 후 10자 미만. parents: shortCode → 게시물 행(dict)."""
    rows, metas = [], []
    stat["comments_raw"] = len(items)
    for c in items:
        text = str(c.get("text") or "")
        par = parents.get(shortcode(c.get("postUrl", "")))
        if par is None or not c.get("id"):
            continue
        if ad_re.search(text):
            stat["ad_comments"] += 1
        elif len(letters_only(text)) < 10:
            stat["short_comments"] += 1
        else:
            url = f"{par['url']}#c{c['id']}"
            h = sha(url)
            if h in seen:
                continue
            seen.add(h)
            d = parse_date(c.get("timestamp"))
            rows.append({**par, "url": url, "url_hash": h, "body": scrub(text), "feed_type": "comment",
                         "body_from": "comment", "date": d.date().isoformat() if d is not None else par["date"],
                         "collected_at": now})
            metas.append(dict(url_hash=h, like_count=c.get("likesCount"), comment_count=c.get("repliesCount"), is_video=""))
    stat["comments_final"] = len(rows)
    return rows, metas


def append_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    if rows:
        pd.DataFrame(rows, columns=columns).to_csv(path, mode="a", header=not path.exists(), index=False, encoding="utf-8-sig")


def load_seen(csv_path: Path) -> set:
    return set(pd.read_csv(csv_path, encoding="utf-8-sig", usecols=["url_hash"])["url_hash"]) if csv_path.exists() else set()


# ---------------------------------------------------------------- 태그 목록·실행
def select_tags(cfg: dict, mode: str, only: list[str] | None = None, include_dropped: bool = False) -> pd.DataFrame:
    df = pd.read_csv(Path(cfg["paths"].get("ig_tags", "config/hashtags_pilot_v2.csv")), encoding="utf-8-sig")
    df = df[df["수집 여부"].astype(str).str.strip() == "수집"]
    if mode == "pilot" and not include_dropped:                  # 판정이 drop 으로 확정된 태그는 다시 수집하지 않는다
        df = df[df["판정"].astype(str).str.strip().str.lower() != "drop"]
    if mode == "full":
        df = df[df["판정"].astype(str).str.strip().str.lower() == "keep"]
    if only:
        df = df[df["tag"].isin(only)]
    return df.reset_index(drop=True)


def estimate(cfg: dict, plan: list[tuple[int, int]]) -> tuple[int, int, float]:
    """plan: [(태그별 게시물 수, 게시물당 댓글 수)] → 최대 게시물·댓글 수와 상한 비용($)."""
    pr = cfg.get("instagram", {}).get("price_per_1000", {"posts": 2.6, "comments": 2.3})
    p, c = sum(a for a, _ in plan), sum(a * b for a, b in plan)
    return p, c, p * pr["posts"] / 1000 + c * pr["comments"] / 1000


def run_instagram(cfg: dict, mode: str, posts_per_tag: int | None = None, comments_per_post: int | None = None,
                  only_tags: list[str] | None = None, assume_yes: bool = False) -> bool:
    """수집 실행. 반환값: 새로 수집된 데이터가 있으면 True."""
    from apify_client import ApifyClient
    from dotenv import load_dotenv
    load_dotenv()
    if not os.getenv("APIFY_TOKEN"):
        log.error("APIFY_TOKEN 이 없습니다. pet_needs_pipeline/.env 에 APIFY_TOKEN=... 를 넣어주세요.")
        return False

    tags = select_tags(cfg, mode, only_tags)
    if tags.empty:
        log.error("수집할 태그가 없습니다. (수집 여부/판정 컬럼 확인)")
        return False
    raw_dir = Path(cfg["paths"]["raw"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_csv, meta_csv = raw_dir / "instagram.csv", raw_dir / "instagram_meta.csv"
    stat_csv = Path(cfg["paths"]["tables"]) / f"ig_tag_stats_{mode}.csv"
    Path(cfg["paths"]["tables"]).mkdir(parents=True, exist_ok=True)
    ckpt, fails = Checkpoint(raw_dir / "checkpoint.json"), FailureLog(cfg["paths"]["logs"])
    site = f"{SITE}_{mode}"                      # 파일럿과 본수집 체크포인트를 분리
    todo = [t for t in (str(x).strip() for x in tags["tag"]) if not (ckpt.done(site, t) or ckpt.is_exhausted(site, t))]
    if not todo:
        log.info("새로 수집할 태그가 없습니다 (모두 체크포인트에 완료로 기록됨)")
        return False
    plan = [limits(cfg, mode, t, posts_per_tag, comments_per_post) for t in todo]
    p, c, usd = estimate(cfg, plan)
    print(f"[--{mode}] 새로 수집할 태그 {len(todo)}개 ({', '.join(todo)}) → 게시물 최대 {p:,}건, 댓글 최대 {c:,}건 "
          f"(약 ${usd:,.2f} 상한, 단가는 config instagram.price_per_1000)")
    if mode == "full" and not assume_yes and input("진행할까요? (y/N) ").strip().lower() != "y":
        log.info("취소됨")
        return False
    seen = load_seen(out_csv)                    # 누적 CSV 기준 url_hash 중복 제거
    ad_re = re.compile("|".join(map(re.escape, cfg["ad_patterns"] + EXTRA_AD)))
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    stats, got_any = [], False

    for _, r in tags.iterrows():
        tag, group = str(r["tag"]).strip(), str(r["query_group"]).strip()
        if ckpt.done(site, tag) or ckpt.is_exhausted(site, tag):
            log.info("  [%s] %s 이미 수집됨 → 건너뜀", SITE, tag)
            continue
        stat = dict.fromkeys(STAT_COLUMNS, 0) | {"tag": tag, "query_group": group}
        now = datetime.now().isoformat(timespec="seconds")
        n_posts, n_comm = limits(cfg, mode, tag, posts_per_tag, comments_per_post)
        try:
            items = run_actor(client, POST_ACTOR, {"hashtags": [tag], "resultsType": "posts", "resultsLimit": n_posts})
            save_raw(raw_dir, tag, "posts", items)
            removed = []
            posts = select_posts(items, tag, group, cfg, ad_re, seen, stat, removed)
            prow, pmeta = post_rows(posts, tag, group, now)
            crow, cmeta = [], []
            parents = comment_targets(posts, prow)
            if parents:
                urls = [r["url"] for r in parents.values()]
                citems = run_actor(client, COMMENT_ACTOR, {"directUrls": urls, "resultsLimit": n_comm,
                                                           "includeNestedComments": False})
                save_raw(raw_dir, tag, "comments", citems)
                crow, cmeta = comment_rows(citems, parents, ad_re, seen, stat, now)
            add_cost(stat, cfg)
            append_csv(out_csv, prow + crow, COLUMNS)
            append_csv(raw_dir / "instagram_dogfilter_removed.csv", removed, REMOVED_COLUMNS)
            append_csv(meta_csv, pmeta + cmeta, META_COLUMNS)
            ckpt.update(site, tag, len(prow) + len(crow), seen, True)
            stats.append(stat)
            got_any = got_any or bool(prow or crow)
            log_tag(stat)
        except KeyboardInterrupt:
            log.warning("사용자 중단 — 완료된 태그까지 저장됨. 다시 실행하면 이어서 수집합니다.")
            break
        except Exception as e:                   # 태그 단위 실패는 기록하고 다음 태그로
            fails.add(SITE, tag, "", mask_secrets(e))

    if stats:                                    # 태그별 필터 건수 누적 (태그 단위로 덮어쓰기)
        new = pd.DataFrame(stats, columns=STAT_COLUMNS)
        if stat_csv.exists():
            old = pd.read_csv(stat_csv, encoding="utf-8-sig")
            new = pd.concat([old[~old["tag"].isin(new["tag"])], new], ignore_index=True).reindex(columns=STAT_COLUMNS).fillna(0)
        new.to_csv(stat_csv, index=False, encoding="utf-8-sig")
    return got_any


def log_tag(stat: dict) -> None:
    log.info("  [%s] %-14s 게시물 %d→%d (기간 %s일), 댓글 %d→%d | 탈락 기간 %d 광고 %d/%d 해시태그만 %d 중복 %d 비강아지 %d 짧은댓글 %d",
             SITE, stat["tag"], stat["posts_raw"], stat["posts_final"], stat["period_days"], stat["comments_raw"],
             stat["comments_final"], stat["old_posts"], stat["ad_posts"], stat["ad_comments"], stat["hashonly_posts"],
             stat["dup_posts"], stat["nodog_posts"], stat["short_comments"])


# ---------------------------------------------------------------- 재처리 (Apify 호출 없이 원본 JSON에서 다시 변환)
def preserve_v1(cfg: dict, mode: str) -> None:
    """재처리 전 결과를 _v1 로 보존. 이미 _v1 이 있으면 덮어쓰지 않는다(여러 번 재처리해도 v1 은 최초 상태)."""
    raw, tables = Path(cfg["paths"]["raw"]), Path(cfg["paths"]["tables"])
    tags_csv = Path(cfg["paths"].get("ig_tags", "config/hashtags_pilot_v2.csv"))
    pairs = [(tags_csv, tags_csv.with_name("hashtags_pilot_v2_v1result.csv"))]
    v1_files = ("ig_funnel", "ig_pilot_report", "ig_pilot_label_sample", "ig_reaction_types", "ig_reaction_summary",
                "ig_reaction_label_sample", "ig_substitute_emotion", f"ig_tag_stats_{mode}")   # v1 시점에 존재하던 산출물만
    pairs += [(tables / f"{n}.csv", tables / f"{n}_v1.csv") for n in v1_files]
    (raw / "v1").mkdir(parents=True, exist_ok=True)     # load_raw 는 data/raw/*.csv 만 읽으므로 하위 폴더에 보관
    pairs += [(raw / n, raw / "v1" / n) for n in ("instagram.csv", "instagram_meta.csv", "instagram_dogfilter_removed.csv")]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
            log.info("v1 보존: %s → %s", src, dst)


def latest_json(raw_dir: Path, tag: str, kind: str) -> list:
    files = sorted((raw_dir / "instagram_raw").glob(f"*_{tag}_{kind}.json"))
    return json.loads(files[-1].read_text("utf-8")) if files else []


def reprocess_instagram(cfg: dict, mode: str = "pilot") -> bool:
    """data/raw/instagram_raw/ 의 원본 JSON 으로 변환·필터를 다시 실행 (Apify 호출·체크포인트 변경 없음)."""
    if mode != "pilot":
        log.error("--reprocess 는 --pilot 에서만 지원합니다 (본수집 데이터는 누적 CSV 에 이미 들어 있어 원본으로 재구성하면 다른 태그 행이 사라집니다).")
        return False
    preserve_v1(cfg, mode)
    raw_dir, tables = Path(cfg["paths"]["raw"]), Path(cfg["paths"]["tables"])
    tables.mkdir(parents=True, exist_ok=True)
    for n in ("instagram.csv", "instagram_meta.csv", "instagram_dogfilter_removed.csv"):
        (raw_dir / n).unlink(missing_ok=True)
    ad_re = re.compile("|".join(map(re.escape, cfg["ad_patterns"] + EXTRA_AD)))
    seen, stats, now = set(), [], datetime.now().isoformat(timespec="seconds")
    for _, r in select_tags(cfg, mode, include_dropped=True).iterrows():
        tag, group = str(r["tag"]).strip(), str(r["query_group"]).strip()
        items = latest_json(raw_dir, tag, "posts")
        if not items:
            continue
        stat, removed = dict.fromkeys(STAT_COLUMNS, 0) | {"tag": tag, "query_group": group}, []
        posts = select_posts(items, tag, group, cfg, ad_re, seen, stat, removed)
        prow, pmeta = post_rows(posts, tag, group, now)
        parents = comment_targets(posts, prow)
        citems = [c for c in latest_json(raw_dir, tag, "comments") if shortcode(c.get("postUrl", "")) in parents]
        crow, cmeta = comment_rows(citems, parents, ad_re, seen, stat, now)
        add_cost(stat, cfg)
        append_csv(raw_dir / "instagram.csv", prow + crow, COLUMNS)
        append_csv(raw_dir / "instagram_meta.csv", pmeta + cmeta, META_COLUMNS)
        append_csv(raw_dir / "instagram_dogfilter_removed.csv", removed, REMOVED_COLUMNS)
        stats.append(stat)
        log_tag(stat)
    pd.DataFrame(stats, columns=STAT_COLUMNS).to_csv(tables / f"ig_tag_stats_{mode}.csv", index=False, encoding="utf-8-sig")
    return bool(stats)


def reset_tags(cfg: dict, mode: str, tags: list[str]) -> None:
    """지정 태그를 처음부터 다시 수집할 수 있게 초기화. 원본 JSON 은 instagram_raw/old/ 로 옮겨 보관(삭제 안 함),
    누적 CSV(본문·메타·강아지 필터 제거 목록)에서 그 태그 행만 빼고, 체크포인트 항목을 지운다. 다른 태그는 건드리지 않는다."""
    raw = Path(cfg["paths"]["raw"])
    old = raw / "instagram_raw" / "old"
    old.mkdir(parents=True, exist_ok=True)
    for tag in tags:
        for f in list((raw / "instagram_raw").glob(f"*_{tag}_posts.json")) + list((raw / "instagram_raw").glob(f"*_{tag}_comments.json")):
            dst = old / f.name
            if dst.exists():
                dst = old / f"{f.stem}_{datetime.now():%H%M%S}{f.suffix}"
            shutil.move(f, dst)
            log.info("원본 보관: %s → %s", f.name, dst)
    main_csv, meta_csv, rm_csv = raw / "instagram.csv", raw / "instagram_meta.csv", raw / "instagram_dogfilter_removed.csv"
    if main_csv.exists():
        df = pd.read_csv(main_csv, encoding="utf-8-sig", dtype=str)
        gone = df[df["keyword"].isin(tags)]["url_hash"]
        df[~df["keyword"].isin(tags)].to_csv(main_csv, index=False, encoding="utf-8-sig")
        if meta_csv.exists():
            m = pd.read_csv(meta_csv, dtype=str)
            m[~m["url_hash"].isin(gone)].to_csv(meta_csv, index=False, encoding="utf-8-sig")
        log.info("누적 CSV 에서 %s 행 %d개 제거", tags, len(gone))
    if rm_csv.exists():
        r = pd.read_csv(rm_csv, encoding="utf-8-sig", dtype=str)
        r[~r["tag"].isin(tags)].to_csv(rm_csv, index=False, encoding="utf-8-sig")
    ck = Checkpoint(raw / "checkpoint.json")
    for tag in tags:
        key = f"{SITE}_{mode}|{tag}"
        ck.state["done"].pop(key, None)
        if key in ck.state["exhausted"]:
            ck.state["exhausted"].remove(key)
    tmp = ck.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ck.state, ensure_ascii=False), "utf-8")
    tmp.replace(ck.path)
    log.info("체크포인트 항목 제거: %s", [f"{SITE}_{mode}|{t}" for t in tags])
