"""스레드(Threads) 키워드 게시물·답글 수집 (Apify automation-lab 액터 2개).

1단계 automation-lab/threads-scraper(mode=search) → 게시물, 2단계 automation-lab/threads-replies-scraper → 답글.
[개인정보] 작성자 아이디·이름·프로필은 CSV에 저장하지 않는다. (Apify 원본 JSON 에는 남아 있으므로 data/raw/threads_raw/ 는 .gitignore)
[크레딧] 필터에 걸릴 게시물(기간·강아지·광고·길이·중복)은 답글 수집 전에 제외한다.
필터 순서: 기간 → 강아지 관련(고양이 전용 제거) → 광고 → 길이 → 중복. 각 단계가 실제로 몇 건을 걸렀는지 stat 에 남긴다.
"""
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from crawlers.instagram_apify import EXTRA_AD, append_csv, has_dog, letters_only, mask_secrets, read_words, run_actor, scrub
from crawlers.runner import Checkpoint
from crawlers.utils import FailureLog, parse_date, sha
from preprocess.filters import in_period

log = logging.getLogger("pipeline")

SITE = "threads"
ACTOR_POSTS, ACTOR_REPLIES = "automation-lab/threads-scraper", "automation-lab/threads-replies-scraper"
COLUMNS = ["source", "url", "url_hash", "title", "body", "date", "keyword", "feed_type", "rating", "body_from",
           "query_group", "collected_at"]
META_COLUMNS = ["url_hash", "like_count", "reply_count", "repost_count"]
STAT_COLUMNS = ["keyword", "query_group", "posts_raw", "posts_ko", "date_ok", "old_posts", "nodog_posts", "cat_only_posts", "ad_posts",
                "short_posts", "dup_posts", "posts_final", "replies_run", "replies_raw", "old_replies", "nodog_replies", "ad_replies",
                "short_replies", "dup_replies", "replies_final", "cost_usd"]
THREADS_AD = EXTRA_AD + ["링크 클릭", "링크클릭", "프로필 링크"]
CODE_RE = re.compile(r"threads\.(?:com|net)/(?:t/|@[\w.]+/post/)([\w-]+)")


# ---------------------------------------------------------------- 공통 유틸
def code_of(url: str) -> str:
    m = CODE_RE.search(url or "")
    return m.group(1) if m else ""


def post_url(item: dict) -> str:
    return item.get("url") or (f"https://www.threads.com/t/{item['code']}" if item.get("code") else "")


def is_korean(text: str, min_ratio: float = 0.3) -> bool:
    """글자(한글·영문·가나·한자) 중 한글 비율이 min_ratio 이상이면 한국어 글."""
    hangul = len(re.findall(r"[가-힣]", text))
    letters = len(re.findall(r"[A-Za-z가-힣ぁ-んァ-ン一-龥]", text))
    return letters > 0 and hangul / letters >= min_ratio


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip()


def raw_path(raw_dir: Path, keyword: str) -> Path:
    return raw_dir / "threads_raw" / f"{datetime.now():%Y%m%d}_{safe_name(keyword)}.json"


def write_raw(path: Path, payload: dict, keep_old: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_old and path.exists():                   # 같은 날 재수집: 기존 원본은 삭제하지 않고 old/ 로 보관
        (path.parent / "old").mkdir(exist_ok=True)
        dst = path.parent / "old" / path.name
        if dst.exists():
            dst = path.parent / "old" / f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}"
        shutil.move(path, dst)
        log.info("기존 원본 보관: %s → %s", path.name, dst)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), "utf-8")


def dog_terms(cfg: dict) -> list[str]:
    return list(cfg["animal"]["dog_terms"]) + ["퍼피"]


