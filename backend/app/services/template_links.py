"""查询模板与下游消费方的联动规则。

聚合查询配置是主对象：查询模板编号、审批状态与模板版本都以 query 表里的记录为准。
数据服务与缓存规则只是下游消费方，读列表、读详情时都必须通过本模块重新解析
「当前审批通过版本」，避免各自保留一份过期的版本副本。

状态约定：
- 查询模板发布或停用后，引用它的数据服务标记为「待重新发布」；
- 查询模板发布或停用后，引用它的缓存规则标记为「待失效」；
- 下游展示的模板版本取查询模板的审批通过版本；审批通过版本与已发布版本冲突时，
  一律以审批通过版本为准。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

QUERY_MODULE = "query"
SERVICE_MODULE = "service"
CACHE_MODULE = "cache"

PUBLISHED = "已发布"
STOPPED = "已停用"

PENDING_REPUBLISH = "待重新发布"
REPUBLISH_NOT_REQUIRED = "无需重新发布"
SYNCED = "已同步"

PENDING_INVALIDATE = "待失效"
CACHE_NORMAL = "正常"
NO_VALID_TEMPLATE = "无有效模板"


def now_text() -> str:
    """统一的时间戳格式，写进审批记录和消费记录。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _code_key(code: object) -> str:
    return str(code or "").strip().upper()


# 模板编号在历史资料里出现过 QUERY / QUER 两种前缀，按编号数字对齐，避免下游认错模板。
_PREFIX_ALIASES = {"QUERY": "QUER"}


def normalize_code(code: object) -> str:
    raw = _code_key(code)
    if "-" not in raw:
        return raw
    prefix, tail = raw.split("-", 1)
    return f"{_PREFIX_ALIASES.get(prefix, prefix)}-{tail}"


def find_template(template_code: object) -> dict[str, Any] | None:
    """按模板编号在 query 表里找到唯一的查询模板记录。"""
    if template_code in (None, ""):
        return None
    code = normalize_code(template_code)
    for row in store.rows(QUERY_MODULE):
        row_code = row.get("模板编号") or row.get("查询模板编号")
        if normalize_code(row_code) == code:
            return row
    return None


def approved_version(template: dict[str, Any]) -> object | None:
    """查询模板对外生效的版本，下游列表与详情都读这一份。

    审批通过版本优先于已发布版本：审批版本与已发布版本冲突时以审批通过版本为准。
    每个版本轮次各自独立：新一轮提交后，尚未审批通过就没有审批通过版本，
    上一轮的结论只作为历史保留，不进入当前消费清单。
    """
    current_seq = int(template.get("版本序号") or 0)
    if current_seq <= 0:
        return None
    approved = template.get("已审批版本")
    if approved not in (None, "") and int(template.get("已审批序号") or 0) == current_seq:
        return approved
    published = template.get("已发布版本")
    if published not in (None, "") and int(template.get("已发布序号") or 0) == current_seq:
        return published
    return None


def active_version(template: dict[str, Any]) -> object | None:
    """当前真正可供下游消费的活动版本；只有已发布状态才有活动版本。"""
    if template.get("status") != PUBLISHED:
        return None
    return approved_version(template)


