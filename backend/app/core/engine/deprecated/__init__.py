"""已废弃的旧执行路径。

`step_agent.py`（StepAgent）与 `reflection_agent.py`（ReflectionAgent）在 `app/`
里已无生产调用，只剩 3 个测试在用。保留是为了不打断历史用例；新代码不要再引用，
需要单步执行请走 `engine/step_loop.py`。
"""
