"""인스타 파일럿 분석: 자동 관련도, 강아지 반응 유형 라벨링(robot), 대체재 감정 표현(substitute), 파일럿 리포트.

입력: data/raw/instagram.csv, outputs/tables/ig_tag_stats_{mode}.csv (수집 단계 필터 건수)
출력: outputs/tables/ig_funnel / ig_reaction_types / ig_reaction_summary / ig_reaction_label_sample /
      ig_substitute_emotion / ig_pilot_label_sample / ig_pilot_report .csv, config/hashtags_pilot_v2.csv 갱신(.bak 백업)
"""
import logging
import re
import shutil
from pathlib import Path

import pandas as pd
import yaml

from crawlers.instagram_apify import read_words

log = logging.getLogger("pipeline")
LABEL_KO = {"fear": "공포", "curious": "호기심", "play": "놀이", "indifferent": "무관심", "adapted": "적응"}
HIT_MIN = 0.10                       # 교집합 태그: 강아지 적중률 10% 이상이면 본수집 후보
REL_MIN, POSTS_MIN = 0.30, 10        # 판정 제안 기준: 자동 관련도 30% 미만 또는 게시물 10개 미만 → drop 후보


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------- 관련도·라벨링
def contains_any(text: str, words) -> list[str]:
    t = str(text or "").lower()
    return [w for w in words if w.lower() in t]


def match_reaction(text: str, rules: dict) -> dict[str, list[str]]:
    """라벨 → 매칭 단어. 단어 앞뒤에 부정 표현('안 무서워', '짖지 않아')이 붙은 매칭은 제외."""
    neg = rules.get("negation", {})
    prefixes, suffix_rx = neg.get("neg_prefix", []), re.compile(neg.get("neg_suffix_regex", "(?!)"))
    exempt = set(neg.get("exempt_labels", []))
    out = {}
    for label, words in rules["labels"].items():
        hit = []
        for w in words:
            for m in re.finditer(re.escape(w), text):
                if label not in exempt:
                    before = text[max(0, m.start() - 4):m.start()].rstrip()
                    after = text[m.end():m.end() + 8]
                    if any(before.endswith(p) for p in prefixes) or suffix_rx.match(after):
                        continue
                hit.append(w)
                break
        if hit:
            out[label] = hit
    return out


def label_reactions(robot: pd.DataFrame, rules: dict) -> pd.DataFrame:
    rows = []
    for r in robot.itertuples():
        m = match_reaction(str(r.body), rules)
        rows.append(dict(url_hash=r.url_hash, tag=r.keyword, feed_type=r.feed_type,
                         labels="|".join(m) if m else "unknown",
                         matched_words="|".join(w for ws in m.values() for w in ws)))
    return pd.DataFrame(rows, columns=["url_hash", "tag", "feed_type", "labels", "matched_words"])


def reaction_summary(types: pd.DataFrame) -> pd.DataFrame:
    def one(g, tag, ft):
        n = len(g)
        row = {"tag": tag, "feed_type": ft, "n_docs": n}
        for lb in LABEL_KO:
            row[lb] = round(g["labels"].str.split("|").map(lambda x: lb in x).sum() / n, 3) if n else 0
        row["unknown"] = round((g["labels"] == "unknown").mean(), 3) if n else 0
        return row
    rows = [one(g, t, "all") for t, g in types.groupby("tag")]
    rows += [one(types if ft == "all" else types[types.feed_type == ft], "전체", ft) for ft in ("all", "post", "comment")]
    return pd.DataFrame(rows)


def label_sample(types: pd.DataFrame, docs: pd.DataFrame) -> pd.DataFrame:
    body = docs.set_index("url_hash")["body"]
    parts = []
    for lb in list(LABEL_KO) + ["unknown"]:
        sub = types[types["labels"].str.split("|").map(lambda x: lb in x)]
        if len(sub):
            parts.append(sub.sample(min(10, len(sub)), random_state=42).assign(rule_label=lb))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=list(types.columns) + ["rule_label"])
    out["body"] = out["url_hash"].map(body)
    out["human_label"] = ""
    return out[["rule_label", "url_hash", "tag", "feed_type", "matched_words", "body", "human_label"]]


