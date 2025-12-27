"""
Infrastructure automation tasks - 根任务文件

此文件自动加载各模块的 tasks.py
"""
from invoke import Collection
from pathlib import Path
import importlib.util
import sys
import os
from dotenv import load_dotenv
from invoke import task

# 加载 .env 文件
load_dotenv()

@task
def check_env(c):
    """全局环境变量校验"""
    missing = []
    for var in ["VPS_HOST", "INTERNAL_DOMAIN"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"\n❌ 错误: 缺少全局环境变量: {', '.join(missing)}")
        exit(1)
    print("✅ 全局环境变量校验通过")


# 创建根 namespace
ns = Collection()
ns.add_task(check_env)

# 自动加载 bootstrap 模块的 tasks
bootstrap_dir = Path(__file__).parent / "bootstrap"
for component_dir in sorted(bootstrap_dir.iterdir()):
    if component_dir.is_dir():
        tasks_file = component_dir / "tasks.py"
        if tasks_file.exists():
            # 动态导入模块
            module_name = f"bootstrap.{component_dir.name}.tasks"
            spec = importlib.util.spec_from_file_location(module_name, tasks_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                
                # 添加到 namespace，使用组件名作为前缀
                component_name = component_dir.name.split('.')[-1]  # 去掉编号前缀
                ns.add_collection(Collection.from_module(module), name=component_name)
                print(f"✅ 加载模块: {component_name}")


# Invoke 会自动查找此变量
# 使用方式: 
# uv run invoke vault.setup
# uv run invoke vault.prepare
