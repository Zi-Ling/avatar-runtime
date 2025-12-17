import sys
import asyncio
import argparse
from pathlib import Path

# 将项目根目录添加到 Python 路径，以便正确导入模块
current_dir = Path(__file__).parent
project_root = current_dir.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from runtime.demo_engine import run_demo_suite

def main():
    """演示入口"""
    parser = argparse.ArgumentParser(description="Avatar Runtime Demo Runner")
    parser.add_argument("--open-workspace", action="store_true", help="演示结束后自动打开工作目录")
    args = parser.parse_args()

    # 示例 JSON 所在目录 (avatar/examples/plans)
    current_dir = Path(__file__).parent
    examples_dir = current_dir / "plans"
    
    # 工作空间目录 (avatar/workspace)
    workspace_dir = current_dir.parent / "workspace"
    
    # 获取相对路径用于显示
    try:
        # 假设我们在项目根目录运行 python -m avatar.examples.run
        # 项目根目录是 avatar 的父目录
        project_root = current_dir.parent.parent
        rel_examples = examples_dir.relative_to(project_root)
        rel_workspace = workspace_dir.relative_to(project_root)
    except ValueError:
        rel_examples = examples_dir
        rel_workspace = workspace_dir

    print(f"🚀 启动演示...")
    print(f"   示例目录: ./{rel_examples.as_posix()}")
    print(f"   工作空间: ./{rel_workspace.as_posix()}")
    
    try:
        asyncio.run(run_demo_suite(
            examples_dir=examples_dir,
            workspace_dir=workspace_dir,
            step_interval=1.0,
            open_workspace=args.open_workspace
        ))
    except KeyboardInterrupt:
        print("\n👋 用户停止演示")
    except Exception as e:
        print(f"\n❌ 演示异常: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
