#!/usr/bin/env python3
"""
Avatar Runtime 演示引擎
"""

import json
import sys
import asyncio
import time
from pathlib import Path
import uuid

# 假设我们在模块环境下运行，直接导入
from .avatar import AvatarMain
from .models import Task, Step, TaskStatus, StepStatus


class MockLLMClient:
    """Mock LLM 客户端（演示不需要真实 LLM）"""
    
    async def chat(self, messages, **kwargs):
        """Mock chat 方法"""
        return {
            "content": "Mock LLM response",
            "role": "assistant"
        }
    
    def chat_sync(self, messages, **kwargs):
        """同步版本"""
        return self.chat(messages, **kwargs)


def load_task_from_json(json_path: Path, workspace: Path) -> Task:
    """从 JSON 文件加载任务定义，转换为 Task 对象"""
    with open(json_path, 'r', encoding='utf-8') as f:
        task_def = json.load(f)

    # 创建 Step 对象
    steps = []
    for step_def in task_def['steps']:
        step = Step(
            id=step_def['id'],
            skill_name=step_def['skill_name'],
            params=step_def['params'],
            depends_on=step_def.get('depends_on', []),
            order=len(steps)
        )
        steps.append(step)

    # 创建 Task 对象
    task = Task(
        id=f"demo_{uuid.uuid4().hex[:8]}",
        goal=task_def.get('description', 'Demo task'),
        steps=steps,
        intent_id=f"intent_{uuid.uuid4().hex[:8]}"
    )

    return task