# ---------------------------------------------------------------- 대체재 감정
def substitute_emotion(sub: pd.DataFrame, words: list[str]) -> pd.DataFrame:
    n, rows = len(sub), []
    for w in words:
        hit = sub[sub["body"].str.contains(re.escape(w), na=False)]
        sents = []
        for b in hit["body"]:
            sents += [s.strip() for s in re.split(r"[.!?\n]+", str(b)) if w in s and 8 <= len(s.strip()) <= 120]
        sents = list(dict.fromkeys(sents))
        pick = pd.Series(sents).sample(min(3, len(sents)), random_state=42).tolist() if sents else []
        rows.append({"emotion_word": w, "doc_count": len(hit), "ratio": round(len(hit) / n, 3) if n else 0,
                     **{f"sample_{i + 1}": (pick[i] if i < len(pick) else "") for i in range(3)}})
    return pd.DataFrame(rows).sort_values("doc_count", ascending=False)


# ---------------------------------------------------------------- 리포트
def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def run_ig_analysis(cfg: dict, mode: str = "pilot") -> None:
    raw, tables = Path(cfg["paths"]["raw"]), Path(cfg["paths"]["tables"])
    sfx = "" if mode == "pilot" else f"_{mode}"                     # full 모드 결과는 _full 파일로 따로 저장 (파일럿 결과 보존)
    docs = pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig", dtype=str).fillna("")
    stats = pd.read_csv(tables / f"ig_tag_stats_{mode}.csv", encoding="utf-8-sig")
    docs = docs[docs["keyword"].isin(stats["tag"])]                 # 이번 모드에서 수집한 태그만
    rel_words = read_words("config/ig_relevance_words.txt")
    docs["relevant"] = docs["body"].map(lambda t: bool(contains_any(t, rel_words)))

    # 필터 깔때기: 게시물과 댓글을 나눠 단계별 건수 기록 (걸러진 게시물의 댓글은 수집 전에 제외됨)
    s = stats.sum(numeric_only=True)
    pr = int(s.posts_raw)
    funnel = [("[게시물] 수집 원본", pr), ("[게시물] 기간 필터 후", pr - int(s.old_posts))]
    funnel.append(("[게시물] 광고 제거 후", funnel[-1][1] - int(s.ad_posts)))
    funnel.append(("[게시물] 해시태그만 캡션 제거 후", funnel[-1][1] - int(s.hashonly_posts)))
    funnel.append(("[게시물] 중복 제거 후", funnel[-1][1] - int(s.dup_posts)))
    funnel.append(("[게시물] robot 강아지 단어 필터 후" if mode == "pilot" else "[게시물] 최종 통과", int(s.posts_final)))
    cr = int(s.comments_raw)
    funnel += [("[댓글] 수집 원본", cr), ("[댓글] 광고 제거 후", cr - int(s.ad_comments)),
               ("[댓글] 10자 미만 제거 후", int(s.comments_final))]
    funnel_df = pd.DataFrame(funnel, columns=["단계", "문서 수"])
    _save(funnel_df, tables / f"ig_funnel{sfx}.csv")

    # 강아지 단어 필터로 제거된 게시물 10건 (눈으로 확인용)
    rm_path = raw / "instagram_dogfilter_removed.csv"
    rm = pd.read_csv(rm_path, encoding="utf-8-sig", dtype=str).fillna("") if rm_path.exists() else pd.DataFrame(columns=["url_hash", "tag", "url", "caption"])
    rm = rm[rm["tag"].isin(stats["tag"])]
    _save(rm.sample(min(10, len(rm)), random_state=42) if len(rm) else rm, tables / f"ig_dogfilter_removed_sample{sfx}.csv")

    summ = pd.DataFrame()
    if mode == "pilot" or (docs["query_group"] == "robot").any():
        # 반응 유형 (robot)
        rules = yaml.safe_load(Path("config/ig_reaction_dict.yaml").read_text("utf-8"))
        robot = docs[docs["query_group"] == "robot"]
        types = label_reactions(robot, rules)
        _save(types, tables / f"ig_reaction_types{sfx}.csv")
        summ = reaction_summary(types) if len(types) else pd.DataFrame()
        _save(summ, tables / f"ig_reaction_summary{sfx}.csv")
        _save(label_sample(types, docs) if len(types) else pd.DataFrame(), tables / f"ig_reaction_label_sample{sfx}.csv")


    # 대체재 감정 (substitute)
    sub = docs[docs["query_group"] == "substitute"]
    emo = substitute_emotion(sub, read_words("config/ig_emotion_words.txt"))
    _save(emo, tables / f"ig_substitute_emotion{sfx}.csv")
    if mode != "pilot" and (tables / "ig_substitute_emotion.csv").exists():          # 파일럿 대비 순위 변화
        _save(emotion_rank_change(pd.read_csv(tables / "ig_substitute_emotion.csv"), emo), tables / f"ig_emotion_rank_change{sfx}.csv")

    # 수동 관련도 라벨링용 샘플: 태그별 10건
    smp = pd.concat([g.sample(min(10, len(g)), random_state=42) for _, g in docs.groupby("keyword")], ignore_index=True) if len(docs) else docs
    smp = smp.rename(columns={"keyword": "tag"})[["tag", "feed_type", "url_hash", "body"]].assign(relevant="")
    _save(smp, tables / f"ig_pilot_label_sample{sfx}.csv")

    # 파일럿 리포트 + hashtags_pilot_v2.csv 갱신
    rows = []
    for st in stats.itertuples():
        d = docs[docs["keyword"] == st.tag]
        rel = d["relevant"].mean() if len(d) else 0.0
        rt = summ[(summ.tag == st.tag) & (summ.feed_type == "all")] if len(summ) else pd.DataFrame()
        ratio = "/".join(f"{LABEL_KO[k]} {pct(rt.iloc[0][k])}" for k in LABEL_KO) if len(rt) else ""
        nonad = st.posts_raw - st.ad_all
        hit = st.nonad_dog / nonad if nonad > 0 else 0.0               # 강아지 적중률 = 강아지 판별 통과 / 광고 제거 후 (기간 무관)
        if st.tag in cfg.get("instagram", {}).get("intersection_tags", []):
            verdict = "본수집 후보" if hit >= HIT_MIN else "drop 후보"      # 교집합 태그는 적중률 기준
        else:
            verdict = "drop 후보" if (rel < REL_MIN or st.posts_final < POSTS_MIN) else "keep 후보"
        rows.append({"tag": st.tag, "query_group": st.query_group, "게시물 수(필터 전)": st.posts_raw,
                     "게시물 수(필터 후)": st.posts_final, "댓글 수(필터 전)": st.comments_raw,
                     "댓글 수(필터 후)": st.comments_final, "광고 제거 수": st.ad_posts + st.ad_comments,
                     "자동 관련도": round(rel, 3), "반응 라벨 비율": ratio, "판정 제안": verdict,
                     "적용 기간(일)": int(st.period_days) or cfg["period_days"],
                     "광고 비율": round(st.ad_all / st.posts_raw, 3) if st.posts_raw else "",
                     "강아지 적중률": round(hit, 3) if st.query_group == "robot" else "",
                     "최근 1년 게시물 비율": round(st.recent_all / st.posts_raw, 3) if st.posts_raw else "",
                     "비용(추정,$)": st.cost_usd,
                     "강아지 통과 게시물 1개당 비용($)": round(st.cost_usd / st.posts_final, 4) if st.posts_final else ""})
    rep = pd.DataFrame(rows)
    _save(rep, tables / ("ig_pilot_report.csv" if mode == "pilot" else f"ig_report{sfx}.csv"))
    if mode == "pilot":                                                              # 파일럿 결과 컬럼·비교표는 파일럿에서만 갱신
        update_hashtag_csv(Path(cfg["paths"].get("ig_tags", "config/hashtags_pilot_v2.csv")), rep, cfg)
        write_compare(tables, rep, funnel_df, summ)
        write_intersection_compare(tables, stats, rep, cfg)
    write_source_decision(cfg)
    log.info("인스타 분석 완료 → outputs/tables/ig_*.csv")


