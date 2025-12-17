from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional
import time


class TaskStatus(Enum):
    """任务状态"""
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    PARTIAL_SUCCESS = auto()


class StepStatus(Enum):
    """步骤状态"""
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class StepResult:
    """步骤执行结果"""
    success: bool
    output: Any = None
    error: Optional[str] = None


@dataclass
class Step:
    """
    一个"调用某个 skill"的原子步骤。
    """
    id: str
    order: int
    skill_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: Optional[StepResult] = None
    depends_on: List[str] = field(default_factory=list)
    description: Optional[str] = None


@dataclass
class Task:
    """
    一次用户请求 / 任务实例。
    由若干 Step 组成。
    """
    id: str
    goal: str
    steps: List[Step]
    intent_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_step(self, step: Step) -> None:
        """添加步骤"""
        self.steps.append(step)

