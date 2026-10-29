"""批次C·11b 代码执行沙箱测试：白名单预审 / 执行 / 危险代码拦截 / 超时"""
import pytest

from app.rag.sandbox import is_safe_code, execute_sandbox


class TestSafeCode:
    def test_simple_calculation_allowed(self):
        assert is_safe_code("print(sum([1, 2, 3]) / 3)")

    def test_math_import_allowed(self):
        assert is_safe_code("import math\nprint(math.sqrt(16))")
        assert is_safe_code("from statistics import mean\nprint(mean([1,2,3]))")

    def test_dangerous_imports_rejected(self):
        assert not is_safe_code("import os\nprint(os.listdir())")
        assert not is_safe_code("import subprocess\nsubprocess.run('dir')")
        assert not is_safe_code("import socket")

    def test_forbidden_calls_rejected(self):
        assert not is_safe_code("print(eval('1+1'))")
        assert not is_safe_code("print(open('secret.txt').read())")
        assert not is_safe_code("print(__import__('os').listdir())")

    def test_class_and_lambda_rejected(self):
        assert not is_safe_code("class X:\n    pass")
        assert not is_safe_code("f = lambda: os.system('x')")

    def test_loop_and_if_allowed(self):
        assert is_safe_code(
            "total = 0\nfor i in range(5):\n    if i % 2 == 0:\n        total += i\nprint(total)"
        )

    def test_dict_and_set_comprehension_allowed(self):
        """批次C.5 质检备忘回归：DictComp/SetComp 与 ListComp 同口径放行"""
        assert is_safe_code(
            "print({x: x * x for x in range(5)})"
        )
        assert is_safe_code(
            "print({x * 2 for x in range(5)})"
        )
        assert is_safe_code(
            "print([x * 2 for x in range(5)])"
        )

    def test_syntax_error_rejected(self):
        assert not is_safe_code("print(")

    def test_dunder_escape_chains_rejected(self):
        """批次C 质检 P1 回归钉住：鸭子类型逃逸链（质检实测可绕出沙箱的
        原链）必须被 dunder 属性拦截拒绝"""
        assert not is_safe_code("().__class__")
        assert not is_safe_code("().__class__.__base__")
        assert not is_safe_code("().__class__.__base__.__subclasses__()")
        assert not is_safe_code("x = {}; x['__globals__']")
        assert not is_safe_code("print((1).__class__)")
        assert not is_safe_code("import math; math.__dict__")
        # 质检补审实测的第二条逃逸链：builtin dunder 名字（Name 节点，
        # 非属性非下标）——__loader__.load_module('os') 在 python -I 下
        # 真实返回 os 模块
        assert not is_safe_code("__loader__.load_module('os')")
        assert not is_safe_code("print(__loader__)")
        assert not is_safe_code("print(__spec__)")
        assert not is_safe_code("print(__builtins__)")

    def test_whitelist_module_namespace_leak_rejected(self):
        """批次C 终审 P1 回归钉住：白名单模块命名空间泄露（质检实测
        statistics.sys.modules['os'].system 八条变体全击穿）——模块属性
        链根追踪 + 公开 API 名单全拒"""
        assert not is_safe_code("import statistics\nstatistics.sys.modules['os']")
        assert not is_safe_code(
            "import statistics\nstatistics.sys.modules['os'].system('echo pwn')"
        )
        assert not is_safe_code("import statistics\nstatistics.random._os")
        assert not is_safe_code("from statistics import sys")
        assert not is_safe_code(
            "import statistics\nstatistics.sys.modules.get('os')"
        )
        assert not is_safe_code("import math\nmath.sys.modules['os']")
        # import 白名单模块不允许 as 别名（属性链根追踪失效）
        assert not is_safe_code("import statistics as st\nst.sys")
        # 公开 API 正常放行
        assert is_safe_code("import statistics\nprint(statistics.mean([1,2,3]))")
        assert is_safe_code("import math\nprint(math.sqrt(16))")
        assert is_safe_code("from statistics import mean\nprint(mean([1,2,3]))")

    def test_indirect_root_alias_escape_rejected(self):
        """批次C 终审v3 P1 回归钉住：间接根形态（赋值别名/容器下标/
        BoolOp）绕过根追踪的逃逸链——属性名黑名单与根形态无关全拒，
        并做执行级断言（execute_sandbox 拒绝执行不落盘）"""
        from app.rag.sandbox import execute_sandbox

        variants = [
            "import statistics\ns = statistics\ns.sys.modules['os'].system('echo PWNED')",
            "import statistics\na = statistics\nb = a\nb.sys.modules['os']",
            "import statistics\n[statistics][0].sys.modules['os']",
            "import statistics\n(statistics,)[0].sys.modules['os']",
            "import statistics\nsorted([statistics])[0].sys.modules['os']",
            "import statistics\n(0 or statistics).sys.modules['os']",
            "import statistics\ns = statistics\ns.random._os",
            "import statistics\ns = statistics\n[s][0].sys",
            "import math\nx = math\nx.sys",
            # 终审v4 补钉：质检实测 REJECT 的 BoolOp 容器组合链（B3c）
            "import statistics\n([] or [statistics])[0].sys.modules['os']",
        ]
        for code in variants:
            assert not is_safe_code(code), f"逃逸变体未被拒绝: {code[:60]}"
        # 执行级断言：代表链被拒后不执行（无 PWNED 输出、无文件落盘）
        out = execute_sandbox(
            "import statistics\ns = statistics\ns.sys.modules['os'].system('echo PWNED')"
        )
        assert "未通过安全预审" in out
        assert "PWNED" not in out
        # 批次C.5 P3：写文件变体被拒后确认不落盘（质检实测曾真实落盘）
        from pathlib import Path

        probe = Path("sandbox_pwn_probe.txt")
        if probe.exists():
            probe.unlink()
        write_variant = (
            "import statistics\ns = statistics\n"
            "s.sys.modules['os'].system('echo hacked > sandbox_pwn_probe.txt')"
        )
        assert not is_safe_code(write_variant)
        execute_sandbox(write_variant)
        assert not probe.exists(), "写文件变体被拒后不得落盘"


