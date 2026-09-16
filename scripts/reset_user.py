#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机管理自选账号（没有邮箱找回，忘了密码就靠这个脚本）

    python3 scripts/reset_user.py --list                        # 看有哪些账号
    python3 scripts/reset_user.py --user laoli --password 新密码  # 重置密码（所有设备需重新登录）
    python3 scripts/reset_user.py --user laoli --delete          # 删除账号（连带其云端自选）

必须在服务器上（或本地同一份目录）执行；直接改 data/users.json 也行，但不建议。
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_server as api          # 复用同一套哈希与存储实现，避免两处口径漂移  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="管理小散研究院的自选账号")
    ap.add_argument("--list", action="store_true", help="列出所有账号")
    ap.add_argument("--user", help="用户名")
    ap.add_argument("--password", help="新密码（至少 6 位）")
    ap.add_argument("--delete", action="store_true", help="删除该账号（含其云端自选）")
    a = ap.parse_args()

    users = api._load_users()
    if a.list or not a.user:
        if users:
            for name, u in users.items():
                n = len(u.get("codes") or [])
                print(f"  {name}  自选 {n} 只  创建于 {u.get('created')}")
        else:
            print("（还没有任何账号）")
        return

    if a.user not in users:
        print("没有这个账号：", a.user)
        return

    if a.delete:
        users.pop(a.user)
        api._save_users(users)
        print("已删除账号：", a.user)
        return

    if not a.password:
        print("要重置密码请加：--password 新密码")
        return
    if len(a.password) < 6:
        print("密码至少 6 位")
        return

    salt = secrets.token_hex(16)
    users[a.user]["salt"] = salt
    users[a.user]["hash"] = api._hash_pw(a.password, salt, 200000)
    users[a.user]["tokens"] = []          # 让所有已登录设备失效，必须重新登录
    api._save_users(users)
    print("已重置密码：", a.user, "（所有设备需重新登录；自选不受影响）")


if __name__ == "__main__":
    main()
