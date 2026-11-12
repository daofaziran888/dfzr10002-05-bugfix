"""聚合查询配置接口：维护查询模板，覆盖提交审批、审批通过、发布模板、停用模板等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.query import QueryService
from app.services import template_links

router = APIRouter(prefix="/api/query", tags=["聚合查询配置"])

service = QueryService()

LIST_FIELDS = ["模板编号", "模板名称", "查询语句", "参数定义", "缓存时间", "结果上限", "执行超时", "模板版本"]
STATUSES = ["草稿", "待审批", "已发布", "已停用"]
ACTIONS = ["提交审批", "撤回审批", "审批通过", "发布模板", "停用模板"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待审批、已发布、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤聚合查询配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合查询配置清单：返回当前过滤条件下的全量数据。

    必须声明在 /{entry_id} 之前，否则 "export" 会被当成路径参数而报 422。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "query", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/consumers", response_model=dict)
def list_consumers(entry_id: int) -> dict:
    """查询模板的下游消费清单：数据服务与缓存规则统一按审批通过版本展示。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    code = entry.get("查询模板编号") or entry.get("模板编号")
    services = [
        template_links.sync_service_entry(row)
        for row in template_links.store.rows(template_links.SERVICE_MODULE)
        if template_links.normalize_code(row.get("查询模板编号")) == template_links.normalize_code(code)
    ]
    caches = [
        template_links.sync_cache_entry(row)
        for row in template_links.store.rows(template_links.CACHE_MODULE)
        if template_links.normalize_code(row.get("查询模板编号")) == template_links.normalize_code(code)
    ]
    return {
        "模板编号": code,
        "查询模板审批状态": entry.get("查询模板审批状态"),
        "聚合查询模板版本": entry.get("聚合查询模板版本"),
        "待重新发布数据服务": [item.get("服务编号") for item in services if item.get("待重新发布")],
        "待失效缓存规则": [item.get("规则编号") for item in caches if item.get("待失效")],
        "数据服务": services,
        "缓存规则": caches,
        "消费记录": entry.get("消费记录") or [],
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条查询模板执行状态动作；跳级、回退与未知动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    operator = str(payload.values.get("operator") or "").strip()
    entry, message = service.run_action(entry_id, action, operator)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
