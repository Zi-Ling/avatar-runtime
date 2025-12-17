# runtime/avatar.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import logging
import asyncio
import inspect
import sys

current_dir = Path(__file__).parent
project_root = current_dir.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from skills import SkillContext
from skills.registry import skill_registry
from .models import Task, StepStatus, TaskStatus


logger = logging.getLogger(__name__)


@dataclass
class _SkillCaller:
    """
    Pure V2 Adapter - 简化版
    """

    base_path: Path
    dry_run: bool = False
    workspace_manager: Optional[Any] = None

    async def call_skill(self, name: str, params: Dict[str, Any]) -> Any:
        skill_cls = skill_registry.get(name)
        if not skill_cls:
            raise ValueError(f"Skill not found: {name}")
        
        # Validation: Enforce V2 Spec
        if not hasattr(skill_cls, 'spec') or not skill_cls.spec.input_model:
             raise ValueError(f"Skill '{name}' is not a valid V2 skill (missing input_model).")

        skill_instance = skill_cls()

        # 动态获取当前工作目录
        current_workspace = self.base_path
        if self.workspace_manager:
            try:
                current_workspace = self.workspace_manager.get_workspace()
            except Exception as e:
                logger.warning(f"Failed to get workspace from manager, using default: {e}")

        ctx = SkillContext(
            base_path=current_workspace,
            dry_run=self.dry_run
        )
        
        # 参数验证
        try:
            input_obj = skill_cls.spec.input_model(**params)
        except TypeError as e:
            # 转换 init missing required argument 错误
            import re
            match = re.search(r"missing \d+ required positional argument[s]?: '(\w+)'", str(e))
            if match:
                field = match.group(1)
                raise ValueError(f"Schema validation failed: '{field}' is required")
            raise ValueError(f"Schema validation failed: {e}")
        except Exception as e:
            raise ValueError(f"Invalid parameters for skill '{name}': {e}")

        # 检查 run 方法是否是协程函数
        if inspect.iscoroutinefunction(skill_instance.run):
            result = await skill_instance.run(ctx, input_obj)
        else:
            result = skill_instance.run(ctx, input_obj)
        
        return result


class AvatarMain:
    def __init__(
        self,
        base_path: str | Path,
        *,
        llm_client: Optional[Any] = None,
        dry_run: bool = False,
        workspace_manager: Optional[Any] = None,
        use_tool_calling: bool = False,
    ) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self.llm_client = llm_client
        self.workspace_manager = workspace_manager
        self.use_tool_calling = use_tool_calling



    def _check_policy(self, skill_name: str, params: dict) -> None:
        """简单的 Policy 检查（模拟 Security Guard）"""
        # 规则1: 禁止删除根目录
        if skill_name == "file.remove":
            abs_path = params.get("abs_path")
            if abs_path and (abs_path == "/" or abs_path == "C:\\" or abs_path == "D:\\"):
                raise RuntimeError(f"Denied by policy: file.remove is only allowed under ./workspace (attempted to remove system root '{abs_path}')")
        
        # 规则2: 禁止写出 workspace (简单模拟)
        # if skill_name.startswith("file.write"):
        #     pass

    async def run_task(self, task: Task, step_interval: float = 0.5) -> Task:
        """简化版任务执行器 - 直接执行，无需 runner"""
        from .models import StepResult
        
        # 创建 SkillCaller
        caller = _SkillCaller(
            base_path=self.base_path,
            dry_run=self.dry_run,
            workspace_manager=self.workspace_manager,
        )
        
        # 按顺序执行所有步骤
        for i, step in enumerate(task.steps, 1):
            # Pacing: 模拟流水线节奏
            if step_interval > 0:
                print(f"   [Runtime] Executing step {i}/{len(task.steps)}: {step.skill_name}...")
                await asyncio.sleep(step_interval)

            try:
                # 1. Policy Check
                self._check_policy(step.skill_name, step.params)
                
                # 2. 调用 skill
                result = await caller.call_skill(step.skill_name, step.params)
                
                # 转换结果
                if hasattr(result, 'model_dump'):
                    output = result.model_dump()
                elif isinstance(result, dict):
                    output = result
                else:
                    output = {"raw": str(result)}
                
                # 判断成功/失败 (默认成功，除非抛出异常)
                # 因为我们移除了 success 字段，所以正常返回就是成功
                success = output.get('success', True)
                
                if success:
                    step.status = StepStatus.SUCCESS
                    step.result = StepResult(success=True, output=output)
                else:
                    # 兼容旧代码或某些 skill 仍返回 success=False 的情况
                    step.status = StepStatus.FAILED
                    error_msg = output.get('message', 'Unknown error')
                    step.result = StepResult(success=False, error=error_msg, output=output)
                    task.status = TaskStatus.FAILED
                    return task
                    
            except Exception as e:
                # 执行异常 (包含 Policy Error, ValidationError, Skill Runtime Error)
                step.status = StepStatus.FAILED
                step.result = StepResult(success=False, error=str(e))
                task.status = TaskStatus.FAILED
                return task
        
        # 所有步骤成功
        task.status = TaskStatus.SUCCESS
        return task
