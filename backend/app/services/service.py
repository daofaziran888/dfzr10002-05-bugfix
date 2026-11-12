"""数据服务发布业务规则：状态流转、字段校验与下游消费口径都收在这里。

数据服务是查询模板的下游消费方：列表与详情里的模板版本一律按查询模板的
审批通过版本实时解析，不在数据服务记录里保留会过期的副本。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import template_links

MODULE = "service"
REQUIRED_FIELDS = ["服务编号", "服务名称", "输出数据集"]
STATUS_ORDER = ["草稿", "待审批", "已发布", "已停用"]
STATUS_DRAFT, STATUS_PENDING, STATUS_PUBLISHED, STATUS_STOPPED = STATUS_ORDER
NEGATIVE_ACTIONS = ["停用服务"]

TRANSITIONS: dict[str, set[str]] = {
    "提交审批": {STATUS_DRAFT, STATUS_STOPPED},
    "发布服务": {STATUS_PENDING},
    "重新发布服务": {STATUS_PUBLISHED},
    "停用服务": {STATUS_PUBLISHED},
}

TRANSITION_HINTS = {
    "提交审批": "只有草稿的数据服务可以提交审批",
    "发布服务": "只有待审批的数据服务可以发布",
    "重新发布服务": "只有已发布的数据服务可以重新发布",
    "停用服务": "只有已发布的数据服务可以停用",
}

LINK_FIELDS = ["查询模板编号", "查询模板版本", "消费版本"]


class ServiceService:
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
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
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
        entry["待重新发布"] = False
        entry["历史消费版本"] = []
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
            return None, f"数据服务 {entry_id} 不存在或已归档"
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于数据服务发布可执行范围"

        current = str(entry.get("status") or "")
        if current not in STATUS_ORDER:
            return None, f"数据服务当前状态「{current}」不在允许的状态序列里"
        if current not in TRANSITIONS[action]:
            return None, (
                f"当前状态「{current}」不能执行「{action}」："
                f"{TRANSITION_HINTS[action]}，状态只能按"
                f"{' → '.join(STATUS_ORDER)} 单向推进"
            )

        if action == "提交审批":
            entry["status"] = STATUS_PENDING
            entry["pending"] = True
        elif action == "发布服务":
            entry["status"] = STATUS_PUBLISHED
            entry["pending"] = False
            template_links.mark_service_republished(entry, operator)
        elif action == "重新发布服务":
            entry["status"] = STATUS_PUBLISHED
            entry["pending"] = False
            template_links.mark_service_republished(entry, operator)
        elif action == "停用服务":
            entry["status"] = STATUS_STOPPED
            entry["pending"] = False
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        self._decorate(entry)
        return entry, f"数据服务已{action}"

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        template_links.sync_service_entry(entry)
        return entry
