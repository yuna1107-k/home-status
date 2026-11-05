#!/usr/bin/env python3
"""登録済みパスキーの確認・削除（端末をなくしたとき用）。

  python3 tools/passkey_admin.py list          一覧
  python3 tools/passkey_admin.py remove <ID>   1 つ削除（ID は先頭の数文字でも可）
  python3 tools/passkey_admin.py reset         全部削除（次に開いたとき最初の登録からやり直し）

削除後は `systemctl --user restart home-status` でログイン中のセッションも無効になる。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cloud  # noqa: E402
import passkey  # noqa: E402


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    creds = passkey.credentials()
    if cmd == "list":
        if not creds:
            print("登録済みのパスキーはありません")
        for c in creds:
            print(f"{c['id'][:16]}…  {c['name']:<16} 登録 {c['created_at']}  最終使用 {c['last_used_at'] or '-'}  ({c['email']})")
        return 0
    if cmd == "remove" and len(sys.argv) > 2:
        hits = [c for c in creds if c["id"].startswith(sys.argv[2])]
        if len(hits) != 1:
            print("該当するパスキーが 1 つに絞れません" if hits else "該当するパスキーがありません")
            return 1
        passkey.remove_credential(hits[0]["id"])
        print(f"削除しました: {hits[0]['name']}")
        return 0
    if cmd == "reset":
        if input(f"{len(creds)} 件のパスキーをすべて削除します。よろしいですか？ [y/N] ").lower() != "y":
            return 1
        for c in creds:
            passkey.remove_credential(c["id"])
        print("すべて削除しました。systemctl --user restart home-status でセッションも無効にしてください")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    code = main()
    # D1 の控えにもすぐ反映する（届かなければサーバーが後で送る）
    cloud.flush_quietly()
    sys.exit(code)
