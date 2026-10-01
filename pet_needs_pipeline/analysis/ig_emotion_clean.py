"""대체재(펫캠·홈캠) 감정 데이터 정제: 고양이 필터 → 홍보 필터 → 감정 대상(target) 분류 → 수동 검토 샘플.

입력: data/raw/instagram.csv 중 본수집(ig_tag_stats_full.csv)에 든 태그의 문서. 파일럿 산출물은 건드리지 않는다.
출력: outputs/tables/ig_emotion_clean_funnel / ig_emotion_by_target / ig_emotion_review_sample / ig_emotion_docs_clean .csv
"""
import logging
import re
from pathlib import Path

import pandas as pd

from crawlers.instagram_apify import has_dog, read_words

log = logging.getLogger("pipeline")
EMO_SPLIT = re.compile(r"[.!?\n]+")


def load_markers(path: str) -> tuple[list, list, list]:
    """(강한 표지, 약한 표지, 보호 표지) 정규식 목록. '~' 접두 = 약함, '!' 접두 = 보호, 're:' 접두 = 정규식."""
    strong, weak, protect = [], [], []
    for line in read_words(path):
        kind = weak if line.startswith("~") else protect if line.startswith("!") else strong
        s = line.lstrip("~!").strip()
        kind.append(re.compile(s[3:] if s.startswith("re:") else re.escape(s), re.I))
    return strong, weak, protect


def sentence_with(text: str, words: list[str]) -> str:
    for s in EMO_SPLIT.split(str(text)):
        s = s.strip()
        if 8 <= len(s) <= 120 and any(w in s for w in words):
            return s
    return str(text).replace("\n", " ")[:120]


def clean_docs(docs: pd.DataFrame, cfg: dict, emo_words: list[str], markers: tuple[list, list]) -> pd.DataFrame:
    """docs(body 포함) → status(kept/removed_cat/removed_promo), promo_suspect, emotions, target, security_mention 열을 붙여 반환."""
    ec, terms = cfg["instagram"]["emotion_clean"], cfg["animal"]["dog_terms"]
    cat_terms = cfg["animal"]["cat_terms"] + ec.get("cat_extra", [])
    strong, weak = markers[0], markers[1]
    protect = markers[2] if len(markers) > 2 else []
    sec_w, concern_w, ctx_w = ec["security_words"], ec["concern_words"], ec["dog_context_words"]

    def dogish(t): return has_dog(t, terms) or any(e in t for e in ec["dog_emoji"])

    out = []
    for r in docs.to_dict("records"):
        t = str(r["body"])
        status, suspect = "kept", 0
        if any(w in t for w in cat_terms) and not dogish(t):          # 고양이 글: 고양이 단어가 있고 강아지 판별에 안 걸림
            status = "removed_cat"
        else:
            s_hit, w_hit = any(p.search(t) for p in strong), any(p.search(t) for p in weak)
            concern = any(w in t for w in sec_w) and any(w in t for w in concern_w)   # 해킹·보안 '우려'를 말하는 사용자 글
            protected = any(p.search(t) for p in protect)                  # 내돈내산·실사용 등 실사용자 표지
            if s_hit and not concern and not protected:
                status = "removed_promo"
            else:
                suspect = int(w_hit or (s_hit and (concern or protected)))   # 애매하면 지우지 않고 플래그만
        emos = [w for w in emo_words if w in t] if status == "kept" else []
        # 감정 대상: dog = 강아지 사전·이모지, 또는 혼자·외출·출근·보고싶 등과 함께 (보고싶 은 다른 감정 키워드와 같이 있을 때만 맥락으로 인정)
        dog = dogish(t) or any(c in t and (c not in emos or len(emos) >= 2) for c in ctx_w)
        sec = any(w in t for w in sec_w)
        target = ("dog" if dog else "security" if sec else "other") if emos else ""
        out.append({**r, "status": status, "promo_suspect": suspect, "emotions": "|".join(emos), "target": target,
                    "security_mention": int(sec) if emos else 0})
    return pd.DataFrame(out)


