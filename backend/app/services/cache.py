"""查询缓存管理业务规则：状态流转、字段校验与缓存口径都收在这里。

缓存规则是查询模板的下游消费方：引用版本与失效标记都按查询模板审批通过版本实时解析。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import template_links

MODULE = "cache"
REQUIRED_FIELDS = ["规则编号", "缓存目标", "缓存时间"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
STATUS_DRAFT, STATUS_PENDING, STATUS_ENABLED, STATUS_STOPPED = STATUS_ORDER
NEGATIVE_ACTIONS = ["停用配置"]

TRANSITIONS: dict[str, set[str]] = {
    "提交校验": {STATUS_DRAFT, STATUS_STOPPED},
    "启用配置": {STATUS_PENDING},
    "刷新缓存": {STATUS_ENABLED, STATUS_STOPPED},
    "停用配置": {STATUS_ENABLED},
}

TRANSITION_HINTS = {
    "提交校验": "只有草稿的缓存规则可以提交校验",
    "启用配置": "只有待校验的缓存规则可以启用",
    "刷新缓存": "只有已启用或已停用的缓存规则可以刷新缓存",
    "停用配置": "只有已启用的缓存规则可以停用",
}

LINK_FIELDS = ["查询模板编号", "查询模板版本", "缓存版本"]


class CacheService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        for row in page_rows:
            self._decorate(row)
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._decorate(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in LINK_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["待失效"] = False
        entry["历史缓存版本"] = []
        rows.append(entry)
        self._decorate(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于查询缓存管理可执行范围"

        current = str(entry.get("status") or "")
        if current not in STATUS_ORDER:
            return None, f"缓存规则当前状态「{current}」不在允许的状态序列里"
        if current not in TRANSITIONS[action]:
            return None, (
                f"当前状态「{current}」不能执行「{action}」："
                f"{TRANSITION_HINTS[action]}，状态只能按"
                f"{' → '.join(STATUS_ORDER)} 单向推进"
            )

        if action == "提交校验":
            entry["status"] = STATUS_PENDING
            entry["pending"] = True
        elif action == "启用配置":
            entry["status"] = STATUS_ENABLED
            entry["pending"] = False
            template_links.mark_cache_refreshed(entry, operator)
        elif action == "刷新缓存":
            entry["status"] = STATUS_ENABLED
            entry["pending"] = False
            template_links.mark_cache_refreshed(entry, operator)
        elif action == "停用配置":
            entry["status"] = STATUS_STOPPED
            entry["pending"] = False
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        self._decorate(entry)
        return entry, f"缓存规则已{action}"

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        template_links.sync_cache_entry(entry)
        return entry
