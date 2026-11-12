"""
ErisPulse-ApiMCP

将 ErisPulse 运行时能力（收发消息 / 模块调用 / 模块与适配器管理 / 事件流观测）
经 MCP（Model Context Protocol）开放给 AI 客户端（Claude Desktop 等）。

架构：机器人进程内经框架 HTTP 路由开 REST 桥（``/apimcp/*``，token 鉴权）；
独立 console script ``erispulse-apimcp`` 以官方 mcp SDK 起 stdio server，
tools 经 HTTP 转发到机器人。
"""

from .Core import Main

__all__ = ["Main"]
