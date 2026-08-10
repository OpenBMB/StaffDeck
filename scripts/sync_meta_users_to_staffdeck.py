#!/usr/bin/env python3
"""同步 meta.platform_users → staffdeck SQLite users 表。

数据源: marmot.meta.platform_users（含 employee_code/email/display_name/role/department_name/position_name）
目标:   staffdeck backend/skill_agent_loop.db 的 users 表

逻辑:
  1. 从 meta.platform_users 读取所有 enabled=true 用户
  2. 按 employee_code 匹配 staffdeck users.username
  3. UPSERT：
     - 已存在：更新 display_name/email/department/position/synced_at/auth_source='sso'
     - 不存在：自动创建（tenant=tenant_demo, role=member, auth_source='sso', password_hash=''）
  4. 角色映射：meta.role (admin/editor/viewer) → staffdeck.role (admin/member)
  5. --cleanup-stale：禁用 staffdeck 中 meta 已不存在的 SSO 用户（清空 password_hash，标记 auth_source='disabled'）

用法:
  python3 scripts/sync_meta_users_to_staffdeck.py [--dry-run] [--cleanup-stale]

关联:
  - 依赖: meta.platform_users（由 sync_employee_info_to_platform_users.py 同步 department/position）
  - 目标: staffdeck users 表（SQLite）
  - 补充: SSO 登录时的 upsert_sso_user() 处理单用户实时同步，本脚本处理批量预同步
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

MARMOT_DSN = "host=127.0.0.1 port=5432 dbname=marmot user=postgres password=postgres"
STAFFDECK_DB = "/root/data-platform/staffdeck/backend/skill_agent_loop.db"
DEFAULT_TENANT = "tenant_demo"

# 角色映射：meta.role → staffdeck.role
ROLE_MAP = {
    "admin": "admin",
    "editor": "member",
    "viewer": "member",
}


def fetch_meta_users() -> list[dict]:
    """从 meta.platform_users 读取所有启用用户。"""
    sql = """
        SELECT employee_code, email, display_name, role,
               COALESCE(department_name, '') AS department_name,
               COALESCE(position_name, '') AS position_name
        FROM meta.platform_users
        WHERE enabled = true
        ORDER BY employee_code
    """
    with psycopg2.connect(MARMOT_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    logger.info(f"从 meta.platform_users 读取 {len(rows)} 名启用用户")
    return [dict(r) for r in rows]


def upsert_staffdeck_user(conn: sqlite3.Connection, meta_user: dict) -> str:
    """UPSERT 单个用户到 staffdeck users 表。

    返回 "created" / "updated"。
    """
    employee_code = meta_user["employee_code"]
    email = meta_user["email"]
    display_name = meta_user["display_name"] or employee_code
    department = meta_user["department_name"] or None
    position = meta_user["position_name"] or None
    mapped_role = ROLE_MAP.get(meta_user["role"], "member")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    cur = conn.cursor()
    # 查现有用户
    cur.execute(
        "SELECT id FROM users WHERE tenant_id = ? AND username = ?",
        (DEFAULT_TENANT, employee_code),
    )
    existing = cur.fetchone()

    if existing:
        # 更新（不覆盖 role，admin 可能手工调整过）
        cur.execute(
            """UPDATE users SET
                display_name = ?, email = ?, department = ?, position = ?,
                employee_code = ?, auth_source = 'sso', synced_at = ?, updated_at = ?
            WHERE tenant_id = ? AND username = ?""",
            (display_name, email, department, position, employee_code, now, now,
             DEFAULT_TENANT, employee_code),
        )
        return "updated"
    else:
        # 创建
        import uuid
        user_id = f"user_{uuid.uuid4().hex[:16]}"
        cur.execute(
            """INSERT INTO users (id, tenant_id, username, display_name, role, password_hash,
                employee_code, email, department, position, auth_source, synced_at,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, 'sso', ?, ?, ?)""",
            (user_id, DEFAULT_TENANT, employee_code, display_name, mapped_role,
             employee_code, email, department, position, now, now, now),
        )
        return "created"


def cleanup_stale_users(conn: sqlite3.Connection, valid_codes: set[str]) -> int:
    """禁用 staffdeck 中不在 valid_codes 中的 SSO 用户。

    清空 password_hash + 标记 auth_source='disabled'（不删除，保留历史数据）。
    跳过 auth_source='local' 的本地账号。
    """
    cur = conn.cursor()
    cur.execute(
        """UPDATE users SET
            password_hash = '', auth_source = 'disabled', updated_at = ?
        WHERE tenant_id = ? AND auth_source = 'sso'
        AND employee_code NOT IN ({})""".format(
            ",".join("?" * len(valid_codes)) if valid_codes else "''"
        ),
        [datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), DEFAULT_TENANT]
        + list(valid_codes),
    )
    return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 meta.platform_users → staffdeck users")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    parser.add_argument("--cleanup-stale", action="store_true", help="禁用 staffdeck 中 meta 已不存在的 SSO 用户")
    args = parser.parse_args()

    try:
        meta_users = fetch_meta_users()
    except Exception as e:
        logger.error(f"读取 meta.platform_users 失败: {e}")
        return 1

    if not meta_users:
        logger.warning("meta.platform_users 无启用用户，退出")
        return 0

    stats = {"created": 0, "updated": 0, "disabled": 0}

    conn = sqlite3.connect(STAFFDECK_DB)
    try:
        for mu in meta_users:
            if args.dry_run:
                # dry-run: 只统计
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM users WHERE tenant_id = ? AND username = ?",
                    (DEFAULT_TENANT, mu["employee_code"]),
                )
                if cur.fetchone():
                    stats["updated"] += 1
                else:
                    stats["created"] += 1
            else:
                result = upsert_staffdeck_user(conn, mu)
                stats[result] += 1

        if args.cleanup_stale and not args.dry_run:
            valid_codes = {mu["employee_code"] for mu in meta_users}
            stats["disabled"] = cleanup_stale_users(conn, valid_codes)

        if not args.dry_run:
            conn.commit()

    except Exception as e:
        conn.rollback()
        logger.error(f"同步失败: {e}", exc_info=True)
        return 1
    finally:
        conn.close()

    if args.dry_run:
        logger.info(
            f"[DRY-RUN] meta 用户 {len(meta_users)} 人 | "
            f"预计创建 {stats['created']} 人 | 预计更新 {stats['updated']} 人"
        )
    else:
        logger.info(
            f"同步完成: meta 用户 {len(meta_users)} 人 | "
            f"创建 {stats['created']} 人 | 更新 {stats['updated']} 人 | "
            f"禁用 {stats['disabled']} 人"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