# ---------------------------------------------------------------- 필터·변환
def filter_posts(items: list[dict], kw: str, group: str, cfg: dict, ad_re, seen: set, stat: dict) -> list[dict]:
    """기간 → 강아지(고양이 전용 제거) → 광고 → 길이 → 중복. 처음 걸리는 단계 하나에만 센다."""
    for k in STAT_COLUMNS[2:]:
        stat[k] = 0
    terms, cats, kmin = dog_terms(cfg), cfg["animal"]["cat_terms"], cfg.get("threads", {}).get("korean_min_ratio", 0.3)
    out = []
    for it in items:
        if it.get("type") not in (None, "post"):                     # 프로필 행 등 제외
            continue
        url, text = post_url(it), str(it.get("text") or "")
        if not url:
            continue
        stat["posts_raw"] += 1
        stat["posts_ko"] += is_korean(text, kmin)
        d = parse_date(it.get("date"))
        stat["date_ok"] += d is not None
        h = sha(url)
        if not in_period(d, cfg):
            stat["old_posts"] += 1
        elif not has_dog(text, terms):
            stat["nodog_posts"] += 1
            stat["cat_only_posts"] += any(w in text for w in cats)   # 고양이 단어만 있고 강아지 단어 없음
        elif ad_re.search(text):
            stat["ad_posts"] += 1
        elif len(letters_only(text)) < 10:
            stat["short_posts"] += 1
        elif h in seen:
            stat["dup_posts"] += 1
            log.info("  [중복] %s 는 이미 다른 키워드에서 수집됨 (%s)", h, kw)
        else:
            seen.add(h)
            out.append({"item": it, "url": url, "hash": h, "text": text})
    stat["posts_final"] = len(out)
    return out


def post_rows(posts: list[dict], kw: str, group: str, now: str) -> tuple[list[dict], list[dict]]:
    rows, metas = [], []
    for p in posts:
        it = p["item"]
        d = parse_date(it.get("date"))
        rows.append(dict(source=SITE, url=p["url"], url_hash=p["hash"], title="", body=scrub(p["text"]),
                         date=d.date().isoformat() if d is not None else "", keyword=kw, feed_type="post",
                         rating="", body_from="post", query_group=group, collected_at=now))
        metas.append(dict(url_hash=p["hash"], like_count=it.get("likeCount"), reply_count=it.get("replyCount"),
                          repost_count=it.get("repostCount")))
    return rows, metas


def reply_targets(posts: list[dict], prow: list[dict]) -> dict:
    """답글이 1개 이상인 게시물만: 게시물 code → {url, row, text}."""
    return {code_of(p["url"]): {"url": p["url"], "row": r, "text": p["text"]}
            for p, r in zip(posts, prow) if (p["item"].get("replyCount") or 0) >= 1 and code_of(p["url"])}


def filter_replies(items: list[dict], parents: dict, cfg: dict, ad_re, seen: set, stat: dict, now: str) -> tuple[list[dict], list[dict]]:
    """답글 필터 (게시물과 같은 순서). 강아지 관련은 답글 본문 또는 부모 게시물 기준."""
    terms, rows, metas = dog_terms(cfg), [], []
    for c in items:
        par = parents.get(code_of(c.get("postUrl", "")))
        rid = c.get("replyId")
        if par is None or not rid:
            continue
        text = str(c.get("text") or "")
        stat["replies_raw"] += 1
        d = parse_date(c.get("createdAt")) or parse_date(par["row"]["date"])      # 답글 시각이 없으면 부모 게시일
        if not in_period(d, cfg):
            stat["old_replies"] += 1
        elif not has_dog(f"{text} {par['text']}", terms):
            stat["nodog_replies"] += 1
        elif ad_re.search(text):
            stat["ad_replies"] += 1
        elif len(letters_only(text)) < 10:
            stat["short_replies"] += 1
        else:
            url = f"{par['url']}#r{rid}"
            h = sha(url)
            if h in seen:
                stat["dup_replies"] += 1
                continue
            seen.add(h)
            rows.append({**par["row"], "url": url, "url_hash": h, "body": scrub(text), "feed_type": "reply", "body_from": "reply",
                         "date": d.date().isoformat() if d is not None else par["row"]["date"], "collected_at": now})
            metas.append(dict(url_hash=h, like_count=c.get("likeCount"), reply_count=c.get("replyCount"), repost_count=c.get("repostCount")))
    stat["replies_final"] = len(rows)
    return rows, metas


def load_seen(csv_path: Path) -> set:
    return set(pd.read_csv(csv_path, encoding="utf-8-sig", usecols=["url_hash"])["url_hash"]) if csv_path.exists() else set()


# ---------------------------------------------------------------- 키워드·수량·비용
def select_keywords(cfg: dict, mode: str, only: list[str] | None = None, include_dropped: bool = False) -> pd.DataFrame:
    df = pd.read_csv(Path(cfg["threads"]["keywords_csv"]), encoding="utf-8-sig", dtype=str).fillna("")
    if mode == "full":
        df = df[df["판정"].str.strip().str.lower() == "keep"]
    elif not include_dropped:
        df = df[df["판정"].str.strip().str.lower() != "drop"]
    if only:
        df = df[df["keyword"].isin(only)]
    return df.reset_index(drop=True)