def emotion_by_target(d: pd.DataFrame, emo_words: list[str]) -> pd.DataFrame:
    """target 별(+전체) 감정 키워드 문서 수·비율·대표 문장 3개. ratio = 해당 target 감정 문서 대비, ratio_of_clean = 정제 후 전체 문서 대비."""
    kept_n = int((d["status"] == "kept").sum())
    emo = d[d["emotions"] != ""]
    rows = []
    for tgt in ("dog", "security", "other", "all"):
        g = emo if tgt == "all" else emo[emo["target"] == tgt]
        for w in emo_words:
            hit = g[pd.Series([w in e.split("|") for e in g["emotions"]], index=g.index, dtype=bool)]
            sents = list(dict.fromkeys(s.strip() for b in hit["body"] for s in EMO_SPLIT.split(str(b))
                                       if w in s and 8 <= len(s.strip()) <= 120))
            pick = pd.Series(sents).sample(min(3, len(sents)), random_state=42).tolist() if sents else []
            rows.append({"target": tgt, "emotion_word": w, "target_docs": len(g), "doc_count": len(hit),
                         "ratio": round(len(hit) / len(g), 3) if len(g) else 0,
                         "ratio_of_clean": round(len(hit) / kept_n, 3) if kept_n else 0,
                         **{f"sample_{i + 1}": (pick[i] if i < len(pick) else "") for i in range(3)}})
    return pd.DataFrame(rows)


def review_sample(d: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """안심·걱정·불안 문서 전체(최대 review_max건, 넘으면 seed=42 무작위) → 수동 검토용."""
    ec = cfg["instagram"]["emotion_clean"]
    rev = ec["review_emotions"]
    g = d[pd.Series([any(w in e.split("|") for w in rev) for e in d["emotions"]], index=d.index, dtype=bool)].copy()
    if len(g) > ec.get("review_max", 100):
        g = g.sample(ec.get("review_max", 100), random_state=42)
    g["감정"] = g["emotions"].map(lambda e: "|".join(w for w in e.split("|") if w in rev))
    g["문장"] = [sentence_with(b, e.split("|")) for b, e in zip(g["body"], g["감정"])]
    out = g.rename(columns={"target": "규칙 target"})[["url_hash", "문장", "감정", "규칙 target", "promo_suspect"]]
    return out.assign(human_target="", human_is_promo="")


def run_emotion_clean(cfg: dict) -> dict:
    raw, tables = Path(cfg["paths"]["raw"]), Path(cfg["paths"]["tables"])
    stats = pd.read_csv(tables / "ig_tag_stats_full.csv", encoding="utf-8-sig")
    docs = pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig", dtype=str).fillna("")
    docs = docs[docs["keyword"].isin(stats["tag"])].reset_index(drop=True)
    emo_words = read_words("config/ig_emotion_words.txt")
    d = clean_docs(docs, cfg, emo_words, load_markers("config/ig_promo_markers.txt"))

    n0 = len(d)
    n_cat, n_promo = int((d.status == "removed_cat").sum()), int((d.status == "removed_promo").sum())
    kept = d[d.status == "kept"]
    emo = kept[kept.emotions != ""]
    funnel = [("정제 대상 문서(본수집 태그의 게시물+댓글)", n0), ("고양이 필터 제거", -n_cat), ("고양이 필터 후", n0 - n_cat),
              ("홍보 필터 제거", -n_promo), ("홍보 필터 후(정제 완료)", len(kept)),
              ("  └ promo_suspect=1 (제거 안 하고 플래그만)", int(kept.promo_suspect.sum())),
              ("감정 키워드가 있는 문서", len(emo)),
              ("  └ target=dog", int((emo.target == "dog").sum())), ("  └ target=security", int((emo.target == "security").sum())),
              ("  └ target=other", int((emo.target == "other").sum()))]
    fdf = pd.DataFrame(funnel, columns=["단계", "문서 수"])
    tables.mkdir(parents=True, exist_ok=True)
    fdf.to_csv(tables / "ig_emotion_clean_funnel.csv", index=False, encoding="utf-8-sig")
    bt = emotion_by_target(d, emo_words)
    bt.to_csv(tables / "ig_emotion_by_target.csv", index=False, encoding="utf-8-sig")
    rv = review_sample(d, cfg)
    rv.to_csv(tables / "ig_emotion_review_sample.csv", index=False, encoding="utf-8-sig")
    d[["url_hash", "keyword", "feed_type", "body", "status", "promo_suspect", "emotions", "target", "security_mention"]].to_csv(
        tables / "ig_emotion_docs_clean.csv", index=False, encoding="utf-8-sig")
    log.info("감정 데이터 정제: %s", " → ".join(f"{a} {b}" for a, b in funnel[:5]))
    return {"funnel": fdf, "by_target": bt, "review": rv, "docs": d}
