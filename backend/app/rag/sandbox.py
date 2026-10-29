"""代码执行沙箱（批次C·11b）——受限为默认的"python"工具

叙事定位（用户 2026-10 拍板）：受限 Agent 哲学为默认、沙箱为可选工具——
AI 应用开发岗位的高频考点。安全设计四层：
1. ast 白名单预审：只允许纯计算类语句（禁 import 危险模块/黑名单调用/
   类定义/输入输出重定向）
2. subprocess 新进程隔离（python -I：不读用户 site-packages 防投毒）
3. 超时（默认 5 秒）
4. 输出截断（1000 字）

用途：员工问"帮我把这几笔报销求个平均""算一下 3 天年假+2 天调休一共
几天"类问题——LLM 决策生成代码，沙箱执行返回结果（程序算，不靠心算）。
"""
import ast
import subprocess
import sys

from loguru import logger

# ast 预审允许的 import 模块（纯计算类）。
# 批次C.5：random 模块入白名单（抽签/随机抽样/临时密码等企业真实场景）——
# 公开 API 全部为纯随机数计算无 IO 能力；内部危险属性（_os/sys/modules）
# 由 _FORBIDDEN_ATTRS 拦截，泄露面与 math/statistics 同级
_ALLOWED_MODULES = {"math", "statistics", "random"}
# 批次C 终审 P1 修复：白名单模块的公开 API 名单——模块对象内部 import
# 的模块（statistics.sys/statistics.random 等）会泄露整个模块图，
# 质检实测 statistics.sys.modules['os'].system(...) 八条变体全击穿。
# 属性链的根是白名单模块名时，属性必须在对应 API 名单内
_MODULE_API: dict[str, set] = {
    "math": {
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "sinh", "cosh", "tanh", "asinh", "acosh", "atanh", "exp", "expm1",
        "log", "log1p", "log2", "log10", "pow", "hypot", "degrees", "radians",
        "pi", "e", "tau", "inf", "nan", "floor", "ceil", "trunc", "fabs",
        "factorial", "gcd", "comb", "perm", "isclose", "fsum", "prod", "lcm",
        "isfinite", "isinf", "isnan", "fmod", "remainder", "ldexp", "frexp",
        "modf", "copysign", "nextafter", "ulp", "dist", "erf", "erfc", "gamma",
        "lgamma", "isqrt", "cbrt", "exp2", "sumprod",  # 终审v3 补遗漏官方 API
        "fma",  # 终审v4 补 Python 3.13 新增 API
    },
    "statistics": {
        "mean", "median", "median_low", "median_high", "median_grouped",
        "mode", "multimode", "stdev", "variance", "pstdev", "pvariance",
        "harmonic_mean", "geometric_mean", "fmean", "quantiles", "correlation",
        "covariance", "linear_regression", "NormalDist",
        "fsum", "StatisticsError",  # 终审v3 补遗漏官方 API
        "LinearRegression", "kde", "kde_random",  # 终审v4 补 3.13 新增 API
    },
    "random": {
        # 批次C.5：随机数公开 API——全部纯计算，无 IO 能力
        "random", "uniform", "randint", "randrange", "choice", "choices",
        "sample", "shuffle", "seed", "getrandbits", "gauss", "normalvariate",
        "SystemRandom",
    },
}
# 调用黑名单（即便通过 import 白名单也不允许直接调用的名字）
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "input", "open",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "dir", "type",  # 批次C 终审：内省类死端防御（当前死端，封住更稳）
    # 批次C.5 P3：help/license/credits 等站点内置信息泄露面
    # （help('os') 输出 os 模块文档、license 输出 Python 许可证——
    # 纯信息泄露不可执行，但无计算合法场景，一并封死）
    "help", "license", "credits", "copyright", "quit", "exit",
}
# 批次C 终审v3 P1 修复：危险属性名黑名单——根追踪只认直接名字，
# 赋值别名（s = statistics; s.sys...）/容器下标/BoolOp 等间接根形态
# 全绕过 API 名单校验（质检实测 10 变体、4 条执行级证实+1 条落盘）。
# 属性名级封死与根形态无关：纯计算代码里这些属性名零合法用途，
# 直接拒绝即封死所有形态（与 dunder 黑名单同层哲学）
_FORBIDDEN_ATTRS = {
    "sys", "modules", "os", "posix", "nt", "subprocess", "builtins",
    "_os", "_random", "mro", "globals", "loader", "spec", "file",
    "get_data", "load_module", "find_module", "system", "popen",
    "_urandom",  # 批次C.5 质检备忘：间接根 r._urandom 实测可达（仅熵读取，
    # 与放行的 SystemRandom 能力等价）——封上与黑名单哲学一致
    # 批次C 最终核验追加：str.format/format_map 盲区可读任意对象属性
    # （质检实测 {0.__globals__[sys]...}.format(statistics.mean) 输出
    # os.environ 含真实密钥）——无调用能力但信息会经沙箱输出流回答案，
    # 纯计算代码无 format 合法场景，直接封死
    "format", "format_map",
    # 批次C.5：注意 "random" 已从本名单移除——它在 random 模块的公开
    # API 里（random.random() 合法函数），继续留在黑名单会误拒；
    # statistics.random 泄露由 API 名单第一跳拦截（statistics 名单无
    # random），间接根形态摸到的 random 模块对象无危险能力（其 _os
    # 等属性仍被本名单拦截）
}
# 允许的语句/表达式节点类型（其余一律拒绝——宁缺勿滥）
_ALLOWED_NODES = {
    ast.Expression, ast.Expr, ast.Assign, ast.AugAssign, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.Call, ast.Name, ast.Constant, ast.List,
    ast.Tuple, ast.Dict, ast.Subscript, ast.Attribute, ast.For,
    ast.If, ast.While, ast.Break, ast.Continue, ast.Module,
    ast.Store, ast.Load, ast.keyword, ast.alias, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub,
    ast.UAdd, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.comprehension, ast.ListComp, ast.GeneratorExp,
    ast.DictComp, ast.SetComp,  # 批次C.5 P3：推导式口径对齐（与 ListComp 同待遇）
    # Import/ImportFrom 本体在此（模块级白名单在 _check_import_ok 二关拦截）
    ast.Import, ast.ImportFrom,
    # 函数定义/Lambda/类定义一律拒绝（宁缺勿滥——纯计算类代码用
    # 赋值+for 循环足够，函数体扩大攻击面且沙箱场景无必要）
}