def limits(mode: str, posts: int | None, replies: int | None) -> tuple[int, int]:
    return posts or (30 if mode == "pilot" else 100), replies or 20


def estimate(cfg: dict, plan: list[tuple[int, int]]) -> tuple[int, int, float]:
    """plan: [(키워드별 게시물 수, 게시물당 답글 수)] → 최대 게시물·답글 수와 상한 비용($). 답글은 모든 게시물이 통과해 전부 받는 최악의 경우."""
    pr = cfg["threads"]["price"]
    p, r = sum(a for a, _ in plan), sum(a * b for a, b in plan)
    usd = sum(pr["start_posts"] + a * pr["post"] + pr["start_replies"] + a * b * pr["reply"] for a, b in plan)
    return p, r, usd


def add_cost(stat: dict, cfg: dict) -> None:
    pr = cfg["threads"]["price"]
    stat["cost_usd"] = round(pr["start_posts"] + stat["posts_raw"] * pr["post"]
                             + (pr["start_replies"] + stat["replies_raw"] * pr["reply"] if stat["replies_run"] else 0), 4)


def post_input(cfg: dict, kw: str, n: int) -> dict:
    after = (datetime.now() - timedelta(days=cfg["period_days"])).strftime("%Y-%m-%d")
    return {"mode": "search", "searchQueries": [kw], "searchSort": cfg["threads"].get("search_sort", "recent"),
            "maxPosts": n, "postedAfter": after}


