#!/usr/bin/env python3
"""手元のパスキーの控え（data/passkeys.json と D1 の kv 'passkeys'）を共通アカウントに揃える。

共通アカウント（auth-db）が正本になったので、手元の控えを次のように整理する。何度実行しても同じ結果になる。
  - auth-db にあるパスキー: rp_id と持ち主（auth_user_id）を auth-db の値に揃える
  - auth-db に無い・RP ID が違う・AUTH_OWNER_USER_ID 以外の人のパスキー: 控えから外す（どのみちログインに使えない）
  - このサイト独自のユーザー ID（user_id）: AUTH_OWNER_USER_ID のユーザーハンドルに置き換える
auth-db は読むだけで、書き込むのは手元の控えと D1 の kv（このサイトの D1）だけ。

  python3 src/tools/passkey_link_auth.py           変更内容の表示だけ
  python3 src/tools/passkey_link_auth.py --apply   反映する（反映後に systemctl --user restart home-status）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import authdb  # noqa: E402
import cloud  # noqa: E402
import passkey  # noqa: E402


def plan(data: dict) -> tuple[dict, list[str]]:
    """整理したあとの控えと、変更点の説明を返す（data は変えない）。"""
    owner = authdb.OWNER_USER_ID
    notes: list[str] = []
    keep = []
    for c in data.get("credentials", []):
        rp = c.get("rp_id") or passkey.RP_ID
        row = authdb.credential(c["id"])
        label = f"{c['id'][:12]}… {c.get('name', '')}"
        if row is None:
            notes.append(f"外す: {label}（auth-db に無い）")
            continue
        if row.get("rp_id") != rp:
            notes.append(f"外す: {label}（auth-db では RP ID が {row.get('rp_id')}）")
            continue
        if owner and row["user_id"] != owner:
            notes.append(f"外す: {label}（持ち主が {row.get('username')}）")
            continue
        new = {**c, "rp_id": rp, "auth_user_id": row["user_id"]}
        if new != c:
            notes.append(f"揃える: {label}（rp_id={rp}, auth_user_id={row['user_id']}）")
        keep.append(new)
    result = {**data, "credentials": keep}
    if owner:
        handle = passkey.b64url(owner.encode())
        if data.get("user_id") != handle:
            notes.append(f"user_id を共通アカウントのユーザーハンドルに置き換える（{owner}）")
            result["user_id"] = handle
    return result, notes


def main() -> int:
    if not authdb.enabled():
        print("共通アカウントが未設定です（.env の AUTH_D1_DATABASE_ID / COMMON_PASSKEY_RP_ID）")
        return 1
    if not authdb.OWNER_USER_ID:
        print("注意: AUTH_OWNER_USER_ID が未設定なので、持ち主での絞り込みと user_id の置き換えはしません")
    with passkey._lock:
        data = passkey._load()
    try:
        result, notes = plan(data)
    except authdb.AuthDbError as ex:
        print(ex)
        return 1
    if not notes:
        print("変更はありません")
        return 0
    print("\n".join(notes))
    if "--apply" not in sys.argv[1:]:
        print("\n--apply を付けると反映します")
        return 0
    with passkey._lock:
        passkey._save(result)
    cloud.flush_quietly()  # D1 の控えにもすぐ反映する（届かなければサーバーが後で送る）
    print("反映しました。systemctl --user restart home-status でログイン中のセッションも作り直してください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
