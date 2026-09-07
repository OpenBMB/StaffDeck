"""域控（LDAP/AD）连通性与账号检索自检。

用法（backend 目录）：
    # 1) 只测服务账号连通 + 检索某个账号（不校验口令）
    python scripts/ldap_probe.py chenjiali

    # 2) 连通用例 + 校验该账号口令是否正确，并打印同步到库的字段
    python scripts/ldap_probe.py chenjiali --password '你的域口令'

    # 3) 打印域账号的候选属性原始值（排查部门/显示名该取哪个字段）
    python scripts/ldap_probe.py chenjiali --password '你的域口令' --dump

退出码 0=正常，1=配置缺失或域控不可达，2=账号未找到/口令错误。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.security import ldap_client  # noqa: E402

# 排查部门/显示名时重点关注的 AD 属性
_FOCUS_KEYWORDS = (
    "depart",
    "company",
    "office",
    "division",
    "title",
    "descr",
    "ou",
    "group",
    "memberof",
    "displayname",
    "samaccount",
    "mail",
)


def _connect(ldap3, settings, user: str, password: str):
    server = ldap3.Server(
        settings.ldap_server_url.strip(),
        connect_timeout=settings.ldap_timeout_seconds,
        get_info=ldap3.NONE,
    )
    return ldap3.Connection(
        server,
        user=user or None,
        password=password or None,
        auto_bind=True,
        receive_timeout=settings.ldap_timeout_seconds,
    )


def _bind_identity(settings, username: str, password: str) -> tuple[str, str]:
    """返回 (bind 用户, bind 口令)：有服务账号用服务账号，否则用账号自身 UPN。"""
    bind_dn = settings.ldap_bind_dn.strip()
    if bind_dn:
        return bind_dn, settings.ldap_bind_password
    domain = settings.ldap_domain.strip()
    return (f"{username}@{domain}" if domain and "@" not in username else username), password


def _dump_attributes(ldap3, settings, username: str, password: str) -> int:
    """用全部属性（*）检索账号，打印部门相关候选属性的原始值。"""
    user, pwd = _bind_identity(settings, username, password)
    try:
        with _connect(ldap3, settings, user, pwd) as conn:
            for candidate in ldap_client._login_candidates(username):
                search_filter = ldap_client._filter_for(candidate)
                for base_dn in settings.ldap_base_dn_list:
                    conn.search(
                        search_base=base_dn,
                        search_filter=search_filter,
                        search_scope=ldap3.SUBTREE,
                        attributes=["*"],
                        size_limit=1,
                    )
                    if not conn.entries:
                        continue
                    entry = conn.entries[0]
                    data = entry.entry_attributes_as_dict
                    print(f"\n[OK] 检索到账号（DN）: {entry.entry_dn}")
                    print("\n--- 重点关注属性（原始值）---")
                    for key in sorted(data):
                            value = data[key]
                            if isinstance(value, list) and len(value) > 6:
                                value = value[:6] + [f"...(共 {len(data[key])} 项)"]
                            print(f"  {key}: {value}")
                    print("\n--- 全部属性名（挑字段用）---")
                    print("  " + ", ".join(sorted(data)))
                    return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] 连接/检索失败：{exc}")
        if not settings.ldap_bind_dn.strip():
            print("提示：未配置服务账号（LDAP_BIND_DN），AD 通常不允许匿名检索，请补服务账号或口令。")
        return 1
    print(f"\n[FAIL] 在配置的检索根下未找到账号 {username}")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="LDAP/AD 连通性与账号检索自检")
    parser.add_argument("username", help="域账号（sAMAccountName / UPN / 邮箱）")
    parser.add_argument("--password", default="", help="域口令，提供时会校验登录")
    parser.add_argument(
        "--dump", action="store_true", help="打印域账号的候选属性原始值（排查部门字段）"
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"开关 LDAP_ENABLED      : {settings.ldap_enabled}")
    print(f"域控 LDAP_SERVER_URL   : {settings.ldap_server_url}")
    print(f"检索根 LDAP_BASE_DNS   : {settings.ldap_base_dn_list}")
    print(f"域名 LDAP_DOMAIN       : {settings.ldap_domain or '(未配置)'}")
    print(f"服务账号 LDAP_BIND_DN  : {settings.ldap_bind_dn or '(未配置，走 UPN 直连 bind)'}")
    print(
        f"部门候选属性 LDAP_ATTR_DEPARTMENT : {settings.ldap_department_attr_list}"
        f"（都为空时回退 DN 的 OU：{settings.ldap_department_from_dn}）"
    )
    if not ldap_client.is_enabled():
        print("\n[FAIL] 域认证未启用：检查 LDAP_ENABLED / LDAP_SERVER_URL / LDAP_BASE_DNS")
        return 1

    if args.dump:
        ldap3 = ldap_client._ldap3()
        if ldap3 is None:
            print("\n[FAIL] 未安装 ldap3")
            return 1
        if not args.password and not settings.ldap_bind_dn.strip():
            print("\n[FAIL] --dump 需要 --password（无服务账号时无法匿名检索）")
            return 1
        return _dump_attributes(ldap3, settings, args.username, args.password)

    if args.password:
        try:
            profile = ldap_client.authenticate(args.username, args.password)
        except ldap_client.LdapUnavailable as exc:
            print(f"\n[FAIL] 域控不可达：{exc}")
            return 1
        if profile is None:
            print("\n[FAIL] 账号未找到或口令错误/账号被禁用")
            return 2
        print("\n[OK] 认证通过，将同步到库的字段：")
        print(f"  username    : {profile.username}")
        print(f"  display_name: {profile.display_name}")
        print(f"  department  : {profile.department}")
        print(f"  dn          : {profile.dn}")
        return 0

    # 不校验口令：用服务账号检索（无服务账号时用空口令 bind 大概率失败，给出提示）
    ldap3 = ldap_client._ldap3()
    if ldap3 is None:
        print("\n[FAIL] 未安装 ldap3")
        return 1
    bind_dn = settings.ldap_bind_dn.strip()
    try:
        with _connect(ldap3, settings, bind_dn, settings.ldap_bind_password) as conn:
            entry = ldap_client._search_user(ldap3, conn, settings, args.username)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] 连接/检索失败：{exc}")
        if not bind_dn:
            print("提示：未配置服务账号（LDAP_BIND_DN），AD 通常不允许匿名检索，请补服务账号。")
        return 1

    if entry is None:
        print(f"\n[FAIL] 在配置的检索根下未找到账号 {args.username}")
        return 2
    print("\n[OK] 检索到账号（未校验口令）")
    print(f"  dn          : {entry.entry_dn}")
    print(f"  username    : {ldap_client._attr_value(entry, settings.ldap_attr_username)}")
    print(f"  display_name: {ldap_client._attr_value(entry, settings.ldap_attr_display_name)}")
    print(f"  department  : {ldap_client._attr_value(entry, settings.ldap_attr_department)}")
    print(f"  department2  : {ldap_client._attr_value(entry, 'memeberOf')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