def update_hashtag_csv(path: Path, rep: pd.DataFrame, cfg: dict) -> None:
    bak = path.with_suffix(".csv.bak")
    if not bak.exists():                                             # 최초 원본만 보존
        shutil.copy(path, bak)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    col_ratio = next(c for c in df.columns if c.startswith("반응 라벨 비율"))
    for r in rep.to_dict("records"):
        m = df["tag"] == r["tag"]
        df.loc[m, "파일럿 게시물 수"] = str(r["게시물 수(필터 후)"])
        df.loc[m, "파일럿 댓글 수(필터 후)"] = str(r["댓글 수(필터 후)"])
        df.loc[m, "자동 관련도"] = pct(r["자동 관련도"])
        df.loc[m, col_ratio] = r["반응 라벨 비율"]
        note = f"기간 {r['적용 기간(일)']}일 확장(반응은 제품 버전과 무관)" if r["적용 기간(일)"] > cfg["period_days"] else ""
        human = df.loc[m, "판정"].str.strip().ne("").any()             # 사람이 판정을 확정했으면 자동 판정 제안은 붙이지 않는다

        def merge(old: str) -> str:                                  # 이전 판정·확장 문구는 갈아끼우고 사람이 쓴 비고는 유지
            keep = [x.strip() for x in old.split(";") if x.strip() and not re.match(r"^(drop|keep|본수집) 후보$|^기간 \d+일 확장", x.strip())]
            return "; ".join(([] if human else [r["판정 제안"]]) + keep + ([note] if note else []))
        df.loc[m, "비고"] = df.loc[m, "비고"].map(merge)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_compare(tables: Path, rep: pd.DataFrame, funnel: pd.DataFrame, summ: pd.DataFrame) -> None:
    """v1(_v1 파일) vs 현재 결과: 필터 단계별 건수, 태그별 수량·자동 관련도, 반응 라벨 비율."""
    f1p, r1p, s1p = (tables / f"{n}_v1.csv" for n in ("ig_funnel", "ig_pilot_report", "ig_reaction_summary"))
    if not (f1p.exists() and r1p.exists()):
        return
    rows = []

    def add(section, item, a, b):
        try:
            diff = round(float(b) - float(a), 3)
        except (TypeError, ValueError):
            diff = ""
        rows.append({"구분": section, "항목": item, "v1": a, "v2": b, "차이(v2-v1)": diff})

    f1, f2 = pd.read_csv(f1p).set_index("단계")["문서 수"], funnel.set_index("단계")["문서 수"]
    for step in list(f2.index) + [x for x in f1.index if x not in f2.index]:
        add("필터 단계별 건수", step, f1.get(step, ""), f2.get(step, ""))
    r1, r2 = pd.read_csv(r1p).set_index("tag"), rep.set_index("tag")
    for tag in [t for t in r2.index if t in r1.index]:
        for col in ("게시물 수(필터 후)", "댓글 수(필터 후)", "자동 관련도"):
            add(col, tag, r1.loc[tag, col], r2.loc[tag, col])
    if s1p.exists() and len(summ):
        a1 = pd.read_csv(s1p)
        a1, a2 = a1[a1.feed_type == "all"].set_index("tag"), summ[summ.feed_type == "all"].set_index("tag")
        for tag in [t for t in a2.index if t in a1.index]:
            for lb in list(LABEL_KO) + ["unknown"]:
                add("반응 라벨 비율", f"{tag}|{lb}", a1.loc[tag, lb], a2.loc[tag, lb])
    _save(pd.DataFrame(rows), tables / "ig_filter_change_compare.csv")


