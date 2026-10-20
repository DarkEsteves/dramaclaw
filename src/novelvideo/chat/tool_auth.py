"""Recognize authentication failures only in host tool result envelopes."""
from __future__ import annotations

import json
from typing import Any

AGENT_SESSION_INVALID_MESSAGE = '系统工具会话已失效，本轮已停止。请恢复服务会话后重试；已保存的原片分析仍可继续使用。'


def agent_session_invalid_result(result: Any) -> bool:
    if not isinstance(result, dict) or result.get('ok') is not False:
        return False
    # Examine transport diagnostics only. Data, transcript, media metadata and
    # nested business objects are untrusted content, even in a failed response.
    return (
        result.get('status_code') == 401
        or result.get('code') in {'invalid_agent_session', 'agent_session_invalid', 'invalid_session', 'auth_invalid'}
        or any(result.get(key) == 'Invalid agent session' for key in ('error', 'detail', 'message'))
    )


def _result_envelopes(value: Any):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if not isinstance(value, dict):
        return
    yield value
    if "ok" in value:
        return
    structured = value.get('structured_content') or value.get('structuredContent')
    if isinstance(structured, dict):
        yield structured
    # MCP text blocks encode the response itself; never recurse into data.
    for block in value.get('content') or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text')
            if isinstance(text, str):
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    yield payload


def codex_tool_agent_session_invalid(event: Any) -> bool:
    if (getattr(event, 'type', None) != 'tool_updated'
            or not str(getattr(event, 'name', '') or '').startswith('dramaclaw.')):
        return False
    return any(agent_session_invalid_result(result)
        for value in (getattr(event, 'structured', None), getattr(event, 'output', None))
        for result in _result_envelopes(value))
