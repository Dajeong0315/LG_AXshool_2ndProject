"""스레드 파일럿 분석: 필터 퍼널(0건 단계 점검 포함), 자동 관련도, 대체재 불만 플래그, 파일럿 리포트.

입력: data/raw/threads.csv, outputs/tables/threads_keyword_stats_{mode}.csv (수집 단계 필터 건수)
출력: outputs/tables/threads_funnel / threads_pilot_report / threads_pilot_label_sample / threads_docs_flags .csv,
      config/threads_keywords.csv 갱신(.bak 백업)
"""
import logging
import re
import shutil
from pathlib import Path

import pandas as pd

from analysis.ig_pilot import contains_any, pct
from crawlers.instagram_apify import read_words

log = logging.getLogger("pipeline")
REL_MIN, POSTS_MIN = 0.30, 10            # 판정 제안: 자동 관련도 30% 미만 또는 게시물 10개 미만 → drop 후보
AUTO_NOTE = re.compile(r"^(drop|keep) 후보$")


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def build_funnel(stats: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """게시물·답글 단계별 전/후/제거 건수. 제거가 0건이면 원인을 점검하는 수치를 '점검' 칸에 남기고 경고 로그를 출력한다."""
    s = stats.sum(numeric_only=True)
    n_ad = len(cfg["ad_patterns"])
    rows = []

    def step(kind, name, before, removed, probe):
        after = before - removed
        note = ""
        if before > 0 and removed == 0:
            note = f"⚠ 0건 걸러짐 — 점검: {probe}"
            log.warning("[threads 퍼널] %s '%s' 단계가 %d건 중 0건을 걸렀습니다. %s", kind, name, before, probe)
        elif removed > 0 and probe:
            note = probe
        rows.append({"구분": kind, "단계": name, "전": int(before), "후": int(after), "제거": int(removed), "점검": note})
        return after

    raw = int(s.posts_raw)
    rows.append({"구분": "게시물", "단계": "수집 원본(프로필 행 제외)", "전": raw, "후": raw, "제거": 0, "점검": f"한국어 글 {int(s.posts_ko)}건({pct(s.posts_ko / raw) if raw else '-'})"})
    n = step("게시물", "기간(최근 %d일)" % cfg["period_days"], raw, int(s.old_posts),
             f"날짜 파싱 성공 {int(s.date_ok)}/{raw}건; 액터 입력 postedAfter 로 서버에서도 기간을 제한하므로 0건이면 정상일 수 있음")
    dog_rate = (n - s.nodog_posts) / n if n else 0
    n = step("게시물", "강아지 관련(고양이 전용 제거)", n, int(s.nodog_posts),
             f"이 단계 입력 중 강아지 단어 포함률 {pct(dog_rate)}; 제거 중 고양이 전용 글 {int(s.cat_only_posts)}건")
    n = step("게시물", "광고·협찬", n, int(s.ad_posts), f"광고 패턴 {n_ad + 10}여 개 적용, 매칭 {int(s.ad_posts)}건")
    n = step("게시물", "길이(정제 후 10자 미만)", n, int(s.short_posts), f"이모지·해시태그·멘션 제거 후 10자 미만 {int(s.short_posts)}건")
    n = step("게시물", "중복(url_hash)", n, int(s.dup_posts), "누적 CSV·다른 키워드 기준 중복 0건")
    assert n == int(s.posts_final), (n, s.posts_final)

    rr = int(s.replies_raw)
    rows.append({"구분": "답글", "단계": "수집 원본(필터 통과 게시물의 답글)", "전": rr, "후": rr, "제거": 0, "점검": f"답글 액터 호출 {int(s.replies_run)}회"})
    m = step("답글", "기간", rr, int(s.old_replies), "답글 시각이 없으면 부모 게시일 사용")
    m = step("답글", "강아지 관련(부모 게시물 기준)", m, int(s.nodog_replies),
             "부모 게시물이 이미 강아지 필터를 통과한 글만 답글을 수집하므로 부모 기준으로 통과 — 0건이 정상")
    m = step("답글", "광고·협찬", m, int(s.ad_replies), f"광고 패턴 매칭 {int(s.ad_replies)}건")
    m = step("답글", "길이(정제 후 10자 미만)", m, int(s.short_replies), f"10자 미만 {int(s.short_replies)}건")
    m = step("답글", "중복(url_hash)", m, int(s.dup_replies), "누적 CSV 기준 중복 0건")
    assert m == int(s.replies_final), (m, s.replies_final)
    return pd.DataFrame(rows, columns=["구분", "단계", "전", "후", "제거", "점검"])


def run_threads_analysis(cfg: dict, mode: str = "pilot") -> None:
    raw, tables = Path(cfg["paths"]["raw"]), Path(cfg["paths"]["tables"])
    sfx = "" if mode == "pilot" else f"_{mode}"
    stats = pd.read_csv(tables / f"threads_keyword_stats_{mode}.csv", encoding="utf-8-sig")
    docs = pd.read_csv(raw / "threads.csv", encoding="utf-8-sig", dtype=str).fillna("") if (raw / "threads.csv").exists() \
        else pd.DataFrame(columns=["url_hash", "body", "keyword", "feed_type", "query_group"])
    docs = docs[docs["keyword"].isin(stats["keyword"])].copy()

    rel_words = read_words(cfg["threads"]["relevance_words"])
    cw = cfg["threads"]["complaint_words"]
    docs["relevant"] = docs["body"].map(lambda t: int(bool(contains_any(t, rel_words))))
    docs["complaint"] = [int(bool(contains_any(b, cw))) if g == "substitute" else 0 for b, g in zip(docs["body"], docs["query_group"])]
    _save(docs[["url_hash", "keyword", "feed_type", "relevant", "complaint"]], tables / f"threads_docs_flags{sfx}.csv")

    funnel = build_funnel(stats, cfg)
    _save(funnel, tables / f"threads_funnel{sfx}.csv")

    smp = pd.concat([g.sample(min(10, len(g)), random_state=42) for _, g in docs.groupby("keyword")], ignore_index=True) if len(docs) else docs
    _save(smp[["keyword", "feed_type", "url_hash", "body"]].assign(relevant="") if len(smp) else pd.DataFrame(columns=["keyword", "feed_type", "url_hash", "body", "relevant"]),
          tables / f"threads_pilot_label_sample{sfx}.csv")

    rows = []
    for st in stats.itertuples():
        d = docs[docs["keyword"] == st.keyword]
        rel = d["relevant"].astype(int).mean() if len(d) else 0.0
        comp = d["complaint"].astype(int).mean() if (len(d) and st.query_group == "substitute") else ""
        verdict = "drop 후보" if (rel < REL_MIN or st.posts_final < POSTS_MIN) else "keep 후보"
        rows.append({"keyword": st.keyword, "query_group": st.query_group,
                     "게시물 수(필터 전)": st.posts_raw, "게시물 수(필터 후)": st.posts_final,
                     "답글 수(필터 전)": st.replies_raw, "답글 수(필터 후)": st.replies_final,
                     "제거_기간": st.old_posts + st.old_replies, "제거_강아지": st.nodog_posts + st.nodog_replies,
                     "제거_광고": st.ad_posts + st.ad_replies, "제거_길이": st.short_posts + st.short_replies,
                     "제거_중복": st.dup_posts + st.dup_replies,
                     "한국어 글 비율": round(st.posts_ko / st.posts_raw, 3) if st.posts_raw else "",
                     "자동 관련도": round(rel, 3), "불만 비율": round(comp, 3) if comp != "" else "",
                     "비용(추정,$)": st.cost_usd, "판정 제안": verdict})
    rep = pd.DataFrame(rows)
    _save(rep, tables / ("threads_pilot_report.csv" if mode == "pilot" else f"threads_report{sfx}.csv"))
    if mode == "pilot":
        update_keywords_csv(Path(cfg["threads"]["keywords_csv"]), rep)
    log.info("스레드 분석 완료 → outputs/tables/threads_*.csv")


def update_keywords_csv(path: Path, rep: pd.DataFrame) -> None:
    bak = path.with_suffix(".csv.bak")
    if not bak.exists():                                             # 최초 원본만 보존
        shutil.copy(path, bak)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    for r in rep.to_dict("records"):
        m = df["keyword"] == r["keyword"]
        df.loc[m, "파일럿 게시물 수"] = str(r["게시물 수(필터 후)"])
        df.loc[m, "파일럿 답글 수(필터 후)"] = str(r["답글 수(필터 후)"])
        df.loc[m, "자동 관련도"] = pct(r["자동 관련도"])
        human = df.loc[m, "판정"].str.strip().ne("").any()          # 사람이 판정을 확정했으면 자동 제안은 붙이지 않는다

        def merge(old: str) -> str:
            keep = [x.strip() for x in old.split(";") if x.strip() and not AUTO_NOTE.match(x.strip())]
            return "; ".join(([] if human else [r["판정 제안"]]) + keep)
        df.loc[m, "비고"] = df.loc[m, "비고"].map(merge)
    df.to_csv(path, index=False, encoding="utf-8-sig")