def _history(entry: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = entry.get(key)
    if not isinstance(value, list):
        value = []
        entry[key] = value
    return value


def sync_service_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """把数据服务的消费版本与查询模板当前审批通过版本对齐。"""
    template = find_template(entry.get("查询模板编号"))
    if template is None:
        if entry.get("查询模板编号") in (None, ""):
            entry["待重新发布"] = False
            entry["消费状态"] = "未引用模板"
            entry["重新发布状态"] = REPUBLISH_NOT_REQUIRED
            entry["版本状态"] = entry["消费状态"]
            return entry
        entry["查询模板版本"] = None
        entry["待重新发布"] = bool(entry.get("待重新发布"))
        entry["消费状态"] = NO_VALID_TEMPLATE
        entry["重新发布状态"] = PENDING_REPUBLISH if entry["待重新发布"] else REPUBLISH_NOT_REQUIRED
        entry["版本状态"] = entry["消费状态"]
        return entry

    effective = approved_version(template)
    active = active_version(template)
    consumed = entry.get("消费版本")
    if consumed in (None, "") and active is not None:
        # 兼容旧数据：没有单独的消费版本时，视为与当前活动版本一致。
        consumed = active
    entry["消费版本"] = consumed

    pending = bool(entry.get("待重新发布"))
    if template.get("status") == STOPPED:
        # 停用后下游必须重新发布，旧版本不再作为活动版本展示。
        pending = True
    elif template.get("status") == PUBLISHED and consumed not in (None, "") and consumed != active:
        pending = True

    # 列表、详情与消费入口展示同一个审批通过版本；能否直接消费由活动版本和待重新发布标记决定。
    entry["查询模板版本"] = effective
    entry["审批通过版本"] = effective
    entry["活动版本"] = active
    entry["查询模板审批状态"] = template.get("status")
    entry["待重新发布"] = pending
    if pending:
        entry["消费状态"] = PENDING_REPUBLISH
        entry["重新发布状态"] = PENDING_REPUBLISH
        entry["版本状态"] = PENDING_REPUBLISH
    elif active is None:
        entry["消费状态"] = NO_VALID_TEMPLATE
        entry["重新发布状态"] = REPUBLISH_NOT_REQUIRED
        entry["版本状态"] = NO_VALID_TEMPLATE
    else:
        entry["消费状态"] = SYNCED
        entry["重新发布状态"] = REPUBLISH_NOT_REQUIRED
        entry["版本状态"] = SYNCED
    return entry


def sync_cache_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """把缓存规则的引用版本与查询模板当前审批通过版本对齐，并给出失效标记。"""
    template = find_template(entry.get("查询模板编号"))
    if template is None:
        if entry.get("查询模板编号") in (None, ""):
            entry["待失效"] = False
            entry["缓存状态"] = "未引用模板"
            entry["失效状态"] = entry["缓存状态"]
            entry["失效标记"] = ""
            return entry
        entry["查询模板版本"] = None
        entry["待失效"] = bool(entry.get("待失效"))
        entry["缓存状态"] = PENDING_INVALIDATE if entry["待失效"] else CACHE_NORMAL
        entry["失效状态"] = entry["缓存状态"]
        entry["失效标记"] = PENDING_INVALIDATE if entry["待失效"] else ""
        return entry

    effective = approved_version(template)
    active = active_version(template)
    cached = entry.get("缓存版本")
    if cached in (None, "") and active is not None:
        cached = active
    entry["缓存版本"] = cached

    pending = bool(entry.get("待失效"))
    if template.get("status") == STOPPED:
        pending = True
    elif template.get("status") == PUBLISHED and cached not in (None, "") and cached != active:
        pending = True

    entry["查询模板版本"] = effective
    entry["审批通过版本"] = effective
    entry["活动版本"] = active
    entry["查询模板审批状态"] = template.get("status")
    entry["待失效"] = pending
    if pending:
        entry["缓存状态"] = PENDING_INVALIDATE
        entry["失效状态"] = PENDING_INVALIDATE
        entry["失效标记"] = PENDING_INVALIDATE
    elif active is None:
        entry["缓存状态"] = NO_VALID_TEMPLATE
        entry["失效状态"] = NO_VALID_TEMPLATE
        entry["失效标记"] = ""
    else:
        entry["缓存状态"] = CACHE_NORMAL
        entry["失效状态"] = CACHE_NORMAL
        entry["失效标记"] = ""
    return entry


def mark_downstream(template: dict[str, Any], action: str, operator: str = "") -> dict[str, Any]:
    """查询模板发布/停用后，同步下游数据服务与缓存规则的标记。

    返回受影响的编号清单，写进查询模板的消费记录，保证历史消费链路可追溯。
    """
    code = normalize_code(template.get("模板编号") or template.get("查询模板编号"))
    effective = approved_version(template)
    active = active_version(template)
    stamp = now_text()
    affected_services: list[str] = []
    affected_caches: list[str] = []

    for service in store.rows(SERVICE_MODULE):
        if normalize_code(service.get("查询模板编号")) != code or not code:
            continue
        # 历史消费记录：下游实际消费的版本必须留档，不能随主对象改写而丢失。
        previous = service.get("消费版本")
        if previous in (None, ""):
            previous = service.get("查询模板版本")
        service["待重新发布"] = True
        service["最近模板动作"] = action
        service["最近模板动作时间"] = stamp
        _history(service, "历史消费版本").append({
            "version": previous,
            "动作": action,
            "操作人": operator,
            "时间": stamp,
        })
        sync_service_entry(service)
        affected_services.append(str(service.get("服务编号") or service.get("id")))

    for cache in store.rows(CACHE_MODULE):
        if normalize_code(cache.get("查询模板编号")) != code or not code:
            continue
        previous = cache.get("缓存版本")
        if previous in (None, ""):
            previous = cache.get("查询模板版本")
        cache["待失效"] = True
        cache["最近模板动作"] = action
        cache["最近模板动作时间"] = stamp
        _history(cache, "历史缓存版本").append({
            "version": previous,
            "动作": action,
            "操作人": operator,
            "时间": stamp,
        })
        sync_cache_entry(cache)
        affected_caches.append(str(cache.get("规则编号") or cache.get("id")))

    return {
        "version": effective,
        "活动版本": active,
        "数据服务": affected_services,
        "缓存规则": affected_caches,
        "时间": stamp,
    }


def mark_service_republished(entry: dict[str, Any], operator: str = "") -> dict[str, Any]:
    """数据服务重新发布后，消费版本追上审批通过版本，清掉待重新发布标记。"""
    template = find_template(entry.get("查询模板编号"))
    if template is None or template.get("status") != PUBLISHED:
        return sync_service_entry(entry)
    effective = approved_version(template)
    active = active_version(template)
    if active is None:
        return sync_service_entry(entry)
    entry["消费版本"] = active
    entry["待重新发布"] = False
    entry["最近重新发布时间"] = now_text()
    _history(entry, "历史消费版本").append({
        "version": active,
        "动作": "重新发布服务",
        "操作人": operator,
        "时间": entry["最近重新发布时间"],
    })
    return sync_service_entry(entry)


def mark_cache_refreshed(entry: dict[str, Any], operator: str = "") -> dict[str, Any]:
    """缓存规则重新启用后，缓存版本追上审批通过版本，解除待失效标记。"""
    template = find_template(entry.get("查询模板编号"))
    if template is None or template.get("status") != PUBLISHED:
        return sync_cache_entry(entry)
    effective = approved_version(template)
    active = active_version(template)
    if active is None:
        return sync_cache_entry(entry)
    entry["缓存版本"] = active
    entry["待失效"] = False
    entry["最近刷新时间"] = now_text()
    _history(entry, "历史缓存版本").append({
        "version": active,
        "动作": "刷新缓存",
        "操作人": operator,
        "时间": entry["最近刷新时间"],
    })
    return sync_cache_entry(entry)
