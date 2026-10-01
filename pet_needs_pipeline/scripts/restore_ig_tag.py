"""백업(*.bakN.*)에서 특정 인스타 태그의 행을 되살린다 (--recollect 가 실패해 행이 사라졌을 때).

사용: python scripts/restore_ig_tag.py 강아지홈캠 bak5
  data/raw/instagram.bak5.csv · instagram_meta.bak5.csv · checkpoint.bak5.json 에서 해당 태그 행/완료 기록만 현재 파일로 복원한다.
  이미 현재 파일에 있는 url_hash 는 건드리지 않는다 (여러 번 실행해도 안전).
"""
import json
import sys
from pathlib import Path

import pandas as pd

raw = Path("data/raw")


def main(tag: str, suffix: str) -> None:
    cur, bak = raw / "instagram.csv", raw / f"instagram.{suffix}.csv"
    d, b = pd.read_csv(cur, encoding="utf-8-sig", dtype=str), pd.read_csv(bak, encoding="utf-8-sig", dtype=str)
    back = b[(b["keyword"] == tag) & ~b["url_hash"].isin(d["url_hash"])]
    pd.concat([d, back], ignore_index=True).to_csv(cur, index=False, encoding="utf-8-sig")
    print(f"{tag}: instagram.csv 에 {len(back)}행 복원")

    mc, mb = raw / "instagram_meta.csv", raw / f"instagram_meta.{suffix}.csv"
    if mc.exists() and mb.exists():
        md, mbk = pd.read_csv(mc, dtype=str), pd.read_csv(mb, dtype=str)
        mback = mbk[mbk["url_hash"].isin(back["url_hash"]) & ~mbk["url_hash"].isin(md["url_hash"])]
        pd.concat([md, mback], ignore_index=True).to_csv(mc, index=False, encoding="utf-8-sig")
        print(f"{tag}: instagram_meta.csv 에 {len(mback)}행 복원")

    cc, cb = raw / "checkpoint.json", raw / f"checkpoint.{suffix}.json"
    if cc.exists() and cb.exists():
        ck, ckb = json.loads(cc.read_text("utf-8")), json.loads(cb.read_text("utf-8"))
        key = f"instagram_full|{tag}"
        if key in ckb["done"] and key not in ck["done"]:
            ck["done"][key] = ckb["done"][key]
            cc.write_text(json.dumps(ck, ensure_ascii=False), "utf-8")
            print(f"{tag}: 체크포인트 완료 기록 복원")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
