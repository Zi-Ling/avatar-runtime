# app/avatar/runtime/core/result.py
"""
执行结果封装
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING


@dataclass
class AgentLoopResult:
    """Agent Loop 执行结果"""
    success: bool
    context: Optional[Any]  # TaskContext
    plan: Any
    error: Optional[str] = None
    iterations: int = 0


@dataclass
class StepExecutionResult:
    """单步执行结果"""
    success: bool
    output: Any = None
    error: Optional[str] = None
    corrected: bool = False  # 是否经过自我修复

