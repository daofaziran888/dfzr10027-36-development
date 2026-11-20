"""调度编排本轮执行测试：依赖调度、名额冲突、产物流水与恢复接续。"""
from __future__ import annotations

from app.services.schedule_run import (
    TASK_BLOCKED,
    TASK_DELIVERED,
    TASK_WAITING,
    ScheduleRunService,
)
from app.store import store

SERVICE = ScheduleRunService()


def seed_plan(plan_id: int, plan_code: str, tasks: list[str], capacity: int = 2, check: str = "是") -> int:
    """登记一条只包含给定聚合任务的调度计划，返回计划 id。"""
    rows = store.rows("schedule_plan")
    entry = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "status": "待激活",
        "pending": True,
        "abnormal": False,
        "计划编号": plan_code,
        "关联任务": ",".join(tasks),
        "Cron表达式": "0 3 * * 1",
        "时区设置": "Asia/Shanghai",
        "失败通知": "邮件",
        "并发上限": str(capacity),
        "依赖检查": check,
        "下次执行": "2026-09-01 03:00",
    }
    rows.append(entry)
    return int(entry["id"])


def seed_agg(task_code: str, metric: str, deps: list[str] | None = None) -> None:
    rows = store.rows("agg_task")
    row = {
        "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
        "status": "待调度",
        "pending": True,
        "abnormal": False,
        "任务编号": task_code,
        "目标指标": metric,
        "聚合维度": "DIM-REGION",
        "计算周期": "日",
        "依赖上游": ",".join(deps or []),
        "并行度": "2",
        "超时分钟": "20",
        "重试上限": "2",
    }
    rows.append(row)


def task_state(detail: dict, task_code: str) -> dict:
    return next(state for state in detail["任务"] if state["聚合任务"] == task_code)


class TestDependencyOrder:
    def test_downstream_waits_for_checked_upstream(self):
        seed_agg("T-UP", "METR-UV", ["page_views"])
        seed_agg("T-DOWN", "METR-CVR", ["T-UP"])
        plan_id = seed_plan(1, "SCHE-T-ORDER", ["T-UP", "T-DOWN"], capacity=2)
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        assert task_state(detail, "T-UP")["状态"] == TASK_DELIVERED
        assert task_state(detail, "T-DOWN")["状态"] == TASK_DELIVERED
        versions = {product["聚合任务"]: product["产物版本"] for product in detail["派生产物"]}
        assert versions == {"T-UP": "T-UP-v1", "T-DOWN": "T-DOWN-v1"}

    def test_downstream_reads_this_round_snapshot(self):
        seed_agg("T-UP", "METR-UV", ["page_views"])
        seed_agg("T-DOWN", "METR-CVR", ["T-UP"])
        plan_id = seed_plan(1, "SCHE-T-SNAPSHOT", ["T-UP", "T-DOWN"], capacity=2)
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        product = next(item for item in detail["派生产物"] if item["聚合任务"] == "T-DOWN")
        assert product["上游快照"] == {"T-UP": "T-UP-v1"}
        assert product["取数来源"] == ["T-UP"]

    def test_external_source_without_dependency_check_blocks_downstream(self):
        seed_agg("T-SRC", "METR-UV", ["page_views"])
        plan_id = seed_plan(1, "SCHE-T-NOCHECK", ["T-SRC"], capacity=2, check="否")
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        state = task_state(detail, "T-SRC")
        assert state["状态"] == TASK_BLOCKED
        assert "page_views" in state["阻断原因"]

    def test_unknown_linked_task_is_rejected(self):
        plan_id = seed_plan(1, "SCHE-T-UNKNOWN", ["NO-SUCH-TASK"], capacity=1)
        detail, message = SERVICE.start_run(plan_id)
        assert detail is None
        assert "NO-SUCH-TASK" in message