def write_intersection_compare(tables: Path, stats: pd.DataFrame, rep: pd.DataFrame, cfg: dict) -> None:
    """교집합 방식 태그(intersection_tags)와 직접 태그(compare_baseline)를 나란히 비교.
    instagram.compare_frozen 이 true 이고 파일이 이미 있으면 덮어쓰지 않는다(수집 방식 선정 근거 보존)."""
    ig = cfg.get("instagram", {})
    if ig.get("compare_frozen") and (tables / "ig_intersection_compare.csv").exists():
        return
    base, st = ig.get("compare_baseline"), stats.set_index("tag")
    cols = [t for t in ig.get("intersection_tags", []) if t in st.index]
    if not base or base not in st.index or not cols:
        return
    r = rep.set_index("tag")
    out = []
    for name in cols:
        for t in (name, base):
            x, y = st.loc[t], r.loc[t]
            out.append((t, {"수집 게시물 수": int(x.posts_raw), "최근 1년 게시물 수": int(x.recent_all),
                            "최근 1년 게시물 비율": y["최근 1년 게시물 비율"], "광고 비율": y["광고 비율"],
                            "강아지 글 수(광고 제외·강아지 판별 통과)": int(x.nonad_dog), "강아지 적중률": y["강아지 적중률"],
                            "최종 사용 게시물 수(모든 필터 통과)": int(x.posts_final), "비용(추정,$)": x.cost_usd,
                            "강아지 통과 게시물 1개당 비용($)": y["강아지 통과 게시물 1개당 비용($)"],
                            "판정 제안": y["판정 제안"], "적용 기간(일)": int(y["적용 기간(일)"])}))
        df = pd.DataFrame({t: v for t, v in out}).reset_index().rename(columns={"index": "항목"})
        df.loc[len(df)] = ["강아지 판별 범위"] + ["검색 태그 제외 해시태그 전체+본문" if t in cols else "해시태그 제외 본문만" for t in df.columns[1:]]
        _save(df, tables / "ig_intersection_compare.csv")


