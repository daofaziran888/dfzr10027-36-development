"""调度编排接口：维护调度计划，覆盖激活计划、立即执行、停用计划等动作。

本轮的按依赖执行走独立接口：启动 / 恢复 / 查询本轮执行详情与产物流水。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.schedule_plan import SchedulePlanService
from app.services.schedule_run import service as run_service

router = APIRouter(prefix="/api/schedule_plan", tags=["调度编排"])

service = SchedulePlanService()

LIST_FIELDS = ["计划编号", "关联任务", "Cron表达式", "时区设置", "失败通知", "并发上限", "依赖检查", "下次执行"]
STATUSES = ["待激活", "运行中", "已完成", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="待激活、运行中、已完成、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、计划编号关键字与状态过滤调度编排列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="schedule_plan.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条调度计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"调度计划 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条调度计划，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="调度计划已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条调度计划执行激活计划、立即执行、停用计划；不允许的动作会被拦下并说明原因。

    「立即执行」会在改状态之外真正启动本轮流水；关联任务没登记或本轮已在下发时，
    状态照常流转，流水侧的说明拼回消息里，便于前端提示。
    """
    action = payload.action.strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    if action == "立即执行":
        _, run_message = run_service.start_run(entry_id)
        message = f"{message}；{run_message}"
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------------------------------------------------------------------
# 本轮执行流水：按依赖调度关联聚合任务
# ---------------------------------------------------------------------------

@router.post("/{entry_id}/runs", response_model=ActionResult)
def start_run(entry_id: int) -> ActionResult:
    """启动本轮调度：按依赖放行关联聚合任务，产物版本回写到本轮流水。"""
    detail, message = run_service.start_run(entry_id)
    if detail is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=detail)


@router.post("/{entry_id}/runs/resume", response_model=ActionResult)
def resume_run(entry_id: int) -> ActionResult:
    """恢复同一轮调度：接续未完成任务，已确认交付的结果原样保留。"""
    detail, message = run_service.resume_run(entry_id)
    if detail is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=detail)


@router.get("/{entry_id}/runs/current")
def get_current_run(entry_id: int) -> dict:
    """读取当前轮次执行详情：已交付、待计算、依赖阻塞与派生产物。"""
    detail = run_service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"调度计划 {entry_id} 还没有本轮执行流水")
    return detail


@router.get("/{entry_id}/runs/current/products")
def list_run_products(entry_id: int) -> dict[str, object]:
    """列出本轮已产出的聚合产物，含实际产物版本与取数依据。"""
    return {"items": run_service.list_products(entry_id)}
