"""页面样本回放、模板切换与失败恢复的应用服务。

这里集中维护三类一致性：
1. 样本原始解析结果按“样本 + 模板版本 + 轮次”留存，回放状态不再回写抹掉解析历史；
2. 模板领取时再次校验站点资格和启用状态，不能用其他站点版本代替；
3. 同一回放单/幂等键的断线重试只产生一份有效解析结果，所有关键动作进入顺序台账。
"""
from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

OPEN_REPLAY_STATUSES = {"待领取", "解析中", "失败待重试", "待确认"}
CLAIMABLE_STATUSES = {"待领取", "失败待重试", "解析中"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplayFlowService:
    def __init__(self) -> None:
        # FastAPI 的同步路由会在线程池中执行；用可重入锁保证并发领取/切换的顺序。
        self.lock = threading.RLock()

    # ---------- 基础工具 ----------

    def _rows(self, table: str) -> list[dict[str, Any]]:
        return store.rows(table)

    def _sample(self, sample_id: int) -> dict[str, Any] | None:
        return store.find("sample_store", int(sample_id))

    def _template(self, template_id: int | str | None) -> dict[str, Any] | None:
        if template_id in (None, ""):
            return None
        try:
            return store.find("page_parser", int(template_id))
        except (TypeError, ValueError):
            return next(
                (t for t in self._rows("page_parser") if str(t.get("解析版本")) == str(template_id)),
                None,
            )

    def _replay(self, replay_id: str) -> dict[str, Any] | None:
        return next((r for r in self._rows("replays") if r.get("id") == replay_id), None)

    def _next_sequence(self) -> int:
        rows = self._rows("operation_ledger")
        return max((int(r.get("sequence", 0) or 0) for r in rows), default=0) + 1

    def _ledger(
        self,
        operation: str,
        summary: str,
        *,
        actor: str = "值班员",
        sample_id: int | None = None,
        replay_id: str | None = None,
        switch_id: str | None = None,
        template_id: int | None = None,
        template_version: str | None = None,
    ) -> dict[str, Any]:
        seq = self._next_sequence()
        entry = {
            "sequence": seq,
            "at": utc_now(),
            "operation": operation,
            "actor": actor,
            "sample_id": sample_id,
            "replay_id": replay_id,
            "switch_id": switch_id,
            "template_id": template_id,
            "template_version": template_version,
            "summary": summary,
        }
        self._rows("operation_ledger").append(entry)
        return entry

    def _idempotent_response(self, key: str | None, scope: str | None = None) -> dict[str, Any] | None:
        if not key:
            return None
        row = next((
            x for x in self._rows("idempotency_keys")
            if x.get("key") == key and (scope is None or x.get("scope") == scope)
        ), None)
        if row is None:
            return None
        response = copy.deepcopy(row.get("response") or {})
        response["duplicate"] = True
        response["idempotent_replay"] = True
        return response

    def _save_idempotent_response(self, key: str | None, scope: str, response: dict[str, Any]) -> None:
        if not key:
            return
        rows = self._rows("idempotency_keys")
        existing = next((x for x in rows if x.get("key") == key), None)
        if existing is None:
            rows.append({
                "key": key,
                "scope": scope,
                "created_at": utc_now(),
                "response": copy.deepcopy(response),
            })

    def _idem_key(self, payload: dict[str, Any], header_key: str | None = None) -> str | None:
        return (
            header_key
            or payload.get("idempotency_key")
            or payload.get("Idempotency-Key")
            or payload.get("request_id")
            or payload.get("requestId")
        )

    def _fail(
        self,
        message: str,
        *,
        error_code: str = "REPLAY_REJECTED",
        status_code: int = 200,
        **extra: Any,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": False,
            "status_code": status_code,
            "error_code": error_code,
            "message": message,
            "recovery_steps": extra.pop("recovery_steps", [
                "保留原回放单号，不要新建回放",
                "按样本查询服务端受理状态和已有回执",
                "排除站点/模板资格问题后，使用同一幂等键重试",
            ]),
        }
        data.update(extra)
        return data

    # ---------- 查询模型 ----------

    def sample_state(self, sample_id: int) -> dict[str, Any]:
        sample = self._sample(sample_id)
        if sample is None:
            return self._fail(f"页面样本 {sample_id} 不存在", error_code="SAMPLE_NOT_FOUND", status_code=404)

        results = [
            r for r in self._rows("parse_results")
            if str(r.get("sample_id")) == str(sample_id)
        ]
        results.sort(key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))

        replays = [
            r for r in self._rows("replays")
            if str(r.get("sample_id")) == str(sample_id)
        ]
        replays.sort(key=lambda r: int(r.get("round", 0) or 0))

        site = sample.get("站点名称", "")
        switches = [
            s for s in self._rows("template_switches")
            if s.get("site") == site or str(s.get("sample_id") or "") == str(sample_id)
        ]
        switches.sort(key=lambda s: int(s.get("sequence", 0) or 0))

        ledger = [
            e for e in self._rows("operation_ledger")
            if str(e.get("sample_id") or "") == str(sample_id)
            or (e.get("switch_id") and e.get("switch_id") in {s.get("id") for s in switches})
        ]
        ledger.sort(key=lambda e: int(e.get("sequence", 0)))

        active = next((r for r in reversed(replays) if r.get("status") in OPEN_REPLAY_STATUSES), None)
        recovery_records = self._recovery_records(sample_id, replays)
        eligible = [
            {
                "id": t.get("id"),
                "template_name": t.get("模板名称"),
                "version": t.get("解析版本"),
                "status": t.get("status"),
                "site": t.get("适用站点"),
            }
            for t in self._rows("page_parser")
            if t.get("适用站点") == site and t.get("status") == "已启用"
        ]
        receipts: list[dict[str, Any]] = []
        for replay in replays:
            receipts.extend(replay.get("receipts") or [])
        receipts.sort(key=lambda r: int(str(r.get("receipt_no", "RC-0")).split("-")[-1] or 0))

        return {
            "ok": True,
            "message": "样本回放状态已查询",
            "sample": sample,
            "parse_results": results,
            "latest_parse_result": results[-1] if results else None,
            "original_parse_result": results[0] if results else None,
            "replays": replays,
            "active_replay": active,
            "receipts": receipts,
            "switch_receipts": switches,
            "template_switches": switches,
            "ledger": ledger,
            "operation_order": ledger,
            "recovery_records": recovery_records,
            "eligible_templates": eligible,
        }

    def _recovery_records(self, sample_id: int, replays: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for replay in replays:
            recovery = replay.get("recovery")
            if recovery:
                records.append({
                    "sample_id": sample_id,
                    "replay_id": replay.get("id"),
                    **recovery,
                })
            for attempt in replay.get("attempts") or []:
                if attempt.get("ok") is False and not recovery:
                    records.append({
                        "sample_id": sample_id,
                        "replay_id": replay.get("id"),
                        "status": "失败待重试",
                        "failure_reason": attempt.get("reason"),
                        "steps": attempt.get("recovery_steps") or [],
                        "at": attempt.get("at"),
                    })
        return records

    def get_replay(self, replay_id: str) -> dict[str, Any]:
        replay = self._replay(replay_id)
        if replay is None:
            return self._fail(f"回放单 {replay_id} 不存在", error_code="REPLAY_NOT_FOUND", status_code=404)
        sample_id = int(replay["sample_id"])
        state = self.sample_state(sample_id)
        return {
            "ok": True,
            "message": "回放回执已查询",
            "replay": replay,
            "receipts": replay.get("receipts") or [],
            "parse_result": next(
                (r for r in self._rows("parse_results") if r.get("result_no") == replay.get("parse_result_id")),
                None,
            ),
            "sample_state": state,
        }

    # ---------- 回放入队 ----------

    def start_replay(
        self,
        sample_id: int,
        payload: dict[str, Any] | None = None,
        *,
        header_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        key = self._idem_key(payload, header_idempotency_key)
        cached = self._idempotent_response(key, "start_replay")
        if cached is not None:
            return cached

        with self.lock:
            sample = self._sample(sample_id)
            if sample is None:
                return self._fail(f"页面样本 {sample_id} 不存在", error_code="SAMPLE_NOT_FOUND", status_code=404)
            if sample.get("status") == "已过期":
                return self._fail(
                    "样本已过期，不能接续回放；请先恢复样本或重新抓取",
                    error_code="SAMPLE_EXPIRED",
                    sample=self.sample_state(sample_id),
                )

            existing = [r for r in self._rows("replays") if str(r.get("sample_id")) == str(sample_id)]
            active = next((r for r in reversed(existing) if r.get("status") in OPEN_REPLAY_STATUSES), None)
            # 待领取/处理中/失败的单据必须继续原单；待确认单据可查询，也允许显式开启下一轮。
            if active and active.get("status") in {"待领取", "解析中", "失败待重试"} and not payload.get("new_round"):
                response = {
                    "ok": True,
                    "already_open": True,
                    "message": f"已有一笔{active.get('status')}的回放 {active.get('id')}，请继续原单",
                    "replay": active,
                    "entry": self.get_sample_entry(sample_id),
                }
                if key:
                    self._save_idempotent_response(key, "start_replay", response)
                return response

            round_no = max((int(r.get("round", 0) or 0) for r in existing), default=0) + 1
            replay_id = str(payload.get("replay_id") or f"RP-S{sample_id}-R{round_no}")
            if self._replay(replay_id):
                replay_id = f"RP-S{sample_id}-R{round_no}-{self._next_sequence()}"

            idem = key or f"idem-{replay_id}"
            replay = {
                "id": replay_id,
                "replay_no": replay_id,
                "sample_id": sample_id,
                "round": round_no,
                "idempotency_key": idem,
                "status": "待领取",
                "stage": "服务端已受理：回放已入队，等待对应站点的启用模板领取",
                "site": sample.get("站点名称", ""),
                "requested_by": payload.get("actor") or payload.get("requested_by") or "值班员",
                "template_id": None,
                "template_name": None,
                "template_version": None,
                "claimed_template_id": None,
                "parse_result_id": None,
                "structured_fields": None,
                "attempts": [{
                    "attempt": 1,
                    "at": utc_now(),
                    "state": "已入队",
                    "ok": True,
                    "reason": "回放单已创建，尚未被模板领取",
                }],
                "recovery": None,
                "receipts": [],
                "created_at": utc_now(),
                "claimed_at": None,
                "completed_at": None,
                "confirmed_at": None,
            }
            self._rows("replays").append(replay)
            sample["active_replay_id"] = replay_id
            # 关键修复：回放不改变“已解析”事实，只记录独立的回放流转状态。
            if sample.get("status") not in {"已解析", "已过期"}:
                sample["status"] = "已解析"
            sample["pending"] = False

            ledger = self._ledger(
                "replay.enqueued",
                f"样本 {sample_id} 的第 {round_no} 轮回放已入队：{replay_id}，等待模板领取",
                actor=replay["requested_by"],
                sample_id=sample_id,
                replay_id=replay_id,
            )
            replay["sequence"] = ledger["sequence"]
            receipt = self._make_receipt(replay, "回放入队回执", "待领取",
                                         "服务端已受理，当前等待模板领取",
                                         ["继续回放/领取样本", "查询回执"])
            replay["receipts"].append(receipt)

            response = {
                "ok": True,
                "message": "回放已入队，样本原解析结果仍可按版本查询",
                "replay": replay,
                "receipt": receipt,
                "entry": self.get_sample_entry(sample_id),
            }
            self._save_idempotent_response(key, "start_replay", response)
            return response

    def get_sample_entry(self, sample_id: int) -> dict[str, Any] | None:
        sample = self._sample(sample_id)
        return copy.deepcopy(sample) if sample else None

    # ---------- 模板资格与字段提取 ----------

    def _eligible_template(
        self, site: str, template_id: int | str | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if template_id not in (None, ""):
            template = self._template(template_id)
            if template is None:
                return None, self._fail(
                    f"指定的模板 {template_id} 不存在",
                    error_code="TEMPLATE_NOT_FOUND",
                )
            template_site = template.get("适用站点", "")
            if template_site != site:
                return None, self._fail(
                    f"模板站点「{template_site}」与样本站点「{site}」不一致，不能用其他站点版本代替",
                    error_code="TEMPLATE_SITE_MISMATCH",
                    template_id=template.get("id"),
                    recovery_steps=[
                        f"改用适用站点为「{site}」且状态为已启用的模板",
                        "如新版本尚未启用，先提交同站模板切换",
                        "不要使用其他站点的版本号或选择器",
                    ],
                )
            if template.get("status") != "已启用":
                return None, self._fail(
                    f"模板 {template.get('解析版本')} 当前为「{template.get('status')}」，无资格领取回放",
                    error_code="TEMPLATE_NOT_ELIGIBLE",
                    template_id=template.get("id"),
                    recovery_steps=[
                        "启用该同站模板，或提交旧版废弃/新版启用的切换",
                        "切换完成后用原回放单号重试",
                    ],
                )
            return template, None

        candidates = [
            t for t in self._rows("page_parser")
            if t.get("适用站点") == site and t.get("status") == "已启用"
        ]
        if not candidates:
            return None, self._fail(
                f"站点「{site}」没有已启用的匹配模板",
                error_code="NO_ELIGIBLE_TEMPLATE",
                recovery_steps=[
                    "在页面解析模板页启用该站点的有效版本",
                    "如需换版，提交“废弃旧版并启用新版”的切换",
                    "然后用原回放单号重试，不要新建回放",
                ],
            )
        candidates.sort(key=lambda t: (int(t.get("switch_sequence", 0) or 0), int(t.get("id", 0))))
        return candidates[-1], None

    def _parse_mapping(self, template: dict[str, Any]) -> dict[str, str]:
        rules = template.get("解析规则")
        if isinstance(rules, dict) and rules:
            return {str(k): str(v) for k, v in rules.items()}
        mapping = template.get("字段映射") or ""
        if isinstance(mapping, dict):
            return {str(k): str(v) for k, v in mapping.items()}
        text = str(mapping).strip()
        rules_out: dict[str, str] = {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    rules_out = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                rules_out = {}
        if not rules_out:
            for part in re.split(r"[,;\n，；]", text):
                if not part.strip():
                    continue
                bits = re.split(r"[:=：]", part, maxsplit=1)
                if len(bits) == 2:
                    rules_out[bits[0].strip()] = bits[1].strip()
        selector = str(template.get("选择器路径") or "").strip()
        if selector and "price" not in rules_out and "价格" not in rules_out:
            rules_out.setdefault("price", selector)
            rules_out.setdefault("价格", selector)
        return rules_out

    def _select_text(self, html: str, selector: str) -> str | None:
        selector = selector.strip()
        if not selector:
            return None
        patterns: list[str] = []
        for token in selector.split():
            token = token.strip()
            if token.startswith("."):
                cls = re.escape(token[1:])
                patterns.append(rf"class=[\"'][^\"']*\b{cls}\b[^\"']*[\"']")
            elif token.startswith("#"):
                ident = re.escape(token[1:])
                patterns.append(rf"id=[\"']{ident}[\"']")
            else:
                patterns.append(re.escape(token))
        attr_pattern = patterns[-1] if patterns else None
        if attr_pattern:
            m = re.search(rf"<(?P<tag>[a-zA-Z0-9]+)[^>]*?{attr_pattern}[^>]*>(?P<body>.*?)</(?P=tag)>",
                          html, flags=re.S | re.I)
            if m:
                text = re.sub(r"<[^>]+>", " ", m.group("body"))
                return re.sub(r"\s+", " ", text).strip()
        # 兜底：直接在文本中寻找选择器里给出的价格数字标记。
        return None

    def _number(self, text: str | None) -> int | float | None:
        if text is None:
            return None
        m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(text))
        if not m:
            return None
        value = float(m.group(0).replace(",", ""))
        return int(value) if abs(value - round(value)) < 1e-9 else value

    def _extract_fields(self, template: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
        html = str(sample.get("原始HTML") or sample.get("原始HTML摘要") or "")
        rules = self._parse_mapping(template)
        fields: dict[str, Any] = {"currency": "CNY"}
        for field, selector in rules.items():
            fields[field] = self._number(self._select_text(html, selector))

        # 统一价格别名，方便不同前端/测试口径查询。
        price = fields.get("price")
        if price is None:
            price = fields.get("价格")
        if price is None:
            for key, value in fields.items():
                if "price" in key.casefold() or "价" in key:
                    if isinstance(value, (int, float)):
                        price = value
                        break
        if price is None:
            raise ValueError("选择器未提取到有效价格字段")

        fields["price"] = price
        fields["价格"] = price
        fields["current_price"] = price
        fields["当前价格"] = price
        if "original_price" in rules or "原价" in rules:
            original = fields.get("original_price", fields.get("原价"))
            fields["original_price"] = original
            fields["原价"] = original
            fields["old_price"] = original
            fields["旧价格"] = original
        if "promo_price" in rules or "促销价" in rules:
            promo = fields.get("promo_price", fields.get("促销价"))
            fields["promo_price"] = promo
            fields["促销价"] = promo
            if promo is not None:
                fields["promotion_price"] = promo
                fields["促销价格"] = promo
                fields["new_price"] = promo
                fields["新价格"] = promo
        return fields

    # ---------- 领取并继续/重试 ----------

    def continue_replay(
        self,
        replay_id: str,
        payload: dict[str, Any] | None = None,
        *,
        header_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        key = self._idem_key(payload, header_idempotency_key)

        # 先查幂等表：即使并发请求同时进入，第一个完成后其他同键请求也只拿回执。
        cached = self._idempotent_response(key, "continue_replay")
        if cached is not None:
            with self.lock:
                replay = self._replay(replay_id)
                if replay and replay.get("delivery_failed") and not replay.get("recovery_recorded"):
                    self._record_recovery(replay, "断线重试命中同一幂等键，服务端未重复登记结果")
                    cached["recovered"] = True
                    cached["message"] = "断线重试已恢复；同一笔回放仍只有一份有效解析结果"
                cached["replay"] = replay
                cached["entry"] = self.get_sample_entry(int(replay["sample_id"])) if replay else None
            return cached

        with self.lock:
            replay = self._replay(replay_id)
            if replay is None:
                return self._fail(f"回放单 {replay_id} 不存在", error_code="REPLAY_NOT_FOUND", status_code=404)
            sample_id = int(replay["sample_id"])
            sample = self._sample(sample_id)
            if sample is None:
                return self._fail(f"页面样本 {sample_id} 不存在", error_code="SAMPLE_NOT_FOUND", status_code=404)

            # 同一笔已有有效结果：任何重试都返回原结果，不再新增 parse_result。
            if replay.get("parse_result_id"):
                result = next(
                    (r for r in self._rows("parse_results") if r.get("result_no") == replay["parse_result_id"]),
                    None,
                )
                if key and not self._idempotent_response(key, "continue_replay"):
                    response = self._success_retry_response(replay, result, recovered=True)
                    self._record_recovery(replay, "断线/重复提交后使用原回放单重试，未新增解析结果")
                    self._save_idempotent_response(key, "continue_replay", response)
                    return response
                return self._success_retry_response(replay, result, recovered=False)

            if replay.get("status") == "已确认":
                return self._success_retry_response(replay, None, recovered=False)

            explicit_template_id = payload.get("template_id") or payload.get("templateId")
            template_id = explicit_template_id or replay.get("claimed_template_id")
            template, error = self._eligible_template(str(sample.get("站点名称", "")), template_id)
            if error is not None and not explicit_template_id and template_id:
                # 恢复场景：上次领取的版本可能已被切换废弃；未显式指定模板时，
                # 回退到该站点当前有资格的版本，而不是把值班员卡在旧选择器上。
                fallback_template, fallback_error = self._eligible_template(
                    str(sample.get("站点名称", "")), None
                )
                if fallback_template is not None:
                    self._append_attempt(
                        replay,
                        "模板改派",
                        True,
                        f"原领取版本 {template_id} 已无资格，重试改用当前同站启用版本 "
                        f"{fallback_template.get('解析版本')}",
                    )
                    template = fallback_template
                    error = None
                else:
                    error = fallback_error or error
            if error is not None:
                replay["status"] = "失败待重试"
                replay["stage"] = "模板资格校验失败，可按恢复步骤换版后重试"
                reason = error["message"]
                self._append_attempt(replay, "资格校验失败", False, reason, error.get("recovery_steps"))
                replay["recovery"] = {
                    "status": "失败待重试",
                    "failure_reason": reason,
                    "error_code": error.get("error_code"),
                    "steps": error.get("recovery_steps") or [],
                    "at": utc_now(),
                }
                self._ledger("replay.failed", f"{replay_id} 领取失败：{reason}",
                             sample_id=sample_id, replay_id=replay_id)
                quarantine = self._quarantine_failure(sample, replay, reason, "模板资格校验")
                if replay.get("recovery"):
                    replay["recovery"]["quarantine_id"] = quarantine["id"]
                receipt = self._make_receipt(replay, "失败回执", "失败待重试", reason,
                                            ["切换同站合格模板", "使用原单号重试"])
                replay["receipts"].append(receipt)
                error.update({
                    "replay": replay,
                    "receipt": receipt,
                    "entry": self.get_sample_entry(sample_id),
                    "sample_state": self.sample_state(sample_id),
                })
                return error

            assert template is not None
            # 领取动作和模板版本在锁内落定，后续查询以这里实际写入的版本为准。
            replay["status"] = "解析中"
            replay["stage"] = f"已被模板 {template.get('解析版本')} 领取，正在写出结构化价格字段"
            replay["claimed_template_id"] = template.get("id")
            replay["template_id"] = template.get("id")
            replay["template_name"] = template.get("模板名称")
            replay["template_version"] = template.get("解析版本")
            replay["claimed_at"] = utc_now()
            self._append_attempt(replay, "已领取", True,
                                 f"模板 {template.get('模板名称')} {template.get('解析版本')} 已从回放队列领取样本")
            self._ledger(
                "replay.claimed",
                f"模板 {template.get('id')}（{template.get('解析版本')}）领取回放 {replay_id}",
                sample_id=sample_id,
                replay_id=replay_id,
                template_id=int(template["id"]),
                template_version=str(template.get("解析版本")),
            )

            try:
                fields = self._extract_fields(template, sample)
            except Exception as exc:  # 选择器失配等业务失败需要可见、可恢复。
                reason = f"模板 {template.get('解析版本')} 解析失败：{exc}"
                steps = [
                    "核对该版本选择器路径与样本 HTML 是否匹配",
                    "修复模板后启用该版本，或切换到同站合格版本",
                    f"使用原回放单号 {replay_id} 重试，不要新建回放",
                ]
                replay["status"] = "失败待重试"
                replay["stage"] = "解析失败，已记录原因与恢复步骤"
                replay["recovery"] = {
                    "status": "失败待重试",
                    "failure_reason": reason,
                    "error_code": "PARSE_FIELD_MISSING",
                    "steps": steps,
                    "at": utc_now(),
                }
                self._append_attempt(replay, "解析失败", False, reason, steps)
                quarantine = self._quarantine_failure(sample, replay, reason)
                replay["recovery"]["quarantine_id"] = quarantine["id"]
                self._ledger("replay.failed", reason, sample_id=sample_id, replay_id=replay_id,
                             template_id=int(template["id"]),
                             template_version=str(template.get("解析版本")))
                receipt = self._make_receipt(replay, "解析失败回执", "失败待重试", reason,
                                             ["修复选择器/切换模板", "原单号重试"])
                replay["receipts"].append(receipt)
                return self._fail(reason, error_code="PARSE_FIELD_MISSING", replay=replay,
                                  receipt=receipt, recovery_steps=steps,
                                  entry=self.get_sample_entry(sample_id))

            seq = self._next_sequence()
            result_no = f"PR-{seq:04d}"
            result = {
                "id": seq,
                "result_no": result_no,
                "sample_id": sample_id,
                "replay_id": replay_id,
                "round": replay.get("round"),
                "source": "模板回放",
                "template_id": template.get("id"),
                "template_name": template.get("模板名称"),
                "template_version": template.get("解析版本"),
                "version": template.get("解析版本"),
                "site": sample.get("站点名称"),
                "fields": fields,
                "price": fields.get("price"),
                "status": "待确认",
                "confirmed": False,
                "created_at": utc_now(),
                "sequence": seq,
            }
            self._rows("parse_results").append(result)

            replay["status"] = "待确认"
            replay["stage"] = f"{template.get('解析版本')} 已写出价格 {fields.get('price')}，分轮回执等待值班员确认"
            replay["parse_result_id"] = result_no
            replay["structured_fields"] = fields
            replay["completed_at"] = utc_now()
            replay["delivery_failed"] = False
            self._append_attempt(replay, "解析完成", True,
                                 f"已按 {template.get('解析版本')} 写出结构化价格 {fields.get('price')}")

            sample["status"] = "已解析"
            sample["pending"] = False
            sample["latest_parse_result_id"] = result_no
            sample["active_replay_id"] = replay_id

            self._ledger(
                "replay.parsed",
                f"{replay_id} 由模板 {template.get('解析版本')} 解析完成：价格 {fields.get('price')}，结果 {result_no} 待确认",
                sample_id=sample_id,
                replay_id=replay_id,
                template_id=int(template["id"]),
                template_version=str(template.get("解析版本")),
            )
            receipt = self._make_receipt(
                replay,
                "分轮回执",
                "待确认",
                f"模板 {template.get('解析版本')} 已受理并完成解析，价格 {fields.get('price')}，请确认本轮结果",
                ["确认分轮回执", "断线时用原单号重试", "继续下一轮回放"],
                extra={"parse_result": result, "fields": fields, "price": fields.get("price")},
            )
            replay["receipts"].append(receipt)

            response = {
                "ok": True,
                "message": "回放已由模板领取并完成解析，分轮回执待确认",
                "replay": replay,
                "receipt": receipt,
                "parse_result": result,
                "fields": fields,
                "version": template.get("解析版本"),
                "price": fields.get("price"),
                "entry": self.get_sample_entry(sample_id),
            }

            # 故障注入：模拟服务端已落库但响应在断线中丢失。
            fault = str(payload.get("fault") or payload.get("simulate") or "").lower()
            if fault in {"disconnect", "network", "response_lost", "断线"}:
                replay["delivery_failed"] = True
                reason = "模拟断线：服务端已受理并写出结果，但分轮回执响应未送达值班员"
                replay["stage"] = "回执响应丢失/断线未知；请用同一回放单和幂等键重试"
                replay["recovery"] = {
                    "status": "失败待重试",
                    "failure_reason": reason,
                    "error_code": "RESPONSE_LOST",
                    "steps": [
                        f"保留原回放单号 {replay_id} 与幂等键 {replay.get('idempotency_key')}",
                        "先按样本查询，可看到服务端实际已登记的结果",
                        "点击断线重试；幂等命中后不会产生第二份解析结果",
                    ],
                    "at": utc_now(),
                }
                self._append_attempt(replay, "响应丢失", False, reason, replay["recovery"]["steps"])
                lost_receipt = self._make_receipt(replay, "断线未知回执", "失败待重试", reason,
                                                  ["按样本查询", "同一幂等键重试"])
                replay["receipts"].append(lost_receipt)
                quarantine = self._quarantine_failure(sample, replay, reason, "回执投递/断线")
                replay["recovery"]["quarantine_id"] = quarantine["id"]
                self._ledger("replay.delivery_lost", reason, sample_id=sample_id, replay_id=replay_id)
                # 即便客户端拿不到成功响应，服务端幂等表也保存成功结果；重试只返回它。
                self._save_idempotent_response(key or replay.get("idempotency_key"), "continue_replay", response)
                fail_response = self._fail(
                    reason,
                    error_code="RESPONSE_LOST",
                    committed=True,
                    replay=replay,
                    receipt=lost_receipt,
                    parse_result=result,
                    recovery_steps=replay["recovery"]["steps"],
                    entry=self.get_sample_entry(sample_id),
                )
                return fail_response

            prior_failure = replay.get("recovery") and replay["recovery"].get("status") == "失败待重试"
            if prior_failure:
                self._record_recovery(replay)
                response["recovered"] = True
                response["message"] = "按恢复步骤重试成功；同一笔回放只登记一份有效解析结果"

            self._save_idempotent_response(key or replay.get("idempotency_key"), "continue_replay", response)
            return response

    def _success_retry_response(
        self, replay: dict[str, Any], result: dict[str, Any] | None, *, recovered: bool
    ) -> dict[str, Any]:
        if result is None:
            result = next(
                (r for r in self._rows("parse_results") if r.get("result_no") == replay.get("parse_result_id")),
                None,
            )
        return {
            "ok": True,
            "duplicate": True,
            "idempotent_replay": True,
            "recovered": recovered,
            "message": "同一笔回放已登记有效结果，重试未新增解析结果"
                       + ("，失败已恢复" if recovered else ""),
            "replay": replay,
            "receipt": (replay.get("receipts") or [])[-1] if replay.get("receipts") else None,
            "parse_result": result,
            "fields": (result or {}).get("fields") or replay.get("structured_fields"),
            "version": (result or {}).get("template_version") or replay.get("template_version"),
            "price": (result or {}).get("price"),
            "entry": self.get_sample_entry(int(replay["sample_id"])),
        }

    def _record_recovery(self, replay: dict[str, Any], reason: str | None = None) -> None:
        if replay.get("recovery_recorded"):
            return
        replay["recovery_recorded"] = True
        recovery_reason = reason or "使用原回放单号和恢复步骤重试成功，未新增有效解析结果"
        if replay.get("recovery"):
            replay["recovery"]["status"] = "已恢复"
            replay["recovery"]["recovered_at"] = utc_now()
            replay["recovery"]["recovery_result"] = recovery_reason
        self._append_attempt(replay, "失败恢复成功", True, recovery_reason)
        self._ledger(
            "replay.recovered",
            f"{replay.get('id')} {recovery_reason}",
            sample_id=int(replay["sample_id"]),
            replay_id=replay.get("id"),
        )
        quarantine = next(
            (q for q in reversed(self._rows("failure_quarantine"))
             if q.get("replay_id") == replay.get("id") and q.get("status") != "已恢复"),
            None,
        )
        if quarantine:
            quarantine["status"] = "已恢复"
            quarantine["pending"] = False
            quarantine["abnormal"] = False
            quarantine["恢复时间"] = utc_now()

    def retry_replay(
        self,
        replay_id: str,
        payload: dict[str, Any] | None = None,
        *,
        header_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        replay = self._replay(replay_id)
        if replay is not None:
            payload.setdefault("idempotency_key", replay.get("idempotency_key"))
        return self.continue_replay(replay_id, payload, header_idempotency_key=header_idempotency_key)

    def confirm_replay(self, replay_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        with self.lock:
            replay = self._replay(replay_id)
            if replay is None:
                return self._fail(f"回放单 {replay_id} 不存在", error_code="REPLAY_NOT_FOUND", status_code=404)
            if replay.get("status") == "已确认":
                return {"ok": True, "duplicate": True, "message": "该轮回执已确认", "replay": replay,
                        "entry": self.get_sample_entry(int(replay["sample_id"]))}
            if replay.get("status") != "待确认":
                return self._fail(
                    f"回放当前为「{replay.get('status')}」，还不能确认回执",
                    error_code="REPLAY_NOT_CONFIRMABLE",
                    replay=replay,
                )
            replay["status"] = "已确认"
            replay["stage"] = "本轮解析结果与回执已确认；可发起下一轮回放"
            replay["confirmed_at"] = utc_now()
            result = next((r for r in self._rows("parse_results")
                           if r.get("result_no") == replay.get("parse_result_id")), None)
            if result:
                result["status"] = "已确认"
                result["confirmed"] = True
            self._ledger("replay.confirmed", f"{replay_id} 分轮回执已确认",
                         sample_id=int(replay["sample_id"]), replay_id=replay_id)
            receipt = self._make_receipt(replay, "确认回执", "已确认", "本轮结果已由值班员确认",
                                         ["发起下一轮回放"])
            replay["receipts"].append(receipt)
            return {"ok": True, "message": "本轮回放结果已确认", "replay": replay, "receipt": receipt,
                    "parse_result": result, "entry": self.get_sample_entry(int(replay["sample_id"]))}

    def claim_next(
        self, template_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        template = self._template(template_id)
        if template is None:
            return self._fail(f"模板 {template_id} 不存在", error_code="TEMPLATE_NOT_FOUND", status_code=404)
        site = str(template.get("适用站点", ""))
        with self.lock:
            queued = [
                r for r in self._rows("replays")
                if r.get("site") == site and r.get("status") in {"待领取", "失败待重试"}
            ]
            queued.sort(key=lambda r: int(r.get("sequence", 0) or 0))
            if not queued:
                return {"ok": True, "message": f"站点「{site}」的回放队列暂无可领取样本", "claimed": None}
            target = queued[0]
            payload["template_id"] = template_id
            return self.continue_replay(str(target["id"]), payload)

    # ---------- 模板切换 ----------

    def switch_template(
        self,
        new_template_id: int | str | None = None,
        payload: dict[str, Any] | None = None,
        *,
        header_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        new_template_id = new_template_id or payload.get("new_template_id") or payload.get("template_id") or payload.get("id")
        key = self._idem_key(payload, header_idempotency_key)
        cached = self._idempotent_response(key, "switch_template")
        if cached is not None:
            return cached

        with self.lock:
            new_template = self._template(new_template_id)
            if new_template is None:
                return self._fail(f"新模板 {new_template_id} 不存在", error_code="TEMPLATE_NOT_FOUND", status_code=404)
            site = str(new_template.get("适用站点", ""))
            if payload.get("site") and str(payload.get("site")) != site:
                return self._fail("提交站点与模板适用站点不一致", error_code="SITE_MISMATCH")
            if new_template.get("status") == "已废弃":
                return self._fail(
                    f"新版本 {new_template.get('解析版本')} 已废弃，不能启用；请先复制或启用有效版本",
                    error_code="NEW_TEMPLATE_DEPRECATED",
                )

            old_id = payload.get("old_template_id") or payload.get("oldTemplateId")
            same_site_templates = [
                t for t in self._rows("page_parser")
                if t.get("适用站点") == site and t.get("id") != new_template.get("id")
            ]
            enabled_old = [t for t in same_site_templates if t.get("status") == "已启用"]
            enabled_old.sort(key=lambda t: (int(t.get("switch_sequence", 0) or 0), int(t.get("id", 0))))
            if old_id:
                old_template = self._template(old_id)
            elif enabled_old:
                old_template = enabled_old[-1]
            else:
                # 重复执行/服务未重置时，仍能从最近一次切换或已废弃版本还原“旧版 → 新版”。
                previous_switch = next((
                    s for s in reversed(self._rows("template_switches"))
                    if s.get("site") == site and s.get("new_template_id") == new_template.get("id")
                    and s.get("old_template_id")
                ), None)
                old_template = self._template(previous_switch.get("old_template_id")) if previous_switch else None
                if old_template is None:
                    deprecated = [t for t in same_site_templates if t.get("status") == "已废弃"]
                    deprecated.sort(key=lambda t: int(t.get("id", 0)))
                    old_template = deprecated[-1] if deprecated else None
            if old_template and old_template.get("适用站点") != site:
                return self._fail("旧模板与新模板不是同一站点，已拒绝切换",
                                  error_code="TEMPLATE_SITE_MISMATCH")

            sample_id = payload.get("sample_id")
            if sample_id is None:
                sample = next((s for s in self._rows("sample_store") if s.get("站点名称") == site), None)
                sample_id = sample.get("id") if sample else None

            seq = self._next_sequence()
            switch_id = str(payload.get("switch_id") or f"SW-{seq:04d}")
            # 切换事务：在同一把锁内废弃同站旧版、启用新版并写回执，顺序台账先于后续领取。
            deprecated_ids = set()
            for candidate in enabled_old:
                candidate["status"] = "已废弃"
                candidate["pending"] = False
                candidate["abnormal"] = False
                candidate["deprecated_at"] = utc_now()
                deprecated_ids.add(candidate.get("id"))
            if old_template is not None and old_template.get("id") not in deprecated_ids:
                old_template["status"] = "已废弃"
                old_template["pending"] = False
                old_template["abnormal"] = False
                old_template["deprecated_at"] = utc_now()
            new_template["status"] = "已启用"
            new_template["pending"] = False
            new_template["abnormal"] = False
            new_template["enabled_at"] = utc_now()
            new_template["switch_sequence"] = seq

            switch = {
                "id": switch_id,
                "receipt_no": switch_id,
                "kind": "模板切换回执",
                "sequence": seq,
                "at": utc_now(),
                "site": site,
                "sample_id": sample_id,
                "old_template_id": old_template.get("id") if old_template else None,
                "old_template_name": old_template.get("模板名称") if old_template else None,
                "old_version": old_template.get("解析版本") if old_template else None,
                "old_status": "已废弃" if old_template else None,
                "deprecated_template_ids": list(deprecated_ids | ({old_template.get("id")} if old_template else set())),
                "new_template_id": new_template.get("id"),
                "new_template_name": new_template.get("模板名称"),
                "new_version": new_template.get("解析版本"),
                "new_status": "已启用",
                # 服务端事务已生效；“未确认”表示值班员尚未确认/签收切换回执。
                "status": "待确认",
                "effective_status": "已生效",
                "ack_status": "未确认",
                "confirmed": False,
                "actor": payload.get("actor") or "值班员",
                "message": (
                    f"同站模板已按顺序切换：{old_template.get('解析版本') if old_template else '无旧版'} 废弃，"
                    f"{new_template.get('解析版本')} 启用；回执尚待值班员确认"
                ),
                "next_actions": ["回到样本页继续原回放", "确认切换回执", "按样本查询处理顺序"],
            }
            self._rows("template_switches").append(switch)
            self._ledger(
                "template.switched",
                switch["message"] + f"，切换回执 {switch_id}，顺序号 {seq}",
                actor=switch["actor"],
                sample_id=int(sample_id) if sample_id else None,
                switch_id=switch_id,
                template_id=int(new_template["id"]),
                template_version=str(new_template.get("解析版本")),
            )
            switch["ledger_sequence"] = seq
            response = {
                "ok": True,
                "message": "模板切换已被服务端受理并生效，切换回执待确认",
                "switch": switch,
                "switch_receipt": switch,
                "entry": new_template,
                "sample_state": self.sample_state(int(sample_id)) if sample_id else None,
            }
            self._save_idempotent_response(key, "switch_template", response)
            return response

    def confirm_switch(self, switch_id: str) -> dict[str, Any]:
        with self.lock:
            switch = next((s for s in self._rows("template_switches") if s.get("id") == switch_id), None)
            if switch is None:
                return self._fail(f"切换回执 {switch_id} 不存在", error_code="SWITCH_NOT_FOUND", status_code=404)
            switch["status"] = "已确认"
            switch["ack_status"] = "已确认"
            switch["confirmed"] = True
            switch["confirmed_at"] = utc_now()
            self._ledger("template.switch_acknowledged", f"切换回执 {switch_id} 已由值班员确认",
                         switch_id=switch_id)
            return {"ok": True, "message": "切换回执已确认", "switch": switch}

    def list_switches(self) -> dict[str, Any]:
        rows = sorted(self._rows("template_switches"), key=lambda s: int(s.get("sequence", 0)))
        return {"ok": True, "items": rows, "total": len(rows)}

    def ledger(self) -> dict[str, Any]:
        rows = sorted(self._rows("operation_ledger"), key=lambda e: int(e.get("sequence", 0)))
        return {"ok": True, "items": rows, "total": len(rows)}

    def queue(self, site: str | None = None) -> dict[str, Any]:
        rows = [
            r for r in self._rows("replays")
            if r.get("status") in {"待领取", "解析中", "失败待重试"}
            and (not site or r.get("site") == site)
        ]
        rows.sort(key=lambda r: int(r.get("sequence", 0) or 0))
        return {"ok": True, "items": rows, "total": len(rows)}

    # ---------- 记录辅助 ----------

    def _append_attempt(
        self, replay: dict[str, Any], state: str, ok: bool, reason: str,
        recovery_steps: list[str] | None = None,
    ) -> None:
        attempts = replay.setdefault("attempts", [])
        attempts.append({
            "attempt": len(attempts) + 1,
            "at": utc_now(),
            "state": state,
            "ok": ok,
            "reason": reason,
            "recovery_steps": recovery_steps or [],
        })

    def _next_receipt_number(self) -> int:
        numbers = [1]
        for replay in self._rows("replays"):
            for receipt in replay.get("receipts") or []:
                raw = str(receipt.get("receipt_no") or receipt.get("id") or "0")
                match = re.search(r"(\d+)$", raw)
                if match:
                    numbers.append(int(match.group(1)))
        return max(numbers) + 1

    def _make_receipt(
        self,
        replay: dict[str, Any],
        kind: str,
        status: str,
        message: str,
        next_actions: list[str],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seq = self._next_receipt_number()
        receipt = {
            "id": seq,
            "receipt_no": f"RC-{seq:04d}",
            "kind": kind,
            "status": status,
            "replay_id": replay.get("id"),
            "sample_id": replay.get("sample_id"),
            "round": replay.get("round"),
            "template_id": replay.get("template_id"),
            "template_version": replay.get("template_version"),
            "price": (replay.get("structured_fields") or {}).get("price") if replay.get("structured_fields") else None,
            "message": message,
            "issued_at": utc_now(),
            "next_actions": next_actions,
        }
        if extra:
            receipt.update(extra)
        return receipt

    def _quarantine_failure(
        self,
        sample: dict[str, Any],
        replay: dict[str, Any],
        reason: str,
        stage: str = "页面回放解析",
    ) -> dict[str, Any]:
        rows = self._rows("failure_quarantine")
        record = next((
            q for q in rows
            if q.get("replay_id") == replay.get("id") and q.get("status") != "已恢复"
        ), None)
        failed_count = len([a for a in replay.get("attempts", []) if a.get("ok") is False])
        if record is None:
            qid = max((int(r.get("id", 0) or 0) for r in rows), default=0) + 1
            record = {"id": qid}
            rows.append(record)
        record.update({
            "样本指纹": sample.get("样本指纹", ""),
            "失败阶段": stage,
            "失败原因": reason,
            "最近失败时间": utc_now(),
            "隔离时间": record.get("隔离时间") or utc_now(),
            "重试次数": str(failed_count),
            "status": "待处理",
            "pending": True,
            "abnormal": True,
            "sample_id": sample.get("id"),
            "replay_id": replay.get("id"),
            "recovery_steps": (replay.get("recovery") or {}).get("steps") or [],
        })
        return record


replay_flow = ReplayFlowService()
