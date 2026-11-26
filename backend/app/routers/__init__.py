"""业务路由汇总：回放富接口先注册，随后是所有模块共用的资源路由。"""
from __future__ import annotations

from app.routers import replay_flow, resource

ROUTERS = [replay_flow, resource]
