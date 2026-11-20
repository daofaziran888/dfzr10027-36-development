"""调度运行流水业务规则：按依赖调度关联聚合任务、记录每轮实际产物。

语义约定（与调度编排的「本轮执行」对应）：

- 一轮调度 = 一条 ``_schedule_runs`` 记录。``待执行 -> 运行中 -> 已完成``；
  窗口名额用尽但还有未完成任务时停在「运行中」，可再次恢复。
- 依赖取数只认本轮流水的交付台账：上游数据源在轮次开始时做一次校验登记，
  聚合任务产生的产物在计算完成时登记。下游只读取台账里本轮已校验的版本。
- 窗口名额按「并发上限」发放：依赖已满足的任务按就绪先后排队，一次只放一个，
  因此只剩一个名额时等待最久的那条先拿到；名额用尽后其余任务停在「待计算」，
  它们下游的任务标记为「依赖阻塞」。
- 计算完成后把实际产物版本回写到本轮次，供下游与详情页核对。
- 已交付的任务不会被重算：恢复执行时直接复用流水中的版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.metric_compute import compute_metric
from app.services.schedule_plan import SchedulePlanService
from app.store import store

RUNS_TABLE = "_schedule_runs"
PRODUCT_TABLE = "_schedule_run_products"
LEDGER_TABLE = "_schedule_run_ledger"

RUN_PENDING = "待执行"
RUN_RUNNING = "运行中"
RUN_COMPLETED = "已完成"

TASK_PENDING = "待计算"
TASK_DELIVERED = "已交付"
TASK_BLOCKED = "依赖阻塞"
TASK_WAITING = "等待名额"

# 已经交付的任务不再进入推进循环
_FINISHED = (TASK_DELIVERED,)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ScheduleRunService:
    """调度计划的本轮执行流水。"""

    def __init__(self) -> None:
        self.plan_service = SchedulePlanService()

    # ------------------------------------------------------------------
    # 存储访问
    # ------------------------------------------------------------------
    def _runs(self) -> list[dict[str, Any]]:
        return store.rows(RUNS_TABLE)

    def _products(self) -> list[dict[str, Any]]:
        return store.rows(PRODUCT_TABLE)

    def _ledger(self) -> list[dict[str, Any]]:
        return store.rows(LEDGER_TABLE)

    def current_run(self, plan_id: int) -> dict[str, Any] | None:
        runs = [run for run in self._runs() if run.get("计划id") == plan_id]
        if not runs:
            return None
        return max(runs, key=lambda run: int(run.get("轮次", 0)))

    # ------------------------------------------------------------------
    # 计划与任务的静态信息
    # ------------------------------------------------------------------
    @staticmethod
    def _split_tasks(plan: dict[str, Any]) -> list[str]:
        raw = str(plan.get("关联任务") or "")
        return [code.strip() for code in raw.replace("；", ",").split(",") if code.strip()]

    @staticmethod
    def _agg_task(task_code: str) -> dict[str, Any] | None:
        for row in store.rows("agg_task"):
            if row.get("任务编号") == task_code:
                return row
        return None

    def _deps(self, task_code: str) -> list[str]:
        task = self._agg_task(task_code)
        if task is None:
            return []
        raw = str(task.get("依赖上游") or "")
        return [code.strip() for code in raw.replace("；", ",").split(",") if code.strip()]

    def _window_capacity(self, plan: dict[str, Any]) -> int:
        try:
            capacity = int(plan.get("并发上限") or 1)
        except (TypeError, ValueError):
            capacity = 1
        return max(capacity, 1)

    def _dependency_check(self, plan: dict[str, Any]) -> bool:
        return str(plan.get("依赖检查") or "").strip() == "是"

    # ------------------------------------------------------------------
    # 轮次初始化
    # ------------------------------------------------------------------
    def start_run(self, plan_id: int) -> tuple[dict[str, Any] | None, str]:
        plan = self.plan_service.get_entry(plan_id)
        if plan is None:
            return None, f"调度计划 {plan_id} 不存在或已归档"
        task_codes = self._split_tasks(plan)
        if not task_codes:
            return None, "调度计划未登记关联任务，无法生成本轮执行"
        missing = [code for code in task_codes if self._agg_task(code) is None]
        if missing:
            return None, f"关联任务未在聚合任务中登记：{'、'.join(missing)}"
        existing = self.current_run(plan_id)
        if existing is not None and existing.get("状态") == RUN_RUNNING:
            return self._view(existing), "第 {} 轮仍在执行中，请用恢复执行接续未完成任务".format(existing.get("轮次"))

        run = self._new_run(plan)
        self._prepare_external_sources(run, plan)
        self._advance(run)
        return self._view(run), "本轮调度执行已启动"

    def _new_run(self, plan: dict[str, Any]) -> dict[str, Any]:
        runs = self._runs()
        round_no = max(
            (int(run.get("轮次", 0)) for run in runs if run.get("计划id") == plan.get("id")),
            default=0,
        ) + 1
        capacity = self._window_capacity(plan)
        run = {
            "id": max((int(item.get("id", 0)) for item in runs), default=0) + 1,
            "计划id": plan.get("id"),
            "计划编号": plan.get("计划编号"),
            "轮次": round_no,
            "状态": RUN_PENDING,
            "开始时间": None,
            "更新时间": None,
            "窗口序号": 1,
            "并发上限": capacity,
            "窗口名额": capacity,
            "剩余名额": capacity,
            "依赖检查": self._dependency_check(plan),
            "任务状态": [
                {
                    "聚合任务": code,
                    "目标指标": (self._agg_task(code) or {}).get("目标指标"),
                    "依赖上游": self._deps(code),
                    "状态": TASK_PENDING,
                    "就绪序号": None,
                    "产物版本": None,
                    "产物编号": None,
                    "取数来源": [],
                    "阻断原因": None,
                }
                for code in self._split_tasks(plan)
            ],
            "上游台账": [],
            "阶段记录": [],
            "_clock": 0,
        }
        runs.append(run)
        return run

    def _prepare_external_sources(self, run: dict[str, Any], plan: dict[str, Any]) -> None:
        """轮次开始时把外部数据源登记进台账；未开启依赖检查时不登记，下游自然阻塞。"""
        checked = self._dependency_check(plan)
        for state in run["任务状态"]:
            for dep in state["依赖上游"]:
                if self._agg_task(dep) is not None:
                    continue  # 聚合任务之间的依赖由交付时登记
                if any(item["依赖"] == dep for item in run["上游台账"]):
                    continue
                version = f"{dep}-R{run['轮次']}-READY"
                run["上游台账"].append({
                    "依赖": dep,
                    "类型": "数据源",
                    "版本": version if checked else None,
                    "已校验": checked,
                    "校验时间": _now() if checked else None,
                })
                self._ledger().append({
                    "计划编号": run["计划编号"],
                    "轮次": run["轮次"],
                    "依赖": dep,
                    "类型": "数据源",
                    "版本": version if checked else None,
                    "已校验": checked,
                    "校验时间": _now() if checked else None,
                })

    # ------------------------------------------------------------------
    # 台账与就绪判断
    # ------------------------------------------------------------------
    def _delivered_versions(self, run: dict[str, Any]) -> dict[str, str]:
        """本轮已交付且已校验的版本：既是下游取数依据，也是「不重算」的依据。"""
        versions: dict[str, str] = {}
        for item in run["上游台账"]:
            if item.get("已校验") and item.get("版本"):
                versions[str(item["依赖"])] = str(item["版本"])
        for state in run["任务状态"]:
            if state["状态"] == TASK_DELIVERED and state.get("产物版本"):
                versions[state["聚合任务"]] = str(state["产物版本"])
        return versions

    def _missing_deps(self, run: dict[str, Any], state: dict[str, Any]) -> list[str]:
        delivered = self._delivered_versions(run)
        return [dep for dep in state["依赖上游"] if dep not in delivered]

    def _tick(self, run: dict[str, Any]) -> int:
        run["_clock"] = int(run.get("_clock", 0)) + 1
        return run["_clock"]

    def _ready(self, run: dict[str, Any], state: dict[str, Any]) -> None:
        """标记任务依赖已满足、进入就绪队列，就绪序号用于「等待最久优先」。"""
        if state.get("就绪序号") is None:
            state["就绪序号"] = self._tick(run)
        state["状态"] = TASK_WAITING
        state["阻断原因"] = None

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------
    def _advance(self, run: dict[str, Any]) -> None:
        if run["状态"] == RUN_COMPLETED:
            return
        if run["状态"] == RUN_PENDING:
            run["状态"] = RUN_RUNNING
            run["开始时间"] = _now()
        self._record(run, "推进本轮", f"窗口 {run['窗口序号']} 剩余名额 {run['剩余名额']}")

        while True:
            # 1) 先按本轮台账重算依赖就绪状态
            for state in run["任务状态"]:
                if state["状态"] in _FINISHED:
                    continue
                missing = self._missing_deps(run, state)
                if missing:
                    state["状态"] = TASK_BLOCKED
                    state["阻断原因"] = f"上游未在本轮交付：{'、'.join(missing)}"
                elif state["状态"] in (TASK_BLOCKED, TASK_PENDING):
                    self._ready(run, state)

            if int(run["剩余名额"]) <= 0:
                break

            # 2) 只剩一个名额时，就绪最早的任务先拿：等待最久优先
            waiting = [state for state in run["任务状态"] if state["状态"] == TASK_WAITING]
            if not waiting:
                break
            waiting.sort(key=lambda state: (int(state.get("就绪序号") or 0),))
            self._execute(run, waiting[0])

        self._finalize(run)

    def _execute(self, run: dict[str, Any], state: dict[str, Any]) -> None:
        task_code = str(state["聚合任务"])
        metric_code = str(state.get("目标指标") or "")
        versions = self._delivered_versions(run)
        snapshot = {dep: versions.get(dep) for dep in state["依赖上游"]}

        result = compute_metric(metric_code) if metric_code else {"error": "未绑定目标指标"}
        if "error" in result:
            state["状态"] = TASK_BLOCKED
            state["阻断原因"] = str(result["error"])
            return

        run["剩余名额"] = int(run["剩余名额"]) - 1
        version = f"{task_code}-v{len([p for p in self._products() if p['聚合任务'] == task_code]) + 1}"
        product = {
            "产物编号": f"{run['计划编号']}-R{run['轮次']}-{task_code}",
            "产物版本": version,
            "计划编号": run["计划编号"],
            "轮次": run["轮次"],
            "聚合任务": task_code,
            "目标指标": metric_code,
            "指标结果": result.get("value"),
            "指标单位": result.get("unit"),
            "取数来源": list(state["依赖上游"]),
            "上游快照": snapshot,
            "产出时间": _now(),
        }
        self._products().append(product)
        self._ledger().append({
            "计划编号": run["计划编号"],
            "轮次": run["轮次"],
            "依赖": task_code,
            "类型": "聚合产物",
            "版本": version,
            "已校验": True,
            "校验时间": _now(),
        })
        run["上游台账"].append({
            "依赖": task_code, "类型": "聚合产物", "版本": version, "已校验": True, "校验时间": _now(),
        })

        state["状态"] = TASK_DELIVERED
        state["产物版本"] = version
        state["产物编号"] = product["产物编号"]
        state["取数来源"] = list(snapshot)
        state["阻断原因"] = None
        self._record(run, "交付产物", f"{task_code} -> {version}")

    def _finalize(self, run: dict[str, Any]) -> None:
        states = run["任务状态"]
        for state in states:
            if state["状态"] in (TASK_PENDING, TASK_WAITING):
                state["状态"] = TASK_WAITING
                state["阻断原因"] = "窗口名额已用尽，等待恢复后继续"
        if states and all(state["状态"] == TASK_DELIVERED for state in states):
            run["状态"] = RUN_COMPLETED
            self._record(run, "本轮完成", "全部关联聚合任务已交付")
        else:
            run["状态"] = RUN_RUNNING
        run["更新时间"] = _now()

    def _record(self, run: dict[str, Any], action: str, detail: str) -> None:
        run["阶段记录"].append({
            "时间": _now(), "轮次": run["轮次"], "窗口": run["窗口序号"],
            "动作": action, "说明": detail,
        })

    # ------------------------------------------------------------------
    # 恢复：同一轮次开新窗口，只推进未完成任务
    # ------------------------------------------------------------------
    def resume_run(self, plan_id: int) -> tuple[dict[str, Any] | None, str]:
        run = self.current_run(plan_id)
        if run is None:
            return None, "当前调度计划还没有本轮执行记录，请先启动调度"
        if run["状态"] == RUN_COMPLETED:
            return None, "本轮调度已全部完成，无需恢复"
        capacity = int(run.get("并发上限") or 1)
        run["窗口序号"] = int(run.get("窗口序号", 1)) + 1
        run["窗口名额"] = capacity
        run["剩余名额"] = capacity
        run["状态"] = RUN_RUNNING
        self._record(run, "恢复执行", f"同一轮次开启窗口 {run['窗口序号']}，接续未完成任务")
        self._advance(run)
        return self._view(run), "本轮调度已恢复，接续未完成任务"

    # ------------------------------------------------------------------
    # 详情与产物
    # ------------------------------------------------------------------
    def _round_products(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = f"{run['计划编号']}-R{run['轮次']}-"
        codes = {state["聚合任务"] for state in run["任务状态"]}
        return [
            dict(item) for item in self._products()
            if str(item.get("产物编号", "")).startswith(prefix) and item.get("聚合任务") in codes
        ]

    def _view(self, run: dict[str, Any]) -> dict[str, Any]:
        states = [dict(state) for state in run["任务状态"]]
        delivered = [state["聚合任务"] for state in states if state["状态"] == TASK_DELIVERED]
        pending = [state["聚合任务"] for state in states if state["状态"] in (TASK_PENDING, TASK_WAITING)]
        blocked = [state["聚合任务"] for state in states if state["状态"] == TASK_BLOCKED]
        return {
            "计划id": run.get("计划id"),
            "计划编号": run.get("计划编号"),
            "轮次": run.get("轮次"),
            "状态": run.get("状态"),
            "开始时间": run.get("开始时间"),
            "更新时间": run.get("更新时间"),
            "窗口序号": run.get("窗口序号"),
            "并发上限": run.get("并发上限"),
            "窗口名额": run.get("窗口名额"),
            "剩余名额": run.get("剩余名额"),
            "依赖检查": run.get("依赖检查"),
            "任务": states,
            "派生产物": self._round_products(run),
            "已交付": delivered,
            "待计算": pending,
            "依赖阻塞": blocked,
            "上游台账": [dict(item) for item in run.get("上游台账", [])],
            "阶段记录": [dict(item) for item in run.get("阶段记录", [])],
        }

    def get_detail(self, plan_id: int) -> dict[str, Any] | None:
        run = self.current_run(plan_id)
        return None if run is None else self._view(run)

    def list_products(self, plan_id: int) -> list[dict[str, Any]]:
        run = self.current_run(plan_id)
        return [] if run is None else self._round_products(run)


service = ScheduleRunService()