class TestWindowCapacity:
    def test_single_slot_lets_longest_waiting_go_first(self):
        # 独立指标先就绪，一组上下游指标后绪；窗口只有 1 个名额
        seed_agg("T-INDEPENDENT", "METR-GMV", ["order_events"])
        seed_agg("T-PARENT", "METR-UV", ["page_views"])
        seed_agg("T-CHILD", "METR-CVR", ["T-PARENT"])
        plan_id = seed_plan(1, "SCHE-T-CAP", ["T-INDEPENDENT", "T-PARENT", "T-CHILD"], capacity=1)
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        assert detail["状态"] == "运行中"
        assert detail["已交付"] == ["T-INDEPENDENT"]
        assert task_state(detail, "T-PARENT")["状态"] == TASK_WAITING
        assert task_state(detail, "T-CHILD")["状态"] == TASK_BLOCKED
        assert detail["窗口名额"] == 1
        assert detail["剩余名额"] == 0

    def test_resume_delivers_remaining_tasks(self):
        seed_agg("T-INDEPENDENT", "METR-GMV", ["order_events"])
        seed_agg("T-PARENT", "METR-UV", ["page_views"])
        seed_agg("T-CHILD", "METR-CVR", ["T-PARENT"])
        plan_id = seed_plan(1, "SCHE-T-RESUME", ["T-INDEPENDENT", "T-PARENT", "T-CHILD"], capacity=1)
        SERVICE.start_run(plan_id)
        detail, _ = SERVICE.resume_run(plan_id)
        assert detail is not None
        assert detail["轮次"] == 1
        assert detail["窗口序号"] == 2
        assert detail["已交付"] == ["T-INDEPENDENT", "T-PARENT"]
        # 上游刚交付但窗口名额已用尽：进入待计算，不再算依赖阻塞
        assert task_state(detail, "T-CHILD")["状态"] == TASK_WAITING
        detail, _ = SERVICE.resume_run(plan_id)
        assert detail is not None
        assert detail["状态"] == "已完成"
        assert detail["待计算"] == []
        assert detail["依赖阻塞"] == []

    def test_completed_results_are_not_recomputed_on_resume(self):
        seed_agg("T-INDEPENDENT", "METR-GMV", ["order_events"])
        seed_agg("T-PARENT", "METR-UV", ["page_views"])
        plan_id = seed_plan(1, "SCHE-T-KEEP", ["T-INDEPENDENT", "T-PARENT"], capacity=1)
        first, _ = SERVICE.start_run(plan_id)
        assert first is not None
        kept = next(item for item in first["派生产物"] if item["聚合任务"] == "T-INDEPENDENT")
        second, _ = SERVICE.resume_run(plan_id)
        assert second is not None
        products = [item for item in second["派生产物"] if item["聚合任务"] == "T-INDEPENDENT"]
        assert len(products) == 1
        assert products[0]["产物版本"] == kept["产物版本"]
        assert products[0]["产出时间"] == kept["产出时间"]
        assert products[0]["指标结果"] == kept["指标结果"]

    def test_capacity_conflict_is_reported_per_task(self):
        seed_agg("T-INDEPENDENT", "METR-GMV", ["order_events"])
        seed_agg("T-PARENT", "METR-UV", ["page_views"])
        seed_agg("T-CHILD", "METR-CVR", ["T-PARENT"])
        plan_id = seed_plan(1, "SCHE-T-VIEW", ["T-INDEPENDENT", "T-PARENT", "T-CHILD"], capacity=1)
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        assert detail["已交付"] == ["T-INDEPENDENT"]
        assert detail["待计算"] == ["T-PARENT"]
        assert detail["依赖阻塞"] == ["T-CHILD"]

    def test_resume_rejects_completed_round(self):
        seed_agg("T-ONLY", "METR-GMV", ["order_events"])
        plan_id = seed_plan(1, "SCHE-T-DONE", ["T-ONLY"], capacity=1)
        SERVICE.start_run(plan_id)
        detail, message = SERVICE.resume_run(plan_id)
        assert detail is None
        assert "无需恢复" in message

    def test_immediate_execute_starts_round(self):
        seed_agg("T-ONLY", "METR-GMV", ["order_events"])
        plan_id = seed_plan(1, "SCHE-T-ACTION", ["T-ONLY"], capacity=1)
        entry, _ = SERVICE.plan_service.run_action(plan_id, "立即执行")
        assert entry is not None
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        assert detail["已交付"] == ["T-ONLY"]
        assert detail["状态"] == "已完成"

    def test_start_while_running_reuses_current_round(self):
        seed_agg("T-A", "METR-GMV", ["order_events"])
        seed_agg("T-B", "METR-UV", ["page_views"])
        plan_id = seed_plan(1, "SCHE-T-ACTIVE", ["T-A", "T-B"], capacity=1)
        first, _ = SERVICE.start_run(plan_id)
        second, message = SERVICE.start_run(plan_id)
        assert first is not None and second is not None
        assert second["轮次"] == first["轮次"]
        assert "仍在执行中" in message