def emotion_rank_change(pilot: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    """감정 키워드 순위 변화(비율 기준, 동률은 같은 순위). rank_change > 0 이면 파일럿보다 순위가 올랐다."""
    a = pilot.set_index("emotion_word")[["doc_count", "ratio"]].add_prefix("pilot_")
    b = full.set_index("emotion_word")[["doc_count", "ratio"]].add_prefix("full_")
    df = a.join(b, how="outer").fillna(0)
    df["pilot_rank"] = df["pilot_ratio"].rank(method="min", ascending=False).astype(int)
    df["full_rank"] = df["full_ratio"].rank(method="min", ascending=False).astype(int)
    df["rank_change"] = df["pilot_rank"] - df["full_rank"]
    return df.sort_values(["full_rank", "pilot_rank"]).reset_index()


AUTO_NOTE = re.compile(r"^(drop|keep|본수집) 후보$|^기간 \d+일 확장")


def write_source_decision(cfg: dict) -> None:
    """태그별 최종 판정·사유·최종 문서 수 요약 → outputs/tables/ig_source_decision.csv (소스 선정 근거)."""
    tables, raw = Path(cfg["paths"]["tables"]), Path(cfg["paths"]["raw"])
    tags = pd.read_csv(Path(cfg["paths"].get("ig_tags", "config/hashtags_pilot_v2.csv")), encoding="utf-8-sig", dtype=str).fillna("")
    docs = pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig", dtype=str).fillna("")
    cnt = docs.groupby(["keyword", "feed_type"]).size().unstack(fill_value=0)
    rep = pd.read_csv(tables / "ig_pilot_report.csv").set_index("tag") if (tables / "ig_pilot_report.csv").exists() else pd.DataFrame()
    rows = []
    for r in tags.to_dict("records"):
        t = r["tag"]
        posts = int(cnt.loc[t, "post"]) if t in cnt.index and "post" in cnt.columns else 0
        comments = int(cnt.loc[t, "comment"]) if t in cnt.index and "comment" in cnt.columns else 0
        note = "; ".join(x.strip() for x in r["비고"].split(";") if x.strip() and not AUTO_NOTE.match(x.strip()))
        if r["수집 여부"] != "수집":
            decision, reason = "제외(스레드로 대체)", r["기대 역할"] or r["수집 여부"]
        elif r["판정"]:
            decision, reason = r["판정"], note
        else:
            sug = rep.loc[t, "판정 제안"] if t in rep.index else "제안 없음"
            decision, reason = f"미확정(자동 제안: {sug})", note
        rows.append({"tag": t, "query_group": r["query_group"], "층": r["층"], "판정": decision, "사유": reason,
                     "최종 게시물 수": posts, "최종 댓글 수": comments, "최종 문서 수": posts + comments})
    _save(pd.DataFrame(rows), tables / "ig_source_decision.csv")