class TestExecuteSandbox:
    def test_executes_simple_code(self):
        out = execute_sandbox("print(sum([1, 2, 3]) / 3)")
        assert "2.0" in out

    def test_runtime_error_reported(self):
        out = execute_sandbox("print(1 / 0)")
        assert "执行出错" in out

    def test_unsafe_code_rejected_before_exec(self):
        out = execute_sandbox("import os\nprint(os.getcwd())")
        assert "未通过安全预审" in out

    def test_no_output_message(self):
        out = execute_sandbox("x = 1 + 1")
        assert "无输出" in out

    def test_timeout_terminates(self):
        out = execute_sandbox(
            "total = 0\nwhile True:\n    total += 1",
            timeout=1.0,
        )
        assert "超时" in out

    def test_output_truncated(self):
        out = execute_sandbox("print('长' * 5000)")
        assert out.endswith("（输出已截断）")

    def test_random_module_restored_and_leaks_still_blocked(self):
        """批次C.5 回归钉住：random 模块入白名单（抽签/抽样场景恢复），
        泄露路径仍封（statistics.random 由 API 第一跳拦、random._os 由
        属性黑名单拦）"""
        from app.rag.sandbox import execute_sandbox

        # 合法随机数功能恢复
        assert is_safe_code("import random\nprint(random.randint(1, 10))")
        assert is_safe_code("import random\nprint(random.choice(['a','b']))")
        assert is_safe_code("import random\nprint(random.sample(range(50), 3))")
        assert is_safe_code("import random\nprint(random.random())")
        # 泄露路径仍封
        assert not is_safe_code("import statistics\nstatistics.random._os")
        assert not is_safe_code("import statistics\ns = statistics\ns.random._os")
        assert not is_safe_code("import random\nrandom._os")
        assert not is_safe_code("import random\nr = random\nr._os")
        # 执行级：随机数真实出结果
        out = execute_sandbox("import random\nprint(random.randint(1, 10))")
        assert "未通过安全预审" not in out

    def test_numeric_attribute_chain_allowed(self):
        """终审v4 P3 修复回归：数值类型深层属性（math.pi.real）不再被
        API 名单逐级校验误拒——名单只在第一跳校验，深层由危险名兜底"""
        assert is_safe_code("import math\nprint(math.pi.real)")
        assert is_safe_code("import math\nx = math.pi\nprint(x.real)")
        assert is_safe_code(
            "import math\nprint(math.pi.as_integer_ratio())"
        )
        # 危险深层属性仍被黑名单拦截（不受第一跳放宽影响）
        assert not is_safe_code("import math\nx = math\nx.sys")
        assert not is_safe_code("import statistics\nstatistics.random._os")
        # 最终核验追加：str.format 盲区（信息泄露通道）封死
        assert not is_safe_code(
            "import statistics\nprint('{0.__globals__}'.format(statistics.mean))"
        )
        assert not is_safe_code(
            "import statistics\n'{0}'.format_map(statistics.__dict__)"
        )

    def test_math_import_executes(self):
        out = execute_sandbox("import math\nprint(math.sqrt(16))")
        assert "4.0" in out
