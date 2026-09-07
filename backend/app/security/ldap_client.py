"""LDAP / AD 域认证客户端。

认证流程（两步，兼容 AD）：
1) 用服务账号（LDAP_BIND_DN）连接并检索用户 DN；未配置服务账号时，
   退化为用 userPrincipalName（user@domain）直连 bind，再用该连接检索自己。
2) 用检索到的 DN + 用户提交的口令重新 bind，成功才算认证通过（避免"只搜到就算过"）。

口令正确与否不区分细节：
- 找不到用户 / 口令错误 / 账号被禁用 → 返回 None（由登录端点决定回退或 401）
- 域控连不上、配置缺失、依赖缺失 → 抛 LdapUnavailable（登录端点可回退本地口令）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# AD: userAccountControl 第 2 位（ACCOUNTDISABLE）表示账号被禁用
_ACCOUNTDISABLE = 0x2


class LdapUnavailable(RuntimeError):
    """域控不可达 / 配置不全 / 依赖缺失，调用方应回退本地认证。"""


@dataclass(frozen=True)
class LdapUser:
    """认证成功后的域账号信息（用于本地库 upsert）。

    仅保留页面展示需要的字段：账号（username）、显示名（display_name）、部门（department）。
    """

    username: str
    display_name: Optional[str]
    department: Optional[str]
    dn: str


def is_enabled() -> bool:
    """域认证是否启用：开关打开 + 配了域控地址 + ldap3 可用 + 至少一个检索根。"""
    settings = get_settings()
    if not settings.ldap_enabled or not settings.ldap_server_url.strip():
        return False
    if not settings.ldap_base_dn_list:
        logger.warning("LDAP 已启用但未配置 LDAP_BASE_DNS，跳过域认证")
        return False
    return _ldap3() is not None


def authenticate(username: str, password: str) -> Optional[LdapUser]:
    """校验域账号口令。成功返回 LdapUser；账号不存在或口令错误返回 None。"""
    ldap3 = _ldap3()
    if ldap3 is None:  # pragma: no cover - is_enabled 已提前拦截
        raise LdapUnavailable("ldap3 未安装，无法使用域认证")
    settings = get_settings()
    if not password:
        return None
    user = username.strip()
    if not user:
        return None

    server = ldap3.Server(
        settings.ldap_server_url.strip(),
        connect_timeout=settings.ldap_timeout_seconds,
        get_info=ldap3.NONE,
    )
    bind_dn = settings.ldap_bind_dn.strip()
    # 有服务账号：先检索 DN 再验口令；无服务账号：先用 UPN 直连 bind 再检索属性
    if bind_dn:
        profile = _search_then_bind(ldap3, server, settings, user, password, bind_dn)
    else:
        profile = _bind_then_search(ldap3, server, settings, user, password)
    return profile


# --------------------------------------------------------------------------
# 内部实现
# --------------------------------------------------------------------------
def _ldap3():
    """延迟导入 ldap3：未安装时不影响应用启动（仅域认证不可用）。"""
    try:
        import ldap3  # noqa: PLC0415

        return ldap3
    except ImportError:
        return None


def _escape_filter_value(value: str) -> str:
    """转义 LDAP 过滤器特殊字符，防注入（RFC 4515）。"""
    escaped = value.replace("\\", "\\5c").replace("*", "\\2a")
    escaped = escaped.replace("(", "\\28").replace(")", "\\29")
    return escaped.replace("\x00", "\\00")


def _login_candidates(username: str) -> list[str]:
    """登录名的候选形式：原样、带域后缀、去域后缀。"""
    settings = get_settings()
    domain = settings.ldap_domain.strip()
    candidates = [username]
    if domain:
        lowered = username.lower()
        if "@" not in username:
            candidates.append(f"{username}@{domain}")
        elif lowered.endswith(f"@{domain.lower()}"):
            candidates.append(username.split("@", 1)[0])
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def _filter_for(username: str) -> str:
    """把过滤器模板中的 {username} 替换为转义后的登录名。"""
    template = get_settings().ldap_user_filter or "(sAMAccountName={username})"
    return template.replace("{username}", _escape_filter_value(username))


def _search_then_bind(ldap3, server, settings, username: str, password: str, bind_dn: str):
    """服务账号模式：bind 服务账号 → 检索用户 DN → 用用户 DN 验口令。"""
    try:
        with ldap3.Connection(
            server,
            user=bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=True,
            receive_timeout=settings.ldap_timeout_seconds,
        ) as conn:
            entry = _search_user(ldap3, conn, settings, username)
            if entry is None:
                return None
            dn = _attr_value(entry, "distinguishedName") or entry.entry_dn
            if _is_disabled(entry):
                logger.info("域账号 %s 已禁用，拒绝登录", dn)
                return None
            profile = _profile_from_entry(settings, entry, dn)
    except LdapUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 域控异常一律收敛为不可用
        raise LdapUnavailable(f"域控检索失败：{exc}") from exc

    if not _bind_as(ldap3, server, settings, dn, password):
        return None
    return profile


def _bind_then_search(ldap3, server, settings, username: str, password: str):
    """无服务账号：先用 UPN/域账号 bind 校验口令，再用同一连接检索属性。"""
    for candidate in _login_candidates(username):
        try:
            with ldap3.Connection(
                server,
                user=candidate,
                password=password,
                auto_bind=True,
                receive_timeout=settings.ldap_timeout_seconds,
            ) as conn:
                entry = _search_user(ldap3, conn, settings, username)
                if entry is not None and _is_disabled(entry):
                    logger.info("域账号 %s 已禁用，拒绝登录", entry.entry_dn)
                    return None
                dn = (
                    (_attr_value(entry, "distinguishedName") or entry.entry_dn)
                    if entry is not None
                    else candidate
                )
                profile = (
                    _profile_from_entry(settings, entry, dn)
                    if entry is not None
                    else LdapUser(
                        username=_clean_username(username),
                        display_name=None,
                        department=_department_from_dn(candidate, settings.ldap_department_exclude_list)
                        if settings.ldap_department_from_dn
                        else None,
                        dn=candidate,
                    )
                )
                return profile
        except LdapUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 口令错误/账号不存在：换下一个候选
            continue
    return None


def _search_user(ldap3, conn, settings, username: str):
    """在配置的各个检索根下查找用户，返回首个命中的 entry。"""
    attributes = [
        settings.ldap_attr_username,
        settings.ldap_attr_display_name,
        *settings.ldap_department_attr_list,
        "distinguishedName",
        "userAccountControl",
    ]
    for candidate in _login_candidates(username):
        search_filter = _filter_for(candidate)
        for base_dn in settings.ldap_base_dn_list:
            try:
                conn.search(
                    search_base=base_dn,
                    search_filter=search_filter,
                    search_scope=ldap3.SUBTREE,
                    attributes=[a for a in attributes if a],
                    size_limit=1,
                )
            except Exception as exc:  # noqa: BLE001
                raise LdapUnavailable(f"域控检索失败（{base_dn}）：{exc}") from exc
            if conn.entries:
                return conn.entries[0]
    return None


def _bind_as(ldap3, server, settings, dn: str, password: str) -> bool:
    """用 DN + 口令 bind：成功 True，口令错误/失败 False，连接异常抛 LdapUnavailable。"""
    try:
        with ldap3.Connection(
            server,
            user=dn,
            password=password,
            receive_timeout=settings.ldap_timeout_seconds,
        ) as conn:
            return bool(conn.bind())
    except Exception as exc:  # noqa: BLE001
        # ldap3 对口令错误通常不抛异常（bind() 返回 False），此处多为网络/协议问题
        raise LdapUnavailable(f"域控校验失败：{exc}") from exc


def _is_disabled(entry) -> bool:
    raw = _attr_value(entry, "userAccountControl")
    if not raw:
        return False
    try:
        return bool(int(raw) & _ACCOUNTDISABLE)
    except (TypeError, ValueError):
        return False


def _attr_value(entry, name: str) -> Optional[str]:
    """读取 entry 属性：单值返回字符串，多值返回首个，缺失返回 None。"""
    if entry is None or not name:
        return None
    try:
        value: Any = getattr(entry, name).value
    except Exception:  # noqa: BLE001 - 属性缺失时 ldap3 抛 LDAPCursorError
        return None
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = str(value).strip()
    return text or None


def _clean_username(username: str) -> str:
    """去掉 userPrincipalName 的域后缀，得到本地账号名。"""
    settings = get_settings()
    domain = settings.ldap_domain.strip()
    if domain and username.lower().endswith(f"@{domain.lower()}"):
        return username[: -len(domain) - 1]
    return username.split("@", 1)[0] if "@" in username else username


def _department_from_dn(dn: str, excludes: Optional[list[str]] = None) -> Optional[str]:
    """从 DN 解析部门：取最靠近 CN 的那个 OU。

    "CN=张三,OU=AILab,OU=复星旅游文化集团,DC=fosun,DC=com" -> "AILab"
    excludes 中的 OU（忽略大小写）会被跳过，继续往外找下一个。
    """
    if not dn:
        return None
    skip = {item.lower() for item in (excludes or [])}
    for part in dn.split(","):
        piece = part.strip()
        if piece[:3].upper() == "OU=":
            value = piece[3:].strip()
            if value and value.lower() not in skip:
                return value
    return None


def _profile_from_entry(settings, entry, dn: str) -> LdapUser:
    if logger.isEnabledFor(logging.INFO):
        try:
            raw = entry.entry_attributes_as_dict
        except Exception:  # noqa: BLE001 - 诊断日志失败不影响认证
            raw = None
        logger.info("域账号 %s 返回属性：%s", dn, raw)
    username = _clean_username(_attr_value(entry, settings.ldap_attr_username) or _clean_username(dn))
    # 部门：按配置的候选属性顺序取第一个非空值；都为空时回退用 DN 的 OU
    department: Optional[str] = None
    for attr in settings.ldap_department_attr_list:
        department = _attr_value(entry, attr)
        if department:
            break
    if not department and settings.ldap_department_from_dn:
        department = _department_from_dn(dn, settings.ldap_department_exclude_list)
    return LdapUser(
        username=username,
        display_name=_attr_value(entry, settings.ldap_attr_display_name),
        department=department,
        dn=dn,
    )
