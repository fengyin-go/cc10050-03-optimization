"""页面解析模板领域服务：管理商品页HTML选择器与字段映射，负责把原始页面解析为结构化价格字段。

数据链路位置：解析层，消费样本库中的页面样本，按模板选择器提取价格字段，
产出的结构化数据交给规格归一器与解析审计。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.replay_flow import replay_flow
from app.store import store


class PageParserService(BaseModuleService):
    MODULE = "page_parser"
    ENTITY = "解析模板"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["模板名称", "适用站点", "选择器路径", "字段映射", "解析版本"]
    REQUIRED_FIELDS = ["模板名称", "适用站点", "选择器路径"]
    STATUS_ORDER = ["草稿", "已启用", "已废弃"]
    ACTION_RULES = {"启用模板": "已启用", "废弃模板": "已废弃"}
    NEGATIVE_ACTIONS = []

    SWITCH_ACTIONS = {
        "切换模板", "切换版本", "切换并启用", "切换到此版本",
        "应用版本切换", "废弃旧版并启用新版", "提交模板切换",
    }
    CLAIM_ACTIONS = {
        "领取回放样本", "从回放队列领取", "从回放队列领取样本",
        "领取队列样本", "领取样本", "领取回放",
    }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        entry["switch_receipts"] = [
            s for s in store.rows("template_switches")
            if s.get("new_template_id") == entry_id or s.get("old_template_id") == entry_id
        ]
        entry["queued_replays"] = [
            r for r in store.rows("replays")
            if r.get("site") == entry.get("适用站点") and r.get("status") in {"待领取", "失败待重试"}
        ]
        return entry

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        context = context or {}
        if action in self.SWITCH_ACTIONS:
            response = replay_flow.switch_template(entry_id, context)
            return response.get("entry"), response["message"], response
        if action in self.CLAIM_ACTIONS:
            response = replay_flow.claim_next(entry_id, context)
            entry = store.find(self.MODULE, entry_id)
            return entry, response["message"], response
        if action == "启用模板":
            response = self.enable_template(entry_id)
            return store.find(self.MODULE, entry_id), response["message"], response
        if action == "废弃模板":
            response = self.deprecate_template(entry_id)
            return store.find(self.MODULE, entry_id), response["message"], response
        outcome = super().run_action(entry_id, action, context)
        if len(outcome) == 3:
            return outcome  # type: ignore[return-value]
        entry, message = outcome
        return entry, message, {}

    # ---- 业务扩展：模板解析与字段提取 ----

    def enable_template(self, entry_id: int) -> dict[str, Any]:
        """启用解析模板，校验选择器与字段映射完整性。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        selector = entry.get("选择器路径", "")
        mapping = entry.get("字段映射", "")
        if not selector or "price" not in selector:
            return {"ok": False, "message": "选择器路径缺少价格节点，不能启用"}
        if not mapping:
            return {"ok": False, "message": "字段映射为空，不能启用"}
        entry["status"] = "已启用"
        entry["pending"] = False
        return {"ok": True, "message": f"模板 {entry.get('模板名称', '')} 已启用"}

    def deprecate_template(self, entry_id: int) -> dict[str, Any]:
        """废弃模板并检查是否有站点仍依赖该模板。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "模板不存在"}
        site = entry.get("适用站点", "")
        pending_samples = [
            s for s in store.rows("sample_store")
            if s.get("站点名称") == site and s.get("status") == "已保存"
        ]
        entry["status"] = "已废弃"
        entry["abnormal"] = bool(pending_samples)
        return {
            "ok": True,
            "message": "模板已废弃",
            "blocking_samples": len(pending_samples),
        }

    def parse_sample(
        self,
        entry_id: int,
        sample_id: int,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用指定模板领取/解析样本，并写出带版本的结构化价格字段与回执。"""
        context = dict(context or {})
        replay_id = context.get("replay_id")
        if not replay_id:
            started = replay_flow.start_replay(sample_id, context)
            replay = started.get("replay") or {}
            replay_id = replay.get("id")
            if not replay_id:
                return started
        context["template_id"] = entry_id
        return replay_flow.continue_replay(str(replay_id), context)

    def switch_version(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """切换到当前模板版本：同事务废弃同站旧版并启用新版，返回切换回执。"""
        return replay_flow.switch_template(entry_id, context or {})

    def switch_template(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """switch_version 的语义别名，便于按业务名直接调用。"""
        return self.switch_version(entry_id, context)

    def claim_next_sample(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """由当前模板从对应站点的回放队列领取下一笔样本。"""
        return replay_flow.claim_next(entry_id, context or {})

    def report_to_audit(self, entry_id: int, hit_rate: float) -> dict[str, Any]:
        """向解析质量审计上报字段命中率。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "模板不存在"}
        rows = store.rows("parse_audit")
        aid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rate_text = f"{hit_rate:.2%}"
        status = "正常" if hit_rate >= 0.9 else "命中率下降"
        rows.append({
            "id": aid,
            "审计时间": datetime.now(timezone.utc).isoformat(),
            "解析模板": entry.get("模板名称", ""),
            "样本总数": "1",
            "字段命中率": rate_text,
            "失败原因分布": "" if hit_rate >= 0.9 else "选择器失配",
            "status": status,
            "pending": True,
            "abnormal": hit_rate < 0.9,
        })
        return {"ok": True, "message": "命中率已上报审计", "audit_id": aid, "rate": rate_text}

    def route_failures(self, entry_id: int, reason: str) -> dict[str, Any]:
        """解析失败时把样本送入失败隔离区。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "模板不存在"}
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"parse-{entry_id}-{fid}",
            "失败阶段": "解析",
            "失败原因": reason,
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "0",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "失败样本已隔离", "quarantine_id": fid}

    def bump_version(self, entry_id: int) -> dict[str, Any]:
        """升级模板解析版本并触发基线重算。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "模板不存在"}
        current = str(entry.get("解析版本", "v1"))
        entry["解析版本"] = f"v{int(current.lstrip('v') or 1) + 1}"
        for base in store.rows("price_baseline"):
            if base.get("status") == "已生效":
                base["status"] = "待重算"
                base["pending"] = True
        return {
            "ok": True,
            "message": "解析版本已升级，基线已标记重算",
            "version": entry.get("解析版本"),
        }
