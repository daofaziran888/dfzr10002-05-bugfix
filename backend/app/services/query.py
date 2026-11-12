"""聚合查询配置业务规则：状态流转、审批记录与下游消费联动都收在这里。

状态只能按「草稿 → 待审批 → 已发布 → 已停用」单向推进，不允许跳级或回退；
唯一允许的显式回退是「撤回审批」，由待审批回到草稿，且撤回结论会保留在审批记录里。
查询模板是主对象：模板编号、审批状态与聚合查询模板版本都以这里的记录为准，
数据服务与缓存规则只是下游消费方，统一通过 template_links 解析审批通过版本。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import template_links

MODULE = "query"
REQUIRED_FIELDS = ["模板编号", "模板名称", "查询语句"]
STATUS_ORDER = ["草稿", "待审批", "已发布", "已停用"]
STATUS_DRAFT, STATUS_PENDING, STATUS_PUBLISHED, STATUS_STOPPED = STATUS_ORDER
RETRACT_ACTION = "撤回审批"
APPROVE_ACTION = "审批通过"

# 审批记录的字段口径：对外展示时历史结论按这套字段截取。
RECORD_KEYS = ("审批编号", "版本", "结论", "提交人", "提交时间", "审批人", "审批时间", "撤回人", "撤回时间")

# 允许的动作与来源状态：每个动作只接受紧邻的上一步，不做跳级。
TRANSITIONS: dict[str, set[str]] = {
    "提交审批": {STATUS_DRAFT, STATUS_STOPPED},
    RETRACT_ACTION: {STATUS_PENDING},
    APPROVE_ACTION: {STATUS_PENDING},
    "发布模板": {STATUS_PENDING},
    "停用模板": {STATUS_PUBLISHED},
}

TRANSITION_HINTS = {
    "提交审批": "只有草稿（或已停用后开启新版本）的查询模板可以提交审批",
    "撤回审批": "只有待审批的查询模板可以撤回审批",
    "审批通过": "只有待审批的查询模板可以执行审批通过",
    "发布模板": "只有待审批且审批通过的查询模板可以发布",
    "停用模板": "只有已发布的查询模板可以停用",
}


class QueryService:
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
            rows = [
                row
                for row in rows
                if keyword in str(row.get("模板编号", ""))
                or keyword in str(row.get("查询模板编号", ""))
            ]
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
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["模板版本"] = "v1"
        entry["版本序号"] = 0
        entry["已审批版本"] = None
        entry["已审批序号"] = 0
        entry["已发布版本"] = None
        entry["已发布序号"] = 0
        entry["审批记录"] = []
        entry["历史审批结论"] = []
        entry["消费记录"] = []
        entry["当前审批记录"] = None
        rows.append(entry)
        self._decorate(entry)
        return entry, []

    # ---------- 状态动作 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档"
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于聚合查询配置可执行范围"

        current = str(entry.get("status") or "")
        if current not in STATUS_ORDER:
            return None, f"查询模板当前状态「{current}」不在允许的状态序列里"
        if current not in TRANSITIONS[action]:
            return None, (
                f"当前状态「{current}」不能执行「{action}」："
                f"{TRANSITION_HINTS[action]}，状态只能按"
                f"{' → '.join(STATUS_ORDER)} 单向推进"
            )

        # 先完成全部校验，再统一落库；任何一步报错都不改动记录，避免残留旧版本。
        if action == "提交审批":
            return self._submit(entry, operator)
        if action == RETRACT_ACTION:
            return self._retract(entry, operator)
        if action == APPROVE_ACTION:
            return self._approve(entry, operator)
        if action == "发布模板":
            return self._publish(entry, operator)
        if action == "停用模板":
            return self._stop(entry, operator)
        return None, f"动作「{action}」尚未接入聚合查询配置状态流转"

    def _submit(self, entry: dict[str, Any], operator: str) -> tuple[dict[str, Any], str]:
        # 从已停用重新提交时开启新一轮版本，旧版本与历史审批结论全部保留。
        seq = int(entry.get("版本序号") or 0) + 1
        version = f"v{seq}"
        code = str(entry.get("模板编号") or entry.get("查询模板编号") or entry["id"])
        record = {
            "审批编号": f"{code}-AP-{seq}",
            "版本": version,
            "序号": seq,
            "结论": STATUS_PENDING,
            "提交人": operator or "值班管理员",
            "提交时间": template_links.now_text(),
        }
        entry["版本序号"] = seq
        entry["模板版本"] = version
        entry["审批记录"] = list(entry.get("审批记录") or [])
        entry["审批记录"].append(record)
        entry["当前审批记录"] = record
        entry["审批结论"] = STATUS_PENDING
        entry["status"] = STATUS_PENDING
        entry["查询模板审批状态"] = STATUS_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近提交时间"] = record["提交时间"]
        self._decorate(entry)
        return entry, f"查询模板已提交审批，生成审批记录 {record['审批编号']}（版本 {version}）"

    def _retract(self, entry: dict[str, Any], operator: str) -> tuple[dict[str, Any] | None, str]:
        record = self._pending_record(entry)
        if record is None:
            return None, "当前没有待审批的审批记录，无法撤回审批"
        stamp = template_links.now_text()
        record["结论"] = "已撤回"
        record["撤回时间"] = stamp
        record["撤回人"] = operator or "值班管理员"
        self._remember_conclusion(entry, record)
        entry["当前审批记录"] = None
        entry["审批结论"] = "已撤回"
        entry["status"] = STATUS_DRAFT
        entry["查询模板审批状态"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近撤回时间"] = stamp
        self._decorate(entry)
        return entry, f"查询模板已撤回审批，记录 {record.get('审批编号')} 保留为已撤回"

    def _approve(self, entry: dict[str, Any], operator: str) -> tuple[dict[str, Any] | None, str]:
        record = self._pending_record(entry)
        if record is None:
            return None, "当前没有待审批的审批记录，无法执行审批通过"
        stamp = template_links.now_text()
        record["结论"] = "审批通过"
        record["审批时间"] = stamp
        record["审批人"] = operator or "值班管理员"
        self._remember_conclusion(entry, record)
        entry["已审批版本"] = record["版本"]
        entry["已审批序号"] = int(record.get("序号") or entry.get("版本序号") or 0)
        entry["审批结论"] = "审批通过"
        entry["status"] = STATUS_PENDING
        entry["查询模板审批状态"] = STATUS_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        self._decorate(entry)
        return entry, f"查询模板版本 {record['版本']} 审批通过，等待发布"

    def _publish(self, entry: dict[str, Any], operator: str) -> tuple[dict[str, Any] | None, str]:
        record = self._pending_record(entry)
        current_seq = int(entry.get("版本序号") or 0)
        approved = entry.get("已审批版本")
        approved_seq = int(entry.get("已审批序号") or 0)
        stamp = template_links.now_text()

        # 发布必须建立在审批通过之上：草稿或只是提交待审，都不能直接发布。
        approved_is_current = approved not in (None, "") and approved_seq == current_seq
        if not approved_is_current:
            return None, "当前版本尚未审批通过，不能发布；请先完成审批通过"
        if record is not None and record.get("序号") != current_seq:
            record = None

        if record is not None and record.get("结论") == STATUS_PENDING:
            if str(record.get("版本")) == str(approved):
                # 已单独审批通过：发布时补齐审批结论，保证审批记录与已发布版本对得上。
                record["结论"] = "审批通过"
                record.setdefault("审批时间", stamp)
                record.setdefault("审批人", operator or "值班管理员")
                self._remember_conclusion(entry, record)
            else:
                # 同一轮里已有审批通过版本：以它为准，待审版本作废，但审批记录保留。
                record["结论"] = "已失效（以审批通过版本为准）"
                record["失效时间"] = stamp
                self._remember_conclusion(entry, record)

        # 只有一个版本能进入消费清单：审批通过版本优先于任何已发布版本。
        published = approved
        superseded = entry.get("已发布版本")
        if superseded not in (None, "") and str(superseded) != str(published):
            entry["冲突说明"] = f"已发布版本 {superseded} 与审批通过版本 {published} 冲突，以审批通过版本为准"
        entry["已发布版本"] = published
        entry["已发布序号"] = int(entry.get("已审批序号") or current_seq)
        entry["模板版本"] = published
        entry["聚合查询模板版本"] = published
        entry["审批结论"] = "审批通过"
        entry["status"] = STATUS_PUBLISHED
        entry["查询模板审批状态"] = STATUS_PUBLISHED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["当前审批记录"] = None
        entry["最近发布时间"] = stamp
        entry["发布记录"] = list(entry.get("发布记录") or [])
        entry["发布记录"].append({
            "版本": published,
            "动作": "发布模板",
            "操作人": operator or "值班管理员",
            "时间": stamp,
        })
        downstream = template_links.mark_downstream(entry, "发布模板", operator)
        entry["消费记录"] = list(entry.get("消费记录") or [])
        entry["消费记录"].append(downstream)
        entry["当前消费版本"] = published
        self._decorate(entry)
        message = f"查询模板已发布（版本 {published}）"
        if downstream["数据服务"] or downstream["缓存规则"]:
            message += (
                f"；{len(downstream['数据服务'])} 个数据服务转为待重新发布，"
                f"{len(downstream['缓存规则'])} 条缓存规则转为待失效"
            )
        return entry, message

    def _stop(self, entry: dict[str, Any], operator: str) -> tuple[dict[str, Any], str]:
        stamp = template_links.now_text()
        entry["status"] = STATUS_STOPPED
        entry["查询模板审批状态"] = STATUS_STOPPED
        entry["pending"] = False
        entry["abnormal"] = True
        entry["当前审批记录"] = None
        entry["最近停用时间"] = stamp
        downstream = template_links.mark_downstream(entry, "停用模板", operator)
        entry["消费记录"] = list(entry.get("消费记录") or [])
        entry["消费记录"].append(downstream)
        entry["当前消费版本"] = template_links.approved_version(entry)
        self._decorate(entry)
        message = "查询模板已停用"
        if downstream["数据服务"] or downstream["缓存规则"]:
            message += (
                f"；{len(downstream['数据服务'])} 个数据服务转为待重新发布，"
                f"{len(downstream['缓存规则'])} 条缓存规则转为待失效"
            )
        return entry, message

    # ---------- 字段整理 ----------

    def _pending_record(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        record = entry.get("当前审批记录")
        if not isinstance(record, dict):
            return None
        if record.get("结论") != STATUS_PENDING:
            return None
        return record

    @staticmethod
    def _remember_conclusion(entry: dict[str, Any], record: dict[str, Any]) -> None:
        """把审批结论留档；同一条审批记录不重复追加。"""
        history = list(entry.get("历史审批结论") or [])
        record_id = record.get("审批编号")
        if record_id and any(item.get("审批编号") == record_id for item in history):
            history = [item for item in history if item.get("审批编号") != record_id]
        history.append({key: record.get(key) for key in RECORD_KEYS})
        entry["历史审批结论"] = history

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """把主对象的三个口径字段固定下来，列表、详情与下游都读同一份。

        模板版本是当前工作版本（提交审批时分配）；聚合查询模板版本是审批通过版本，
        审批通过版本与已发布版本冲突时以审批通过版本为准。
        """
        code = entry.get("模板编号") or entry.get("查询模板编号")
        entry["查询模板编号"] = code
        entry["查询模板审批状态"] = entry.get("status")
        entry["聚合查询模板版本"] = template_links.approved_version(entry)
        return entry
