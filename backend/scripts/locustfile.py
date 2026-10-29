"""locust 压测脚本（批次5.5 压测演练）：模拟员工并发提问

用法（backend 目录）：
    locust -f scripts/locustfile.py --host http://localhost:8010 \
        --headless -u 20 -r 5 -t 60s --csv=/tmp/locust_result

-u 20 = 最多 20 个并发用户；-r 5 = 每秒上 5 个；-t 60s = 持续 60 秒。
每个虚拟用户：登录 → 循环随机提问（SSE 流式收完 done 才算完成）。
配合 Jaeger：压测期间所有请求的 span 自动上报，可定位瓶颈阶段。
"""
import os
import itertools
import random

from locust import HttpUser, between, task

# 与演示库语料匹配的典型问题池（覆盖四检索腿：向量/BM25/图谱/规则）
QUESTIONS = [
    "年假怎么申请？",
    "报销 8000 元谁审批？",
    "考勤管理制度有哪些规定？",
    "郑浩和王建国是什么关系？",
    "研发团队有哪些人？",
    "差旅住宿标准是多少？",
    "办公电脑坏了找谁？",
    "请假 5 天谁审批？",
    "入职需要准备哪些材料？",
    "公司都有哪些制度？",
]

# 压测账号池（每虚拟用户固定一个，防顶号机制互相踢下线——
# 压测演示发现：同账号并发登录时旧 token 全失效"账号已在其他设备登录"）
PERF_ACCOUNTS = [f"perf{i:02d}" for i in range(1, 21)]
PERF_PASSWORD = os.environ["PERF_PASSWORD"]
_user_counter = itertools.count()


def _next_account() -> str:
    """账号池取号：超过 20 个虚拟用户时循环复用（复用会触发顶号——
    压测时 -u 请勿超过 20）。计数器为进程级：单进程（--headless）正常；
    分布式多 worker 会跨进程撞号，届时改为按 worker_id 取模分配"""
    return PERF_ACCOUNTS[next(_user_counter) % len(PERF_ACCOUNTS)]


class KnowledgeBaseUser(HttpUser):
    """模拟一名登录员工持续提问（每虚拟用户独立账号）"""

    wait_time = between(2, 5)  # 每问间隔 2~5 秒（真实用户节奏）

    def on_start(self):
        username = _next_account()
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": PERF_PASSWORD},
            catch_response=True,
        )
        if resp.status_code == 200:
            self.token = resp.json()["access_token"]
        else:
            self.token = None
            resp.failure(f"登录失败 HTTP {resp.status_code}")

    @task
    def ask_question(self):
        if not self.token:
            return
        question = random.choice(QUESTIONS)
        headers = {"Authorization": f"Bearer {self.token}"}
        # SSE 流式问答：以 name="chat" 记录完整问答耗时（收到 done 为止）
        with self.client.post(
            "/api/v1/chat/stream",
            json={"message": question, "conversation_id": None},
            headers=headers,
            stream=True,
            catch_response=True,
            name="chat",
            timeout=120,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            done = False
            for line in resp.iter_lines():
                if line and b'"type": "done"' in line:
                    done = True
                    break
            if not done:
                resp.failure("未收到 done 事件（生成可能超时/失败）")