async def run_demo_suite(
    examples_dir: str | Path, 
    workspace_dir: str | Path, 
    step_interval: float = 1.0,
    open_workspace: bool = False
):
    """运行演示套件"""
    print("""
╔═══════════════════════════════════════════════════════════════════════╗
║                 Avatar Runtime 演示                                   ║
║                                                                       ║                                  
║             展示：文件操作、参数验证、策略阻断                             ║
╚═══════════════════════════════════════════════════════════════════════╝
    """)
    
    # 设置目录
    examples_dir = Path(examples_dir).resolve()
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(exist_ok=True, parents=True)
    
    # 推断项目根目录
    try:
        project_root = Path(__file__).resolve().parent.parent.parent
    except:
        project_root = Path.cwd()

    # 清理旧文件
    print("🧹 清理工作空间...")
    try:
        for f in workspace.glob("*.txt"):
            f.unlink()
    except Exception:
        pass
    
    # 尝试显示相对路径
    try:
        rel_workspace = workspace.relative_to(project_root)
        rel_examples = examples_dir.relative_to(project_root)
        print(f"   📁 工作目录: ./{rel_workspace.as_posix()}")
        print(f"   📋 示例目录: ./{rel_examples.as_posix()}\n")
    except ValueError:
        print(f"   📁 工作目录: {workspace.as_posix()}")
        print(f"   📋 示例目录: {examples_dir.as_posix()}\n")
    
    # 初始化 AvatarMain（使用真实组件）
    print("🚀 初始化 Avatar Runtime...")
    
    # Mock LLM（演示不需要真实 LLM）
    mock_llm = MockLLMClient()
    
    # 创建 AvatarMain 实例
    avatar = AvatarMain(
        base_path=workspace,
        llm_client=mock_llm,
        dry_run=False
    )
    
    print("   ✅ Runtime 初始化完成")
    print(f"   📦 已加载 1 个技能\n")
    
    # 运行所有示例
    example_files = [
        ("success.json", "✨ 成功案例"),
        ("missing_param.json", "❌ 失败案例1：缺少参数"),
        ("policy_block.json", "🚫 失败案例2：策略阻断")
    ]
    
    results = []
    
    for json_file, title in example_files:
        print(f"\n{'='*80}")
        print(f"{title}")
        print(f"{'='*80}\n")
        
        json_path = examples_dir / json_file
        if not json_path.exists():
            print(f"⚠️  文件不存在: {json_file}")
            continue
        
        try:
            # 1. 加载任务定义
            with open(json_path, 'r', encoding='utf-8') as f:
                task_def = json.load(f)
            
            print(f"📋 描述: {task_def['description']}")
            print(f"🎯 预期结果: {task_def.get('expected_result', 'unknown')}")
            print(f"📝 场景: {task_def.get('scenario', 'N/A')}\n")
            
            # 2. 转换为 Task 对象
            task = load_task_from_json(json_path, workspace)
            
            # 3. 执行任务（使用真实的 run_task）
            print("▶️  开始执行...\n")
            
            try:
                # Pacing
                result_task = await avatar.run_task(task, step_interval=step_interval)
                
                # 4. 输出结果
                print("\n📊 执行结果:")
                for i, step in enumerate(result_task.steps, 1):
                    status_emoji = {
                        StepStatus.SUCCESS: "✅",
                        StepStatus.FAILED: "❌",
                        StepStatus.SKIPPED: "⏭️",
                        StepStatus.PENDING: "⏳",
                        StepStatus.RUNNING: "▶️"
                    }.get(step.status, "❓")
                    
                    print(f"  [{i}] {status_emoji} {step.skill_name} - {step.status.name}")
                    
                    if step.result:
                        if step.result.output:
                            # 只显示关键字段
                            key_fields = ['path', 'output_path', 'bytes_written', 'files_concatenated']
                            filtered = {k: v for k, v in step.result.output.items() if k in key_fields and v is not None}
                            if filtered:
                                for k, v in filtered.items():
                                    print(f"      {k}: {v}")
                        
                        if step.result.error:
                            print(f"      ❌ 错误: {step.result.error}")
                
                # 判断整体成功/失败
                expected_result = task_def.get('expected_result', 'success')
                
                if expected_result == 'failure':
                    # 预期失败：检查是否真的失败了
                    actual_failed = result_task.status in [TaskStatus.FAILED, TaskStatus.PARTIAL_SUCCESS]
                    if actual_failed:
                        print(f"\n✅ 结果: 符合预期（任务失败）")
                    else:
                        print(f"\n⚠️  结果: 不符合预期（任务应该失败但成功了）")
                else:
                    # 预期成功
                    if result_task.status == TaskStatus.SUCCESS:
                        print(f"\n✅ 结果: 任务执行成功")
                    else:
                        print(f"\n❌ 结果: 任务执行失败")
            
                # 保存 Artifacts (Trace)
                artifact_dir = workspace / "artifacts"
                artifact_dir.mkdir(exist_ok=True)
                trace_filename = f"trace_{json_file}"
                artifact_path = artifact_dir / trace_filename
                
                with open(artifact_path, "w", encoding="utf-8") as f:
                    # 简单记录任务状态和结果
                    trace_data = {
                        "run_id": str(uuid.uuid4()),
                        "timestamp": time.time(),
                        "task_id": result_task.id,
                        "status": result_task.status.name,
                        "steps": [
                            {
                                "skill": s.skill_name,
                                "status": s.status.name,
                                "error": s.result.error if s.result else None
                            }
                            for s in result_task.steps
                        ]
                    }
                    json.dump(trace_data, f, indent=2)
                
                try:
                    rel_path = artifact_path.relative_to(project_root)
                    print(f"📄 Trace Artifact: ./{rel_path.as_posix()}")
                except ValueError:
                    print(f"📄 Trace Artifact: {artifact_path.name}")

                # 计算最终状态
                if expected_result == 'failure':
                    is_success = result_task.status in [TaskStatus.FAILED, TaskStatus.PARTIAL_SUCCESS]
                else:
                    is_success = result_task.status == TaskStatus.SUCCESS

                results.append({
                    'name': json_file,
                    'success': is_success,
                    'expected': expected_result,
                    'task': result_task
                })
                
            except Exception as e:
                print(f"\n❌ 执行异常: {str(e)}")
                
                # 判断是否符合预期
                expected_result = task_def.get('expected_result', 'success')
                if expected_result == 'failure':
                    print(f"✅ 结果: 符合预期（发生异常）")
                    
                    # 检查错误信息是否符合预期
                    expected_error = task_def.get('expected_error', '')
                    error_str = str(e)
                    if expected_error and expected_error in error_str:
                        print(f"   预期错误信息已匹配: '{expected_error}'")
                else:
                    print(f"❌ 不符合预期（任务应该成功但失败了）")
                    import traceback
                    traceback.print_exc()
                
                # 保存 Artifacts (Failure Trace)
                artifact_dir = workspace / "artifacts"
                artifact_dir.mkdir(exist_ok=True)
                trace_filename = f"failure_{json_file}"
                artifact_path = artifact_dir / trace_filename
                
                with open(artifact_path, "w", encoding="utf-8") as f:
                    failure_data = {
                        "run_id": str(uuid.uuid4()),
                        "timestamp": time.time(),
                        "error": str(e),
                        "expected_result": expected_result
                    }
                    json.dump(failure_data, f, indent=2)
                
                try:
                    rel_path = artifact_path.relative_to(project_root)
                    print(f"📄 Failure Artifact: ./{rel_path.as_posix()}")
                except ValueError:
                    print(f"📄 Failure Artifact: {artifact_path.name}")

                results.append({
                    'name': json_file,
                    'success': False,
                    'expected': expected_result,
                    'error': str(e)
                })
        
        except Exception as e:
            print(f"❌ 加载/执行失败: {str(e)}")
            import traceback
            traceback.print_exc()
    
    # 输出总结
    print(f"\n\n{'='*80}")
    print("📊 执行总结")
    print("="*80 + "\n")
    
    for result in results:
        name = result['name']
        expected = result['expected']
        
        if 'error' in result:
            status_emoji = "✅" if expected == 'failure' else "❌"
            print(f"{status_emoji} {name}: 发生异常 (预期: {expected})")
        else:
            task = result['task']
            actual = "success" if task.status == TaskStatus.SUCCESS else "failure"
            matches = (expected == actual)
            status_emoji = "✅" if matches else "❌"
            print(f"{status_emoji} {name}: {actual} (预期: {expected})")
    
    # 验证生成的文件（仅针对成功案例）
    print(f"\n{'='*80}")
    print("📁 工作空间文件验证")
    print("="*80 + "\n")
    
    created_files = list(workspace.glob("*.txt"))
    if created_files:
        for filepath in sorted(created_files):
            content = filepath.read_text(encoding='utf-8')
            print(f"✅ {filepath.name}")
            print(f"   大小: {len(content)} 字节")
            if len(content) <= 200:
                print(f"   内容: {content}")
            else:
                print(f"   内容预览: {content[:200]}...")
            print()
    else:
        print("⚠️  没有生成文件")
    
    print(f"{'='*80}")
    print("✨ 演示完成!")
    print(f"{'='*80}\n")
    
    if open_workspace:
        # 自动打开工作目录
        try:
            import os
            import subprocess
            import platform
            
            print(f"📂 正在打开工作目录: {workspace.name}...")
            
            if platform.system() == "Windows":
                os.startfile(workspace)
            elif platform.system() == "Darwin":  # macOS
                subprocess.Popen(["open", str(workspace)])
            else:  # Linux
                subprocess.Popen(["xdg-open", str(workspace)])
                
        except Exception as e:
            print(f"⚠️  无法自动打开目录: {e}")