# ---------------------------------------------------------------- 실행
def run_threads(cfg: dict, mode: str, posts_per_kw: int | None = None, replies_per_post: int | None = None,
                only: list[str] | None = None, assume_yes: bool = False) -> bool:
    """수집 실행. 반환값: 새로 수집된 데이터가 있으면 True."""
    from apify_client import ApifyClient
    from dotenv import load_dotenv
    load_dotenv()
    if not os.getenv("APIFY_TOKEN"):
        log.error("APIFY_TOKEN 이 없습니다. pet_needs_pipeline/.env 에 APIFY_TOKEN=... 를 넣어주세요.")
        return False
    kws = select_keywords(cfg, mode, only)
    if kws.empty:
        log.error("수집할 키워드가 없습니다. (threads_keywords.csv 의 판정 컬럼 확인)")
        return False

    raw_dir = Path(cfg["paths"]["raw"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_csv, meta_csv = raw_dir / "threads.csv", raw_dir / "threads_meta.csv"
    tables = Path(cfg["paths"]["tables"])
    tables.mkdir(parents=True, exist_ok=True)
    stat_csv = tables / f"threads_keyword_stats_{mode}.csv"
    ckpt, fails = Checkpoint(raw_dir / "checkpoint.json"), FailureLog(cfg["paths"]["logs"])
    site = f"{SITE}_{mode}"
    todo = [k for k in (str(x).strip() for x in kws["keyword"]) if not (ckpt.done(site, k) or ckpt.is_exhausted(site, k))]
    if not todo:
        log.info("새로 수집할 키워드가 없습니다 (모두 체크포인트에 완료로 기록됨)")
        return False
    plan = [limits(mode, posts_per_kw, replies_per_post) for _ in todo]
    p, r, usd = estimate(cfg, plan)
    print(f"[--{mode}] 새로 수집할 키워드 {len(todo)}개 ({', '.join(todo)}) → 게시물 최대 {p:,}건, 답글 최대 {r:,}건 "
          f"(약 ${usd:,.2f} 상한, 단가는 config threads.price)")
    if mode == "full" and not assume_yes and input("진행할까요? (y/N) ").strip().lower() != "y":
        log.info("취소됨")
        return False

    seen = load_seen(out_csv)
    ad_re = re.compile("|".join(map(re.escape, cfg["ad_patterns"] + THREADS_AD)))
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    groups = dict(zip(kws["keyword"].str.strip(), kws["query_group"].str.strip()))
    stats, got_any = [], False
    for kw in todo:
        group = groups[kw]
        n_posts, n_rep = limits(mode, posts_per_kw, replies_per_post)
        stat = dict.fromkeys(STAT_COLUMNS, 0) | {"keyword": kw, "query_group": group}
        now = datetime.now().isoformat(timespec="seconds")
        try:
            inp = post_input(cfg, kw, n_posts)
            items = run_actor(client, ACTOR_POSTS, inp)
            path = raw_path(raw_dir, kw)
            payload = {"keyword": kw, "posts_input": inp, "posts": items, "replies": []}
            write_raw(path, payload)
            posts = filter_posts(items, kw, group, cfg, ad_re, seen, stat)
            prow, pmeta = post_rows(posts, kw, group, now)
            crow, cmeta = [], []
            parents = reply_targets(posts, prow)
            if parents:
                stat["replies_run"] = 1
                ritems = run_actor(client, ACTOR_REPLIES, {"postUrls": [v["url"] for v in parents.values()], "maxRepliesPerPost": n_rep})
                payload["replies"] = ritems
                write_raw(path, payload, keep_old=False)
                crow, cmeta = filter_replies(ritems, parents, cfg, ad_re, seen, stat, now)
            add_cost(stat, cfg)
            append_csv(out_csv, prow + crow, COLUMNS)
            append_csv(meta_csv, pmeta + cmeta, META_COLUMNS)
            ckpt.update(site, kw, len(prow) + len(crow), seen, True)
            stats.append(stat)
            got_any = got_any or bool(prow or crow)
            log_keyword(stat)
        except KeyboardInterrupt:
            log.warning("사용자 중단 — 완료된 키워드까지 저장됨. 다시 실행하면 이어서 수집합니다.")
            break
        except Exception as e:                   # 키워드 단위 실패는 기록하고 다음 키워드로
            fails.add(SITE, kw, "", mask_secrets(e))

    if stats:
        new = pd.DataFrame(stats, columns=STAT_COLUMNS)
        if stat_csv.exists():
            old = pd.read_csv(stat_csv, encoding="utf-8-sig")
            new = pd.concat([old[~old["keyword"].isin(new["keyword"])], new], ignore_index=True).reindex(columns=STAT_COLUMNS).fillna(0)
        new.to_csv(stat_csv, index=False, encoding="utf-8-sig")
    return got_any


def log_keyword(s: dict) -> None:
    log.info("  [%s] %-12s 게시물 %d→%d (한국어 %d, 기간 -%d 강아지 -%d[고양이 전용 %d] 광고 -%d 길이 -%d 중복 -%d), 답글 %d→%d (기간 -%d 광고 -%d 길이 -%d 중복 -%d)",
             SITE, s["keyword"], s["posts_raw"], s["posts_final"], s["posts_ko"], s["old_posts"], s["nodog_posts"], s["cat_only_posts"],
             s["ad_posts"], s["short_posts"], s["dup_posts"], s["replies_raw"], s["replies_final"], s["old_replies"], s["ad_replies"],
             s["short_replies"], s["dup_replies"])


def reset_keywords(cfg: dict, mode: str, keywords: list[str]) -> None:
    """지정 키워드를 처음부터 다시 수집할 수 있게 초기화 (원본 JSON 은 old/ 로 보관, 누적 CSV 행·체크포인트 항목 제거)."""
    raw = Path(cfg["paths"]["raw"])
    old = raw / "threads_raw" / "old"
    old.mkdir(parents=True, exist_ok=True)
    for kw in keywords:
        for f in (raw / "threads_raw").glob(f"*_{safe_name(kw)}.json"):
            dst = old / f.name
            if dst.exists():
                dst = old / f"{f.stem}_{datetime.now():%H%M%S}{f.suffix}"
            shutil.move(f, dst)
            log.info("원본 보관: %s → %s", f.name, dst)
    main_csv, meta_csv = raw / "threads.csv", raw / "threads_meta.csv"
    if main_csv.exists():
        df = pd.read_csv(main_csv, encoding="utf-8-sig", dtype=str)
        gone = df[df["keyword"].isin(keywords)]["url_hash"]
        df[~df["keyword"].isin(keywords)].to_csv(main_csv, index=False, encoding="utf-8-sig")
        if meta_csv.exists():
            m = pd.read_csv(meta_csv, dtype=str)
            m[~m["url_hash"].isin(gone)].to_csv(meta_csv, index=False, encoding="utf-8-sig")
        log.info("누적 CSV 에서 %s 행 %d개 제거", keywords, len(gone))
    ck = Checkpoint(raw / "checkpoint.json")
    for kw in keywords:
        key = f"{SITE}_{mode}|{kw}"
        ck.state["done"].pop(key, None)
        if key in ck.state["exhausted"]:
            ck.state["exhausted"].remove(key)
    tmp = ck.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ck.state, ensure_ascii=False), "utf-8")
    tmp.replace(ck.path)
    log.info("체크포인트 항목 제거: %s", [f"{SITE}_{mode}|{k}" for k in keywords])