def _attr_root(node: ast.Attribute) -> str | None:
    """属性链的根名字（statistics.sys.modules → "statistics"）"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else None


def _check_import_ok(node: ast.AST) -> bool:
    """import/import-from 只允许白名单模块（import math / from math import sqrt）。

    批次C 终审 P1 修复：
    - import 白名单模块不允许 as 别名（别名会让属性链根追踪失效）
    - from 白名单模块 import 的名字必须在模块公开 API 名单内
      （from statistics import sys → 拒）
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            mod = alias.name.split(".")[0]
            if mod not in _ALLOWED_MODULES or alias.asname is not None:
                return False
        return True
    if isinstance(node, ast.ImportFrom):
        if not node.module:
            return False
        mod = node.module.split(".")[0]
        if mod not in _ALLOWED_MODULES:
            return False
        return all(
            alias.name in _MODULE_API.get(mod, set()) for alias in node.names
        )
    return False


def _walk_check(node: ast.AST) -> bool:
    """递归检查节点树：所有节点必须在白名单类型内；调用名不得在黑名单。

    批次C 质检 P1 修复：ast.Attribute 全量放行+只拦 Name 节点，鸭子类型
    逃逸链（().__class__.__base__.__subclasses__() → os._wrap_close...
    __globals__['system']）可绕出沙箱——所有 dunder（双下划线）属性
    一律拒绝（纯计算类代码没有合法 dunder 访问场景）；Subscript 的
    字符串键同样禁 dunder（封死 __globals__/__dict__ 下标路径）。
    """
    if type(node) not in _ALLOWED_NODES:
        return False
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _check_import_ok(node)
    if isinstance(node, ast.Call):
        # 调用名黑名单（裸名调用）；属性调用（math.sqrt）由公开 API 名单
        # + 危险属性名黑名单双层拦截（直接名字根走 API 名单，间接根形态
        # 靠 _FORBIDDEN_ATTRS 兜底）
        func = node.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAMES:
            return False
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
            and (node.id in _FORBIDDEN_NAMES or node.id.startswith("__")):
        # dunder 名字（__loader__/__spec__ 等 builtin）同样拦截——质检补审
        # 实测 __loader__.load_module('os') 在 python -I 下真实返回 os 模块，
        # 名字级 dunder 与属性/下标级同风险；纯计算代码无合法 dunder 名字
        return False
    if isinstance(node, ast.Attribute) and (
        node.attr.startswith("__") or node.attr in _FORBIDDEN_NAMES
        or node.attr in _FORBIDDEN_ATTRS
    ):
        # dunder 属性封死鸭子类型逃逸入口（__class__/__base__/__globals__...）；
        # _FORBIDDEN_ATTRS 封死模块图危险属性（sys/modules/os/random...）
        # ——与链根形态无关（别名/下标/BoolOp 根全部在此拦截）
        return False
    if isinstance(node, ast.Attribute):
        # 批次C 终审 P1：白名单模块命名空间泄露拦截——属性链根是
        # math/statistics 时，属性必须在公开 API 名单（statistics.sys 等
        # 模块内部导入暴露的整图路径在此拒绝）。
        # 终审v4 P3 修复：API 名单只在"第一跳"（node.value 是 Name 的
        # 直接形式 math.XXX）校验——深层属性（math.pi.real 的 .real 是
        # 数值类型的合法属性）不查 API 名单，由 _FORBIDDEN_ATTRS 拦
        # 危险名兜底（statistics.sys 第一跳即被双重拦截）
        root = _attr_root(node)
        if (root in _MODULE_API and isinstance(node.value, ast.Name)
                and node.attr not in _MODULE_API[root]):
            return False
    if isinstance(node, ast.Subscript):
        # 下标键为字符串时同样禁 dunder（封死 d['__globals__'] 路径）
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                and (key.value.startswith("__") or key.value in _FORBIDDEN_NAMES):
            return False
    return all(_walk_check(child) for child in ast.iter_child_nodes(node))


def is_safe_code(code: str) -> bool:
    """ast 白名单预审；解析失败/含未允许结构返回 False（宁缺勿滥）"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return _walk_check(tree)


def execute_sandbox(code: str, timeout: float = 5.0) -> str:
    """沙箱执行 Python 代码，返回 stdout（截断 1000 字）。

    预审不通过/超时/异常均返回错误文案（不抛异常——工具失败不炸主链路）。
    """
    if not is_safe_code(code):
        return "代码未通过安全预审：仅支持纯计算类代码（math/statistics/random 模块）"
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0:
            err = (result.stderr or "").strip().splitlines()
            return f"执行出错：{err[-1][:200] if err else '未知错误'}"
        if not out:
            return "（代码执行完成，无输出）"
        return out if len(out) <= 1000 else out[:1000] + "…（输出已截断）"
    except subprocess.TimeoutExpired:
        return f"执行超时（>{timeout:g} 秒），已终止"
    except Exception as e:
        logger.warning("沙箱执行异常: {}", e)
        return f"沙箱执行失败：{type(e).__name__}"