class TestRunLedger:
    def test_products_record_actual_results(self):
        seed_agg("T-GMV", "METR-GMV", ["order_events"])
        seed_agg("T-UV", "METR-UV", ["page_views"])
        plan_id = seed_plan(1, "SCHE-T-PRODUCTS", ["T-GMV", "T-UV"], capacity=2)
        detail, _ = SERVICE.start_run(plan_id)
        assert detail is not None
        results = {item["聚合任务"]: item["指标结果"] for item in detail["派生产物"]}
        assert results["T-GMV"] == 5339200.0
        assert results["T-UV"] == 137330

    def test_detail_lists_round_versions(self):
        seed_agg("T-GMV", "METR-GMV", ["order_events"])
        plan_id = seed_plan(1, "SCHE-T-VERSIONS", ["T-GMV"], capacity=1)
        SERVICE.start_run(plan_id)
        detail = SERVICE.get_detail(plan_id)
        assert detail is not None
        assert task_state(detail, "T-GMV")["产物版本"] == "T-GMV-v1"
        assert detail["派生产物"][0]["产物编号"] == "SCHE-T-VERSIONS-R1-T-GMV"

    def test_run_round_increments_after_completion(self):
        seed_agg("T-ONLY", "METR-GMV", ["order_events"])
        plan_id = seed_plan(1, "SCHE-T-ROUND", ["T-ONLY"], capacity=1)
        first, _ = SERVICE.start_run(plan_id)
        assert first is not None
        assert first["轮次"] == 1
        second, _ = SERVICE.start_run(plan_id)
        assert second is not None
        assert second["轮次"] == 2
        assert second["派生产物"][0]["产物版本"] == "T-ONLY-v2"


class TestSeededPlans:
    def test_weekly_plan_keeps_a_standalone_and_a_dependency_chain(self):
        detail, message = SERVICE.start_run(2)
        assert detail is not None
        assert detail["轮次"] == 1
        assert detail["已交付"] == ["AGG-UV-BASE", "AGG-GMV-WEEKLY"]
        assert detail["待计算"] == ["AGG-CVR-DERIVED"]
        assert detail["依赖阻塞"] == ["AGG-ARPU-DERIVED"]

    def test_weekly_plan_resumes_to_completion(self):
        SERVICE.start_run(2)
        detail, _ = SERVICE.resume_run(2)
        assert detail is not None
        assert detail["状态"] == "已完成"
        assert detail["待计算"] == []
        assert detail["依赖阻塞"] == []
        assert [item["聚合任务"] for item in detail["派生产物"]] == [
            "AGG-UV-BASE", "AGG-GMV-WEEKLY", "AGG-CVR-DERIVED", "AGG-ARPU-DERIVED",
        ]

    def test_previously_delivered_results_survive_resume(self):
        first, _ = SERVICE.start_run(2)
        assert first is not None
        kept = {item["聚合任务"]: item for item in first["派生产物"]}
        second, _ = SERVICE.resume_run(2)
        assert second is not None
        after = {item["聚合任务"]: item for item in second["派生产物"]}
        for task_code, product in kept.items():
            assert after[task_code]["产物版本"] == product["产物版本"]
            assert after[task_code]["产出时间"] == product["产出时间"]
