"""页面样本库领域服务：保存原始页面样本用于解析回归与回放，负责样本指纹去重与过期治理。

数据链路位置：样本留存层，接收爬取任务产出的原始页面，向页面解析模板
提供待解析样本，向失败隔离区提供重试回放素材。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.replay_flow import replay_flow
from app.store import store


class SampleStoreService(BaseModuleService):
    MODULE = "sample_store"
    ENTITY = "页面样本"
    OPERATION_TERM = "抓取"
    LIST_FIELDS = ["样本批次", "站点名称", "原始HTML摘要", "抓取时间", "样本指纹"]
    REQUIRED_FIELDS = ["样本批次", "站点名称", "原始HTML摘要"]
    STATUS_ORDER = ["已保存", "已解析", "已过期"]
    ACTION_RULES = {"标记解析": "已解析", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    REPLAY_START_ACTIONS = {"回放", "发起回放", "创建回放", "加入回放队列", "回放队列"}
    REPLAY_CONTINUE_ACTIONS = {
        "继续回放", "继续原回放", "接续回放", "接续原回放",
        "领取并解析", "领取并继续", "领取样本",
    }
    REPLAY_RETRY_ACTIONS = {"重试回放", "断线重试", "断线重试同一笔", "重试本笔回放"}
    REPLAY_CONFIRM_ACTIONS = {"确认回执", "确认回放", "确认分轮回执", "确认本轮结果"}
    REPLAY_QUERY_ACTIONS = {"查询回执", "查询回放回执", "刷新回执", "查询处理顺序"}

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size
        )
        for row in rows:
            replay = next((
                r for r in reversed(store.rows("replays"))
                if str(r.get("sample_id")) == str(row.get("id"))
                and r.get("status") in {"待领取", "解析中", "失败待重试", "待确认"}
            ), None)
            result = next((
                r for r in reversed(store.rows("parse_results"))
                if str(r.get("sample_id")) == str(row.get("id"))
            ), None)
            row["回放状态"] = replay.get("status") if replay else "无在办回放"
            row["当前阶段"] = replay.get("stage") if replay else "历史解析结果可查询"
            row["最新回放单号"] = replay.get("id") if replay else row.get("active_replay_id", "")
            row["最新解析价格"] = (result or {}).get("price")
            row["最新模板版本"] = (result or {}).get("template_version")
        return rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        state = replay_flow.sample_state(entry_id)
        if state.get("ok"):
            for key in (
                "parse_results", "latest_parse_result", "original_parse_result",
                "replays", "active_replay", "receipts", "switch_receipts",
                "template_switches", "ledger", "operation_order",
                "recovery_records", "eligible_templates",
            ):
                entry[key] = state.get(key)
            active = state.get("active_replay") or {}
            latest = state.get("latest_parse_result") or {}
            entry["回放状态"] = active.get("status", "无在办回放")
            entry["当前阶段"] = active.get("stage", "历史解析结果可查询")
            entry["最新回放单号"] = active.get("id", "")
            entry["最新解析价格"] = latest.get("price")
            entry["最新模板版本"] = latest.get("template_version")
        return entry

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档", {"ok": False}

        if action in self.REPLAY_START_ACTIONS:
            response = replay_flow.start_replay(entry_id, context)
            return response.get("entry"), response["message"], response

        replay_id = context.get("replay_id") or entry.get("active_replay_id")
        if not replay_id:
            state = replay_flow.sample_state(entry_id)
            active = state.get("active_replay") if state.get("ok") else None
            replay_id = (active or {}).get("id")

        if action in self.REPLAY_CONTINUE_ACTIONS:
            if not replay_id:
                started = replay_flow.start_replay(entry_id, context)
                replay_id = (started.get("replay") or {}).get("id")
                if not replay_id:
                    return started.get("entry"), started["message"], started
            response = replay_flow.continue_replay(str(replay_id), context)
            return response.get("entry"), response["message"], response

        if action in self.REPLAY_RETRY_ACTIONS:
            if not replay_id:
                return entry, "没有可重试的回放单；请先发起回放", {"ok": False}
            response = replay_flow.retry_replay(str(replay_id), context)
            return response.get("entry"), response["message"], response

        if action in self.REPLAY_CONFIRM_ACTIONS:
            if not replay_id:
                return entry, "没有待确认的分轮回执", {"ok": False}
            response = replay_flow.confirm_replay(str(replay_id), context)
            return response.get("entry"), response["message"], response

        if action in self.REPLAY_QUERY_ACTIONS:
            state = replay_flow.sample_state(entry_id)
            return self.get_entry(entry_id), "已按样本查询解析字段、回执与处理顺序", state

        result = super().run_action(entry_id, action, context)
        if len(result) == 3:
            return result  # type: ignore[return-value]
        rich_entry, message = result
        return rich_entry, message, {}

    # ---- 业务扩展：样本留存与指纹去重 ----

    def mark_parsed(self, entry_id: int) -> dict[str, Any]:
        """标记样本已解析并联动解析量指标。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        if entry.get("status") != "已保存":
            return {"ok": False, "message": "仅已保存样本可标记解析"}
        entry["status"] = "已解析"
        entry["pending"] = False
        site = entry.get("站点名称", "")
        row = next((m for m in store.rows("collect_metrics") if m.get("站点名称") == site), None)
        if row is not None:
            row["解析量"] = str(int(row.get("解析量", "0") or 0) + 1)
            row["统计时间"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True, "message": "样本已标记解析", "site": site}

    def mark_expired(self, entry_id: int) -> dict[str, Any]:
        """把样本标记过期，释放回归占位。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "样本不存在"}
        entry["status"] = "已过期"
        entry["pending"] = False
        return {"ok": True, "message": "样本已过期"}

    def dedup_by_fingerprint(self, entry_id: int) -> dict[str, Any]:
        """按样本指纹去重，重复样本直接过期。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "样本不存在"}
        fp = entry.get("样本指纹", "")
        duplicates = [
            s for s in store.rows(self.MODULE)
            if s.get("id") != entry.get("id") and s.get("样本指纹") == fp
        ]
        for dup in duplicates:
            dup["status"] = "已过期"
            dup["pending"] = False
        return {
            "ok": True,
            "message": "指纹去重完成",
            "duplicates_expired": len(duplicates),
            "fingerprint": fp,
        }

    def replay_to_parser(self, entry_id: int) -> dict[str, Any]:
        """创建独立回放单；保留样本已解析状态和全部历史解析结果。"""
        response = replay_flow.start_replay(entry_id, {"actor": "页面样本库"})
        state = replay_flow.sample_state(entry_id)
        eligible = state.get("eligible_templates") if isinstance(state, dict) else []
        if eligible:
            response["template"] = eligible[-1].get("template_name") or eligible[-1].get("version")
            response["eligible_template"] = eligible[-1]
        return response

    def continue_replay(
        self,
        entry_id: int,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """继续该样本当前在办的回放；没有在办单时先入队。"""
        context = dict(context or {})
        entry = store.find(self.MODULE, entry_id) or {}
        replay_id = context.get("replay_id") or entry.get("active_replay_id")
        if not replay_id:
            state = replay_flow.sample_state(entry_id)
            replay_id = (state.get("active_replay") or {}).get("id") if state.get("ok") else None
        if not replay_id:
            started = replay_flow.start_replay(entry_id, context)
            replay_id = (started.get("replay") or {}).get("id")
            if not replay_id:
                return started
        return replay_flow.continue_replay(str(replay_id), context)

    def retry_replay(
        self,
        entry_id: int,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用原回放单和幂等键重试，保证同一笔只登记一份有效结果。"""
        context = dict(context or {})
        entry = store.find(self.MODULE, entry_id) or {}
        replay_id = context.get("replay_id") or entry.get("active_replay_id")
        if not replay_id:
            state = replay_flow.sample_state(entry_id)
            replay_id = (state.get("active_replay") or {}).get("id") if state.get("ok") else None
        if not replay_id:
            return {"ok": False, "message": "没有可重试的回放单，请先发起回放"}
        return replay_flow.retry_replay(str(replay_id), context)

    def receipt_state(self, entry_id: int) -> dict[str, Any]:
        """按样本查询结构化解析字段、分轮回执、切换回执和处理顺序。"""
        return replay_flow.sample_state(entry_id)

    def query_receipts(self, entry_id: int) -> dict[str, Any]:
        """receipt_state 的业务命名别名。"""
        return self.receipt_state(entry_id)

    def quarantine_oldest(self, batch: str, max_keep: int = 50) -> dict[str, Any]:
        """批次超过容量时把最旧样本送入失败隔离区归档。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("样本批次") == batch]
        if len(rows) <= max_keep:
            return {"ok": True, "message": "批次容量未超限", "count": len(rows)}
        overflow = sorted(rows, key=lambda r: str(r.get("抓取时间", "")))[:len(rows) - max_keep]
        qrows = store.rows("failure_quarantine")
        base_id = max((int(r.get("id", 0)) for r in qrows), default=0)
        for idx, sample in enumerate(overflow, start=1):
            qrows.append({
                "id": base_id + idx,
                "样本指纹": sample.get("样本指纹", ""),
                "失败阶段": "留存溢出",
                "失败原因": f"批次 {batch} 超出容量",
                "隔离时间": datetime.now(timezone.utc).isoformat(),
                "重试次数": "0",
                "status": "已丢弃",
                "pending": False,
                "abnormal": False,
            })
        return {"ok": True, "message": "溢出样本已隔离", "quarantined": len(overflow)}

    def site_sample_stats(self, site: str) -> dict[str, Any]:
        """统计站点样本量并更新采集量指标产出量。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("站点名称") == site]
        parsed = sum(1 for r in rows if r.get("status") == "已解析")
        metrics = next((m for m in store.rows("collect_metrics") if m.get("站点名称") == site), None)
        if metrics is not None:
            metrics["产出量"] = str(parsed)
        return {
            "ok": True,
            "message": "站点样本统计完成",
            "total": len(rows),
            "parsed": parsed,
        }
