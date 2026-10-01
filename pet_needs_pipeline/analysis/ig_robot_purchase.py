"""기존 인스타 CSV 에서 '로봇청소기 구매 고민 글(반려가구 한정)' 추출 (신규 수집 없음).

입력: 인스타 CSV 여러 개 (body 또는 caption 컬럼). 예)
  python -m analysis.ig_robot_purchase data/raw/instagram.csv data/raw/v1/instagram.csv data/raw/instagram_dogfilter_removed.csv
출력: outputs/tables/ig_robot_purchase.csv (후보 전체 + 분류 컬럼), ig_robot_purchase_summary.csv
분류(규칙 기반, 검수 전제):
  stage: 구매 전 고민 / 구매 후(후기) / 기타   ad: 협찬·이벤트 표지   dog: 반려가구 근거
"""
import re
import sys
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "outputs/tables"
ROBOT = r"로봇청소기|로봇 청소기|로청|로보락|제트봇|에코백스|드리미|나르왈|물걸레"
DOG = r"강아지|반려견|댕댕|멍멍|멍스타|견주|강쥐|말티|푸들|포메|비숑|시츄|치와와|골든|리트리버|웰시|믹스견|반려가구|털 ?날|털갈이|털 ?빠"
PRE = r"고민|살까|사려|사볼|구매 ?예정|구입 ?예정|알아보|추천해|추천 ?부탁|뭐가 ?좋|어떤 ?게|비교|고르|골라|살지|사야|사고 ?싶|살 ?예정|견적|문의"
POST = r"샀|구매했|구입했|사길|내돈내산|써보|사용 ?중|쓰고 ?있|들였|장만|후기"
AD = r"협찬|지원 ?받아|제공 ?받|이벤트|광고|체험단|공구|할인|쿠폰|출시|\bAD\b|#ad"


def load(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, encoding="utf-8-sig")
    text = d["body"] if "body" in d else d["caption"]
    out = pd.DataFrame({"text": text.fillna("").astype(str), "url": d.get("url", ""),
                        "feed_type": d.get("feed_type", "post"), "date": d.get("date", ""),
                        "tag": d.get("keyword", d.get("tag", ""))})
    out["file"] = Path(path).name if "v1" not in path else "v1/" + Path(path).name
    return out


def main(paths):
    df = pd.concat([load(p) for p in paths], ignore_index=True)
    print("입력 행수:", len(df))
    df = df.drop_duplicates("text")
    df = df[df.text.str.contains(ROBOT)].copy()
    df["dog"] = df.text.str.contains(DOG)
    df["pre"] = df.text.str.contains(PRE)
    df["post"] = df.text.str.contains(POST)
    df["ad"] = df.text.str.contains(AD, case=False)
    df["stage"] = df.apply(lambda r: "구매 전 고민" if r.pre else ("구매 후(후기)" if r.post else "기타"), axis=1)
    df["반려가구·광고제외"] = df.dog & ~df.ad
    df = df.rename(columns={"text": "본문"}).sort_values(["반려가구·광고제외", "pre"], ascending=False)
    df.to_csv(OUT / "ig_robot_purchase.csv", index=False, encoding="utf-8-sig")
    base = df[df.dog]
    summ = pd.DataFrame([
        {"단계": "로봇청소기 언급(중복 제거)", "건수": len(df)},
        {"단계": "└ 반려가구 근거(강아지 단어)", "건수": len(base)},
        {"단계": "  └ 광고·협찬 제외", "건수": int((base.ad == False).sum())},
        {"단계": "    └ 구매 전 고민(고민·추천·비교 등)", "건수": int(((base.ad == False) & base.pre).sum())},
        {"단계": "    └ 구매 후 후기(털 때문에 산 경우 포함)", "건수": int(((base.ad == False) & ~base.pre & base.post).sum())},
    ])
    summ.to_csv(OUT / "ig_robot_purchase_summary.csv", index=False, encoding="utf-8-sig")
    print(summ.to_string(index=False))
    for r in df[df["반려가구·광고제외"] & df.pre].itertuples():
        print("-", r.feed_type, "|", r.본문[:150].replace("\n", " "))


if __name__ == "__main__":
    main(sys.argv[1:])
