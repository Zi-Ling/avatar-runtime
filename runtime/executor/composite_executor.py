"""
复合任务执行器（Composite Task Executor）

职责（纯流程控制）：
- 执行流程控制（循环、顺序）
- 事件发布（开始、完成、失败）
- 失败策略（是否继续、终止）
- 学习记录
- 记忆记录

不做（委托给 OrchestrationService）：
- 任务分解
- 依赖解析
- Intent 构造
- 输出提取
"""
from __future__ import annotations

import logging
import time
import os
from pathlib import Path
from typing import Optional, Any, Dict, List

from .runtime.core import BaseExecutor, AgentLoopResult, TaskContext
from .runtime.events import EventType


logger = logging.getLogger(__name__)


class CompositeTaskExecutor(BaseExecutor):
    """
    复合任务执行器（瘦身版）
    
    这是一个纯流程控制器，所有业务逻辑委托给 OrchestrationService。
    """
    
    def __init__(
        self,
        orchestration_service: OrchestrationService,
        planner: Any,
        dag_runner: Any,
        skill_context: Any,
        skill_guard: Optional[Any] = None,
        failure_policy: Optional[FailurePolicy] = None,
        memory_manager: Optional[Any] = None,
        learning_logger: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        """
        初始化执行器
        
        Args:
            orchestration_service: 编排服务（核心依赖）
            planner: 任务规划器
            dag_runner: DAG 执行器
            skill_context: 技能上下文
            skill_guard: 技能守卫
            failure_policy: 失败策略
            memory_manager: 记忆管理器
            learning_logger: 学习日志记录器
            event_bus: 事件总线
        """
        super().__init__(event_bus=event_bus)
        self._orchestration = orchestration_service
        self._planner = planner
        self._dag_runner = dag_runner
        self._skill_context = skill_context
        self._skill_guard = skill_guard
        self._failure_policy = failure_policy or FailurePolicy()
        self._memory_manager = memory_manager
        self._learning_logger = learning_logger
        
        # 初始化 Replanner（用于子任务失败重规划）
        from .runtime.recovery.replanner import Replanner
        from app.config import config
        self._replanner = Replanner(
            planner=planner,
            max_replan_attempts=config.max_replan_attempts
        )
    
    async def execute(
        self,
        raw_request: str,
        original_intent: Optional[IntentSpec],
        env_context: dict
    ) -> AgentLoopResult:
        """
        执行复合任务（纯流程控制）
        
        Args:
            raw_request: 原始用户请求
            original_intent: 原始 Intent
            env_context: 环境上下文
        
        Returns:
            AgentLoopResult: 执行结果
        """
        start_time = time.time()
        
        # 🎯 [方案1-步骤3: 黑板模式] - 创建/获取共享 SessionContext
        session_context = None
        session_id = None
        
        if original_intent and hasattr(original_intent, 'metadata') and original_intent.metadata:
            session_id = original_intent.metadata.get('session_id')
        
        if session_id and self._memory_manager:
            try:
                from .runtime.core.session import SessionContext
                
                # 尝试从内存中恢复 SessionContext
                session_data = self._memory_manager.get_working_state(f"session:{session_id}:context")
                
                if session_data:
                    session_context = SessionContext.from_dict(session_data)
                    logger.info(f"[CompositeExecutor] ✅ Restored SessionContext for {session_id}")
                else:
                    # 创建新的 SessionContext
                    session_context = SessionContext.create(session_id)
                    logger.info(f"[CompositeExecutor] ✅ Created new SessionContext for {session_id}")
            except Exception as e:
                logger.warning(f"[CompositeExecutor] Failed to setup SessionContext: {e}")
                # 继续执行，不中断流程
        
        try:
            # 1. 任务分解（委托给 OrchestrationService）
            logger.info(f"[CompositeExecutor] Decomposing request: '{raw_request[:50]}...'")
            
            try:
                composite = await self._orchestration.decompose(
                    raw_request, original_intent, env_context
                )
            except Exception as decompose_error:
                # 处理任务分解失败
                from .planner.orchestrator.decomposer.exceptions import DecompositionTimeoutError
                
                if isinstance(decompose_error, DecompositionTimeoutError):
                    # 任务分解超时，返回友好错误
                    logger.error(f"[CompositeExecutor] Task decomposition failed: {decompose_error}")
                    
                    from .runtime.core.errors import ErrorClassifier, ErrorType
                    error_info = ErrorClassifier._build_error_info(
                        ErrorType.TASK_DECOMPOSITION_FAILED,
                        str(decompose_error)
                    )
                    formatted_error = ErrorClassifier.format_for_frontend(error_info)
                    
                    self._emit_event(EventType.SYSTEM_ERROR, payload={
                        "error": formatted_error["message"],
                        "error_details": formatted_error
                    })
                    
                    return AgentLoopResult(
                        success=False,
                        context=self._create_error_context(raw_request, original_intent, env_context),
                        plan=None,
                        error=formatted_error["message"],
                        iterations=0
                    )
                else:
                    # 其他分解错误，重新抛出
                    raise
            
            # 确保 session_id 传递
            if original_intent and hasattr(original_intent, 'metadata') and original_intent.metadata:
                session_id = original_intent.metadata.get('session_id')
                if session_id and 'session_id' not in composite.metadata:
                    composite.metadata['session_id'] = session_id
            
            logger.info(f"[CompositeExecutor] Decomposed into {len(composite.subtasks)} subtasks")
            
            # 发送任务分解事件
            self._emit_task_decomposed(composite)
            
            # 2. 循环执行子任务
            iteration = 0
            max_iterations = len(composite.subtasks) * 2
            
            while not composite.is_complete() and iteration < max_iterations:
                iteration += 1
                
                # 获取可执行的子任务
                ready_subtasks = composite.get_ready_subtasks()
                
                if not ready_subtasks:
                    if composite.has_failed():
                        logger.error("[CompositeExecutor] Has failed subtasks, checking for fallback...")
                        
                        # 🎯 新增：检查是否需要触发 fallback
                        failed_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.FAILED])
                        success_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.SUCCESS])
                        
                        # 如果没有任何子任务成功，或者关键子任务失败，尝试 fallback
                        if success_count == 0 or failed_count >= len(composite.subtasks) // 2:
                            logger.info(
                                f"[CompositeExecutor] Triggering fallback for orchestrated task "
                                f"(success={success_count}, failed={failed_count})"
                            )
                            # 触发 fallback（在下面的汇总阶段处理）
                            composite._needs_fallback = True
                        
                        break
                    else:
                        # 全部完成
                        break
                
                # 执行第一个准备好的子任务
                subtask = ready_subtasks[0]
                await self._execute_one_subtask(
                    subtask, composite, original_intent, env_context, session_context
                )
                
                # 发送进度更新
                self._emit_progress(composite, subtask)
                
                # 检查失败策略
                if subtask.status == SubTaskStatus.FAILED:
                    failed_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.FAILED])
                    if self._failure_policy.should_stop_on_failure(subtask.id, failed_count):
                        logger.error("[CompositeExecutor] Failure policy triggered, checking for fallback...")
                        
                        # 🎯 新增：检查是否需要触发 fallback
                        success_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.SUCCESS])
                        if success_count == 0:
                            composite._needs_fallback = True
                        
                        break
            
            # 3. 汇总结果
            success = composite.is_complete() and not composite.has_failed()
            
            # 3.5. 构建详细的错误报告（如果有失败）
            error_report = None
            if not success:
                error_report = self._build_failure_report(composite)
                
                # 🎯 新增：如果需要 fallback，尝试执行
                if getattr(composite, '_needs_fallback', False):
                    logger.info("[CompositeExecutor] Attempting fallback for failed orchestrated task...")
                    fallback_result = await self._try_fallback(raw_request, error_report, env_context)
                    
                    # 检查 fallback 是否返回了有用的内容
                    # 注意：fallback skill 的 success 总是 False，所以不检查 success 字段
                    if fallback_result and (fallback_result.get("response_zh") or fallback_result.get("message")):
                        # Fallback 执行成功，更新结果
                        logger.info("[CompositeExecutor] ✅ Fallback executed")
                        # 注意：即使 fallback 成功，原始任务仍然标记为失败
                        # 但我们会在 error_report 中添加 fallback 结果供前端展示
                        zh_response = fallback_result.get('response_zh', '')
                        en_response = fallback_result.get('response_en', '')
                        fallback_msg = zh_response or en_response or fallback_result.get('message', '')
                        error_report = f"{error_report}\n\n[Fallback Response]\n{fallback_msg}"
                    else:
                        logger.warning("[CompositeExecutor] ❌ Fallback execution failed")
            
            # 4. 记录学习数据
            if self._learning_logger:
                self._learning_logger.record(
                    user_request=raw_request,
                    plan=composite,
                    context={
                        "type": "orchestrated",
                        "subtask_count": len(composite.subtasks),
                        "success": success,
                        "iterations": iteration
                    }
                )
            
            # 5. 记录记忆
            if self._memory_manager:
                self._record_memory(composite, raw_request, start_time)
            
            # 6. 持久化 SessionContext（黑板状态）
            if session_context and self._memory_manager and session_id:
                try:
                    self._memory_manager.set_working_state(
                        f"session:{session_id}:context",
                        session_context.to_dict()
                    )
                    logger.info(f"[CompositeExecutor] ✅ Persisted SessionContext for {session_id}")
                except Exception as e:
                    logger.warning(f"[CompositeExecutor] Failed to persist SessionContext: {e}")
            
            # 7. 发送最终事件
            self._emit_final_event(composite, success)
            
            # 8. 构建最终上下文
            final_context = self._build_final_context(composite, env_context)
            
            return AgentLoopResult(
                success=success,
                context=final_context,
                plan=composite,
                error=error_report,
                iterations=iteration
            )
            
        except Exception as e:
            logger.error(f"[CompositeExecutor] Fatal error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            self._emit_event(EventType.SYSTEM_ERROR, payload={
                "error": f"Orchestration failed: {str(e)}"
            })
            
            return AgentLoopResult(
                success=False,
                context=self._create_error_context(raw_request, original_intent, env_context),
                plan=None,
                error=str(e),
                iterations=0
            )
    
    async def _execute_one_subtask(
        self,
        subtask: Any,
        composite: CompositeTask,
        original_intent: Optional[IntentSpec],
        env_context: dict,
        session_context: Optional[Any] = None
    ):
        """
        执行单个子任务（纯流程控制）
        
        Args:
            subtask: 当前子任务
            composite: 所属的复合任务
            original_intent: 原始 Intent
            env_context: 环境上下文
        """
        logger.info(f"[CompositeExecutor] Executing subtask {subtask.id}: '{subtask.goal}'")
        
        # 发送子任务开始事件
        self._emit_event(EventType.SUBTASK_START, payload={
            "subtask_id": subtask.id,
            "goal": subtask.goal,
            "order": subtask.order,
            "total": len(composite.subtasks),
            "session_id": composite.metadata.get("session_id")
        })
        
        subtask.status = SubTaskStatus.RUNNING
        
        try:
            # 1. 创建 Intent（委托给 OrchestrationService）
            #    ← 这里会自动：解析依赖 + 设置完整 metadata（包括 subtask_type）
            completed_subtasks = composite.get_completed_subtasks()
            
            intent = self._orchestration.create_subtask_intent(
                subtask, composite, original_intent, completed_subtasks
            )
            
            # 2. 规划子任务步骤
            task = await self._planner.make_task(
                intent,
                env_context,
                ctx=None,
                memory=None
            )
            
            # 发送规划完成事件
            self._emit_event(EventType.PLAN_GENERATED, payload={
                "subtask_id": subtask.id,
                "subtask_goal": subtask.goal,
                "task": self._sanitize_task(task),
                "parent_composite_id": composite.id
            })
            
            # 2.5. 参数校验 + 补救（规划后、执行前）
            from .planner.core.validation.step_validator import StepValidator
            
            validation_context = {
                "subtask_goal": subtask.goal,  # ← 关键：最精确的目标
                "user_goal": getattr(original_intent, "request", "") if original_intent else "",
                "artifacts": self._collect_available_artifacts(composite),
                "blackboard": session_context.to_dict() if session_context else {},
                "extra_texts": self._collect_dependency_outputs(composite, subtask)
            }
            
            validation_result = StepValidator.validate_and_resolve_params(
                task=task,
                context=validation_context
            )
            
            if not validation_result.success:
                # 参数缺失，直接触发 Replan
                logger.warning(
                    f"[CompositeExecutor] Parameter validation failed for subtask {subtask.id}. "
                    f"Missing: {validation_result.missing_params}. Triggering replan..."
                )
                
                # 触发 Replan
                failed_step = task.steps[0] if task.steps else None
                replan_success = await self._replanner.replan(task, failed_step, env_context)
                
                if replan_success:
                    # Replan 成功，重新校验并执行
                    logger.info(f"[CompositeExecutor] ⚡ Replanned subtask {subtask.id} after param validation failure")
                    
                    # 重新校验
                    validation_result = StepValidator.validate_and_resolve_params(
                        task=task,
                        context=validation_context
                    )
                    
                    if not validation_result.success:
                        # Replan 后仍然失败
                        subtask.status = SubTaskStatus.FAILED
                        subtask.error = f"Parameter validation failed even after replan: {validation_result.error}"
                        logger.error(f"[CompositeExecutor] ❌ Subtask {subtask.id} failed: {subtask.error}")
                        return
                else:
                    # Replan 失败
                    subtask.status = SubTaskStatus.FAILED
                    subtask.error = f"Parameter validation failed and replan exhausted: {validation_result.error}"
                    logger.error(f"[CompositeExecutor] ❌ Subtask {subtask.id} failed: {subtask.error}")
                    return
            
            # 3. 执行子任务
            context = TaskContext.from_task(task, env=env_context)
            if self._memory_manager:
                context.attach("memory_manager", self._memory_manager)
            
            # 🎯 [方案1-步骤3: 黑板模式] - 注入共享 SessionContext
            if session_context:
                context.attach("session_context", session_context)
                
                # 从 SessionContext 中读取上游依赖的输出
                if subtask.depends_on:
                    for dep_id in subtask.depends_on:
                        # 尝试从 SessionContext.variables 中获取上游输出
                        upstream_key = f"subtask_{dep_id}_output"
                        upstream_value = session_context.get_variable(upstream_key)
                        
                        if upstream_value is not None:
                            # 注入到当前 TaskContext，供本子任务使用
                            context.variables.set(f"upstream_{dep_id}", upstream_value)
                            logger.debug(
                                f"[CompositeExecutor] Injected upstream output from {dep_id} "
                                f"via SessionContext (blackboard)"
                            )
            
            if hasattr(self._skill_context, "execution_context"):
                self._skill_context.execution_context = context
            
            task.status = TaskStatus.RUNNING
            context.mark_running()
            
            await self._dag_runner.run(
                task,
                ctx=self._skill_context,
                state=None,
                skill_guard=self._skill_guard,
                event_bus=self.event_bus
            )
            
            # 4. 检查执行结果
            if task.status == TaskStatus.SUCCESS:
                # 收集输出（委托给 OrchestrationService）
                outputs = self._orchestration.collect_subtask_outputs(
                    subtask, task, composite
                )
                
                # 🎯 [方案C] 兜底写盘：确保期望的文件被写入
                outputs = self._ensure_files_written(subtask, outputs)
                
                subtask.status = SubTaskStatus.SUCCESS
                subtask.task_id = task.id
                subtask.task_result = task
                
                # 🎯 [方案1-步骤3: 黑板模式] - 同步输出到 SessionContext
                if session_context:
                    try:
                        # 1. 同步所有收集到的输出
                        for output_key, output_value in outputs.items():
                            session_context.set_variable(
                                f"subtask_{subtask.id}_{output_key}",
                                output_value
                            )
                        
                        # 2. 同步主输出（如果有）
                        if outputs:
                            main_output = list(outputs.values())[0]
                            session_context.set_variable(
                                f"subtask_{subtask.id}_output",
                                main_output
                            )
                        
                        # 3. 同步 TaskContext.variables 中的所有变量
                        for var_name, var_value in context.variables.vars.items():
                            if not var_name.startswith("_"):
                                session_context.set_variable(
                                    f"subtask_{subtask.id}_var_{var_name}",
                                    var_value
                                )
                        
                        # 4. 同步 Artifacts（产物）
                        for artifact in context.artifacts.items:
                            session_context.add_artifact({
                                "id": artifact.id,
                                "type": artifact.type,
                                "uri": artifact.uri,
                                "meta": {
                                    **artifact.meta,
                                    "subtask_id": subtask.id,
                                    "composite_id": composite.id
                                }
                            })
                        
                        logger.info(
                            f"[CompositeExecutor] ✅ Synced {len(outputs)} outputs + "
                            f"{len(context.artifacts.items)} artifacts to SessionContext (blackboard)"
                        )
                    except Exception as e:
                        logger.warning(f"[CompositeExecutor] Failed to sync to SessionContext: {e}")
                
                logger.info(
                    f"[CompositeExecutor] Subtask {subtask.id} succeeded. "
                    f"Outputs: {list(outputs.keys())}"
                )
                
                # 缓存成功的计划（v2 架构：执行后才缓存）
                try:
                    plan_cache = get_plan_cache(self._memory_manager)
                    intent_type = getattr(intent, "intent_type", "action")
                    domain = getattr(intent, "domain", "general")
                    # 从 intent.params 获取解析后的输入参数
                    resolved_inputs = getattr(intent, "params", {})
                    
                    cache_success = plan_cache.put(
                        task=task,
                        resolved_inputs=resolved_inputs,
                        intent_type=intent_type,
                        domain=domain
                    )
                    
                    if cache_success:
                        logger.info(f"[CompositeExecutor] ✅ Plan cached for subtask {subtask.id}")
                    else:
                        logger.debug(f"[CompositeExecutor] Plan not cached (rejected by validator)")
                except Exception as e:
                    # 缓存失败不影响执行结果
                    logger.warning(f"[CompositeExecutor] Failed to cache plan: {e}")
                
                # 发送子任务完成事件
                self._emit_subtask_complete(subtask, task, composite)
            else:
                # 失败处理：尝试重规划或使用 fallback
                error_details = []
                error_details.append(f"Task execution failed with status: {task.status.name}")
                
                # 收集失败步骤的具体错误
                from .planner.models import StepStatus
                failed_steps = [s for s in task.steps if s.status == StepStatus.FAILED]
                if failed_steps:
                    error_details.append(f"Failed steps: {len(failed_steps)}/{len(task.steps)}")
                    for step in failed_steps[:3]:
                        step_error = step.result.error if step.result and hasattr(step.result, 'error') else "Unknown error"
                        error_details.append(f"  - {step.skill_name}: {step_error}")
                
                error_msg = "\n".join(error_details)
                
                # 尝试重规划（原地修改 task）
                replan_success = await self._replanner.replan(task, failed_steps[0] if failed_steps else None, env_context)
                
                if replan_success:
                    # 重规划成功，重新执行
                    logger.info(f"[CompositeExecutor] ⚡ Replanned subtask {subtask.id}, re-executing...")
                    
                    context = TaskContext.from_task(task, env=env_context)
                    if self._memory_manager:
                        context.attach("memory_manager", self._memory_manager)
                    if session_context:
                        context.attach("session_context", session_context)
                    if hasattr(self._skill_context, "execution_context"):
                        self._skill_context.execution_context = context
                    
                    task.status = TaskStatus.RUNNING
                    context.mark_running()
                    
                    await self._dag_runner.run(
                        task,
                        ctx=self._skill_context,
                        state=None,
                        skill_guard=self._skill_guard,
                        event_bus=self.event_bus
                    )
                    
                    # 再次检查结果
                    if task.status == TaskStatus.SUCCESS:
                        outputs = self._orchestration.collect_subtask_outputs(subtask, task, composite)
                        outputs = self._ensure_files_written(subtask, outputs)
                        subtask.status = SubTaskStatus.SUCCESS
                        subtask.task_id = task.id
                        subtask.task_result = task
                        logger.info(f"[CompositeExecutor] ✅ Subtask {subtask.id} succeeded after replan")
                        
                        # 缓存重规划后成功的计划（v2 架构：执行后才缓存）
                        try:
                            plan_cache = get_plan_cache(self._memory_manager)
                            intent_type = getattr(intent, "intent_type", "action")
                            domain = getattr(intent, "domain", "general")
                            resolved_inputs = getattr(intent, "params", {})
                            
                            cache_success = plan_cache.put(
                                task=task,
                                resolved_inputs=resolved_inputs,
                                intent_type=intent_type,
                                domain=domain
                            )
                            
                            if cache_success:
                                logger.info(f"[CompositeExecutor] ✅ Replanned plan cached for subtask {subtask.id}")
                            else:
                                logger.debug(f"[CompositeExecutor] Replanned plan not cached (rejected by validator)")
                        except Exception as e:
                            logger.warning(f"[CompositeExecutor] Failed to cache replanned plan: {e}")
                        
                        self._emit_subtask_complete(subtask, task, composite)
                        return
                
                # 重规划失败或仍然失败，标记为失败
                subtask.status = SubTaskStatus.FAILED
                subtask.task_id = task.id
                subtask.task_result = task
                subtask.error = error_msg
                
                logger.error(
                    f"[CompositeExecutor] ❌ Subtask {subtask.id} failed (after replan attempt):\n"
                    f"  Goal: {subtask.goal}\n"
                    f"  Depends on: {subtask.depends_on}\n"
                    f"  Error: {subtask.error}"
                )
                
                # 发送子任务失败事件
                self._emit_event(EventType.SUBTASK_FAILED, payload={
                    "subtask_id": subtask.id,
                    "goal": subtask.goal,
                    "error": subtask.error,
                    "depends_on": subtask.depends_on,
                    "session_id": composite.metadata.get("session_id")
                })
        
        except Exception as e:
            # 🎯 【防止 python.run 乱入】步骤3：禁止所有类型约束错误的 type 改写重试
            error_msg = str(e)
            is_type_constraint_error = (
                "Forbidden skills used" in error_msg or
                "Plan validation failed for subtask type" in error_msg or
                "生成的计划违反了任务类型约束" in error_msg
            )
            
            if is_type_constraint_error:
                # 🚫 新策略：所有类型约束错误都不允许改 type 重试
                # 原因：这是 python.run 乱入的主要后门
                logger.error(
                    f"[CompositeExecutor] 🚫 Type constraint violation detected for subtask {subtask.id}. "
                    f"Marking as failed (no type-change retry to prevent skill constraint bypass)"
                )
                
                # 提取被禁止的技能（用于错误报告）
                import re
                forbidden_skills_match = re.search(r"Forbidden skills used: \[([^\]]+)\]", error_msg)
                if forbidden_skills_match:
                    skills_str = forbidden_skills_match.group(1)
                    used_skills = [s.strip().strip("'\"") for s in skills_str.split(",")]
                    logger.error(
                        f"[CompositeExecutor] Planner violated type constraints by using: {used_skills} "
                        f"(not allowed for subtask type '{subtask.type.value}')"
                    )
                    
                    # 增强错误消息
                    enhanced_error = (
                        f"Planning failed: Attempted to use forbidden skills {used_skills} "
                        f"for task type '{subtask.type.value}'. This indicates a planner constraint violation. "
                        f"Consider rephrasing the request or breaking it into simpler subtasks."
                    )
                    e = RuntimeError(enhanced_error)
                
                # 不重试，直接执行下面的失败处理逻辑
            
            # 失败处理（原有逻辑）
            subtask.status = SubTaskStatus.FAILED
            subtask.error = f"{type(e).__name__}: {str(e)}"
            logger.error(
                f"[CompositeExecutor] ❌ Subtask {subtask.id} exception:\n"
                f"  Goal: {subtask.goal}\n"
                f"  Depends on: {subtask.depends_on}\n"
                f"  Exception: {subtask.error}"
            )
            import traceback
            logger.error(traceback.format_exc())
            
            # 发送子任务失败事件
            self._emit_event(EventType.SUBTASK_FAILED, payload={
                "subtask_id": subtask.id,
                "goal": subtask.goal,
                "error": str(e),
                "session_id": composite.metadata.get("session_id")
            })
    
    # ========== 事件发布方法 ==========
    
    def _emit_task_decomposed(self, composite: CompositeTask):
        """发送任务分解事件"""
        steps_summary = [{"id": st.id, "goal": st.goal} for st in composite.subtasks]
        self._emit_event(EventType.TASK_DECOMPOSED, payload={
            "message": f"识别到 {len(composite.subtasks)} 个子任务",
            "steps": steps_summary,
            "session_id": composite.metadata.get("session_id")
        })
        
        self._emit_event(EventType.PLAN_GENERATED, payload={
            "composite_task": composite.to_dict(),
            "subtask_count": len(composite.subtasks)
        })
    
    def _emit_subtask_complete(self, subtask: Any, task: Task, composite: CompositeTask):
        """发送子任务完成事件"""
        from .planner.models import StepStatus
        
        summary = "执行完成"
        raw_output = None
        skill_name = None
        duration = 0
        
        # 获取最后一个成功步骤的输出
        success_steps = [s for s in task.steps if s.status == StepStatus.SUCCESS]
        if success_steps:
            last_step = success_steps[-1]
            skill_name = last_step.skill_name
            raw_output = last_step.result.output if last_step.result else None
            duration = last_step.result.duration if last_step.result and hasattr(last_step.result, 'duration') else 0
        
        self._emit_event(EventType.SUBTASK_COMPLETE, payload={
            "subtask_id": subtask.id,
            "goal": subtask.goal,
            "summary": summary,
            "skill_name": skill_name,
            "raw_output": raw_output,
            "duration": duration,
            "session_id": composite.metadata.get("session_id")
        })
    
    def _emit_progress(self, composite: CompositeTask, current_subtask: Any):
        """发送进度更新事件"""
        update_payload = {
            "composite_task": composite.to_dict(),
            "current_subtask": current_subtask.id,
            "subtask_status": current_subtask.status.value
        }
        
        if current_subtask.task_result:
            update_payload["current_subtask_task"] = self._sanitize_task(current_subtask.task_result)
        
        self._emit_event(EventType.TASK_UPDATED, payload=update_payload)
    
    def _emit_final_event(self, composite: CompositeTask, success: bool):
        """发送最终事件"""
        composite_dict = composite.to_dict()
        
        # 为每个子任务添加完整的task执行结果
        for i, subtask in enumerate(composite.subtasks):
            if subtask.task_result:
                try:
                    task_dict = self._sanitize_task(subtask.task_result)
                    composite_dict["subtasks"][i]["task"] = task_dict
                except Exception as e:
                    logger.error(f"Failed to sanitize task for subtask {i}: {e}")
        
        if success:
            logger.info(f"[CompositeExecutor] 🎉 All subtasks completed successfully")
            self._emit_event(EventType.TASK_COMPLETED, payload={
                "composite_task": composite_dict,
                "success": True,
                "type": "orchestrated"
            })
        else:
            # 部分失败时，仍然发送 TASK_COMPLETED 事件（前端会根据 subtask status 显示）
            # 不发送 SYSTEM_ERROR，避免误导用户
            failed_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.FAILED])
            logger.warning(f"[CompositeExecutor] Completed with {failed_count} failed subtasks")
            self._emit_event(EventType.TASK_COMPLETED, payload={
                "composite_task": composite_dict,
                "success": False,  # 标记为部分成功
                "type": "orchestrated",
                "partial_success": True,
                "failed_count": failed_count
            })
    
    # ========== 辅助方法 ==========
    
    def _collect_available_artifacts(self, composite: CompositeTask) -> List[str]:
        """收集已完成子任务生成的 Artifacts"""
        artifacts = []
        for st in composite.get_completed_subtasks():
            if hasattr(st, "task_result") and st.task_result:
                task = st.task_result
                for step in task.steps:
                    if step.result and hasattr(step.result, "output") and isinstance(step.result.output, dict):
                        # 提取路径类字段
                        for key in ["path", "file_path", "output_path", "dst"]:
                            if key in step.result.output:
                                artifacts.append(str(step.result.output[key]))
        return artifacts
    
    def _collect_dependency_outputs(self, composite: CompositeTask, current_subtask: Any) -> List[str]:
        """收集当前子任务依赖的上游输出文本"""
        texts = []
        if not current_subtask.depends_on:
            return texts
        
        for dep_id in current_subtask.depends_on:
            dep_subtask = next((st for st in composite.subtasks if st.id == dep_id), None)
            if not dep_subtask or not hasattr(dep_subtask, "task_result"):
                continue
            
            task = dep_subtask.task_result
            if not task or not task.steps:
                continue
            
            # 提取输出的文本摘要
            for step in task.steps:
                if step.result and hasattr(step.result, "output"):
                    output = step.result.output
                    if isinstance(output, dict):
                        for key, val in output.items():
                            if isinstance(val, str) and 2 < len(val) < 200:
                                texts.append(val)
                    elif isinstance(output, str) and 2 < len(output) < 200:
                        texts.append(output)
        
        return texts
    
    def _build_failure_report(self, composite: CompositeTask) -> str:
        """
        构建详细的失败报告
        
        Args:
            composite: 复合任务
        
        Returns:
            str: 详细的错误报告
        """
        failed_subtasks = [st for st in composite.subtasks if st.status == SubTaskStatus.FAILED]
        pending_subtasks = [st for st in composite.subtasks if st.status == SubTaskStatus.PENDING]
        success_subtasks = [st for st in composite.subtasks if st.status == SubTaskStatus.SUCCESS]
        
        report_lines = [
            "Orchestrated task failed:",
            f"  Total subtasks: {len(composite.subtasks)}",
            f"  Succeeded: {len(success_subtasks)}",
            f"  Failed: {len(failed_subtasks)}",
            f"  Pending (skipped): {len(pending_subtasks)}",
            ""
        ]
        
        # 列出失败的子任务
        if failed_subtasks:
            report_lines.append("Failed subtasks:")
            for st in failed_subtasks:
                report_lines.append(f"  - {st.id}: {st.goal}")
                if st.depends_on:
                    report_lines.append(f"    Depends on: {st.depends_on}")
                if st.error:
                    # 限制错误消息长度
                    error_preview = st.error[:200] + "..." if len(st.error) > 200 else st.error
                    report_lines.append(f"    Error: {error_preview}")
            report_lines.append("")
        
        # 列出被跳过的子任务（因为依赖失败）
        if pending_subtasks:
            report_lines.append("Skipped subtasks (dependencies not met):")
            for st in pending_subtasks:
                report_lines.append(f"  - {st.id}: {st.goal}")
                if st.depends_on:
                    report_lines.append(f"    Depends on: {st.depends_on}")
            report_lines.append("")
        
        # 依赖链分析
        report_lines.append("Dependency chain:")
        for st in composite.subtasks:
            status_icon = "✓" if st.status == SubTaskStatus.SUCCESS else ("✗" if st.status == SubTaskStatus.FAILED else "○")
            deps_str = f" (depends on {st.depends_on})" if st.depends_on else ""
            report_lines.append(f"  {status_icon} {st.id}{deps_str}")
        
        return "\n".join(report_lines)
    
    def _record_memory(self, composite: CompositeTask, raw_request: str, start_time: float):
        """记录记忆"""
        try:
            duration = time.time() - start_time
            success_count = len([st for st in composite.subtasks if st.status == SubTaskStatus.SUCCESS])
            
            # 生成记忆文本
            summary = f"分解为 {len(composite.subtasks)} 个子任务，成功 {success_count} 个"
            
            # 判断最终状态
            final_status = "success" if success_count == len(composite.subtasks) else "partial_success"
            if success_count == 0:
                final_status = "failed"
            
            # 使用正确的方法：remember_task_run
            self._memory_manager.remember_task_run(
                task_id=composite.id,
                status=final_status,
                summary=summary,
                extra={
                    "user_request": raw_request,
                    "subtask_count": len(composite.subtasks),
                    "success_count": success_count,
                    "duration": duration,
                    "subtasks": [
                        {"id": st.id, "goal": st.goal, "status": st.status.value}
                        for st in composite.subtasks
                    ]
                }
            )
            
            logger.info(f"✅ Orchestrated task memory recorded: {composite.id}")
        except Exception as e:
            logger.error(f"Failed to record memory: {e}")
    
    def _build_final_context(self, composite: CompositeTask, env_context: dict) -> dict:
        """构建最终上下文"""
        success_subtasks = [st for st in composite.subtasks if st.status == SubTaskStatus.SUCCESS]
        
        # 收集所有输出
        all_outputs = {}
        for st in success_subtasks:
            all_outputs[st.id] = st.actual_outputs
        
        return {
            "composite_task_id": composite.id,
            "subtasks": [st.to_dict() for st in composite.subtasks],
            "outputs": all_outputs,
            "success_count": len(success_subtasks),
            "total_count": len(composite.subtasks)
        }
    
    def _create_error_context(self, raw_request: str, original_intent: Optional[IntentSpec], env_context: dict) -> dict:
        """创建错误上下文"""
        return {
            "error": "Orchestration failed",
            "request": raw_request,
            "intent_id": getattr(original_intent, "id", None)
        }
    
    def _ensure_files_written(self, subtask: Any, outputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        [方案C] 兜底写盘
        
        如果 subtask 期望输出文件，且有内容生成但未写盘，则自动写入。
        """
        if not hasattr(subtask, 'expected_outputs') or not subtask.expected_outputs:
            return outputs
            
        # 提取可用内容
        # 优先使用 content/text 字段
        content = outputs.get("content") or outputs.get("text") or outputs.get("generated_content")
        
        # 如果没有直接的内容字段，尝试查找任何长文本输出
        if not content:
            for k, v in outputs.items():
                if k.endswith("_output") and isinstance(v, str) and len(v) > 10:
                    content = v
                    break
        
        if not content or not isinstance(content, str):
            return outputs
            
        updated_outputs = outputs.copy()
        files_written = []
        
        for expected_file in subtask.expected_outputs:
            # 1. 检查是否是文件目标（通过扩展名）
            # 注意：排除 .png 等非文本格式，除非后续支持
            text_exts = ['.txt', '.md', '.csv', '.json', '.yaml', '.py', '.log', '.xml', '.html', '.css', '.js']
            is_text_file = any(expected_file.endswith(ext) for ext in text_exts)
            
            if not is_text_file:
                continue
                
            # 2. 检查是否已经写盘
            # 判断依据：输出中是否包含该文件名的路径，或者明确的 file_path 字段指向它
            already_written = False
            
            # 检查 values 中是否包含该文件名
            for v in outputs.values():
                if isinstance(v, str) and expected_file in v:
                    # 简单启发式：如果输出值包含文件名，可能是文件路径
                    # 进一步检查文件是否存在
                    try:
                        if os.path.exists(v):
                            already_written = True
                            break
                    except:
                        pass
            
            # 检查当前目录是否存在该文件（且是最近修改的？）
            # 这里简化处理：如果文件已存在且最近修改，假设已写入
            if not already_written and os.path.exists(expected_file):
                # 检查修改时间是否在任务开始之后（这里没有任务开始时间，只能粗略判断）
                # 为安全起见，如果文件存在，我们倾向于认为它被写过了，或者至少不覆盖它
                # 但为了修复"只生成未写入"的问题，如果文件大小为0或不存在，才写入
                if os.path.getsize(expected_file) > 0:
                    # check content similarity? No, too complex.
                    # Assume if it exists, the skill wrote it.
                    already_written = True
            
            if already_written:
                continue
                
            # 3. 执行兜底写入
            try:
                logger.info(f"[CompositeExecutor] 🛡️ Fallback: Writing content to '{expected_file}' for subtask {subtask.id}")
                
                # 写入当前工作目录
                file_path = Path(expected_file)
                file_path.write_text(content, encoding='utf-8')
                
                abs_path = str(file_path.absolute())
                updated_outputs[f"file_{expected_file}"] = abs_path
                files_written.append(expected_file)
                
            except Exception as e:
                logger.error(f"[CompositeExecutor] Fallback write failed for {expected_file}: {e}")
        
        if files_written:
            logger.info(f"[CompositeExecutor] ✅ Fallback wrote {len(files_written)} files: {files_written}")
            
        return updated_outputs

    def _sanitize_task(self, task: Task) -> dict:
        """序列化 Task 对象（用于事件）"""
        try:
            return {
                "id": task.id,
                "goal": task.goal,
                "status": task.status.name,
                "steps": [
                    {
                        "id": s.id,
                        "skill_name": s.skill_name,
                        "status": s.status.name,
                        "order": s.order
                    }
                    for s in task.steps
                ]
            }
        except Exception as e:
            logger.error(f"Failed to sanitize task: {e}")
            return {"id": getattr(task, "id", "unknown"), "error": str(e)}
    
    async def _try_fallback(
        self,
        user_request: str,
        error_report: str,
        env_context: dict
    ) -> Optional[Dict[str, Any]]:
        """
        尝试使用 Fallback Skill 兜底
        
        Args:
            user_request: 原始用户请求
            error_report: 失败报告
            env_context: 环境上下文
        
        Returns:
            Fallback 执行结果，如果失败则返回 None
        """
        try:
            from .skills.registry import skill_registry
            
            # 获取 fallback skill
            fallback_skill_cls = skill_registry.get("llm.fallback")
            if not fallback_skill_cls:
                logger.warning("[CompositeExecutor] Fallback skill not found in registry")
                return None
            
            # 准备 fallback 参数
            fallback_params = {
                "user_message": user_request,
                "intent": "orchestrated_task",
                "reason": f"Orchestrated task failed: {error_report[:300]}"  # 截断
            }
            
            # 创建 SkillContext
            from .skills.context import SkillContext
            fallback_ctx = SkillContext(
                base_path=self._skill_context.base_path if hasattr(self._skill_context, 'base_path') else None,
                dry_run=False,
                memory_manager=self._memory_manager,
                learning_manager=self._learning_logger
            )
            
            # 执行 fallback
            fallback_skill = fallback_skill_cls()
            input_obj = fallback_skill_cls.spec.input_model(**fallback_params)
            result = fallback_skill.run(fallback_ctx, input_obj)
            
            # 处理异步结果
            import asyncio
            if asyncio.iscoroutine(result):
                result = await result
            
            # 转换为字典
            if hasattr(result, 'model_dump'):
                return result.model_dump()
            elif isinstance(result, dict):
                return result
            else:
                return {"success": True, "message": str(result)}
                
        except Exception as e:
            logger.error(f"[CompositeExecutor] Fallback execution failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
