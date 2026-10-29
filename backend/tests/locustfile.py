"""
Locust 压力测试脚本
模拟真实用户：登录 → 知识库问答（SSE流式） → 查看会话列表

运行方式:
  cd backend
  locust -f tests/locustfile.py --host=http://localhost:8000

然后打开浏览器 http://localhost:8089 ，设置用户数开始测试

命令行模式（无Web界面）:
  locust -f tests/locustfile.py --host=http://localhost:8000 \
    --headless --users=10 --spawn-rate=2 --run-time=60s
"""
import os
import time
import random
import json
import httpx
from locust import HttpUser, task, between, events


# ── 电商问答问题池（基于已上传知识库文档） ──
QUESTION_POOL = [
    # iPhone 相关问题
    "iPhone 15 Pro Max的电池容量是多少",
    "iPhone 15和iPhone 15 Pro有什么区别",
    "iPhone 15 Pro支持多少倍光学变焦",
    "iPhone 15系列有哪些颜色可选",
    "iPhone 15 Pro Max价格是多少",
    "iPhone 15的防水等级是什么",
    "iPhone 15 Pro的芯片是什么型号",
    "MagSafe无线充电功率是多少瓦",
    # 华为相关问题
    "华为Mate 60 Pro的电池容量是多少",
    "Mate 60 Pro支持多少瓦快充",
    "华为Mate 60系列有什么颜色",
    "Mate 60 Pro和Pro+有什么区别",
    "华为Mate 60 Pro的摄像头参数",
    "华为手机的保修期是多长时间",
    # 蓝牙耳机问题
    "AirPods Pro第二代价格是多少",
    "华为FreeBuds Pro 3的降噪效果怎么样",
    "推荐一款性价比高的蓝牙耳机",
    "索尼WF-1000XM5续航多久",
    "AirPods Pro和FreeBuds Pro 3哪个好",
    "小米Buds 4 Pro多少钱",
    # 洗衣机问题
    "推荐一款10公斤的滚筒洗衣机",
    "海尔精华洗洗衣机价格多少",
    "小天鹅水魔方有什么特点",
    "洗烘一体机值得买吗",
    "滚筒洗衣机选购要注意什么",
    "西门子洗烘一体机价格",
    "美的洗衣机保修多久",
    # 综合问题
    "推荐一款适合长辈的手机",
    "2000元以内有什么好的蓝牙耳机",
    "iPhone和华为哪个更值得买",
]


class RAGUser(HttpUser):
    """RAG知识库问答普通用户"""

    # 请求间隔：模拟用户在2-10秒内思考后提问
    wait_time = between(2, 10)

    def on_start(self):
        """登录——失败重试3次"""
        idx = random.randint(1, 20)
        username = f"testuser{idx}"

        for attempt in range(3):
            try:
                resp = self.client.post("/api/v1/auth/login", json={
                    "username": username, "password": os.environ["PERF_PASSWORD"],
                })
                if resp.status_code == 200 and "access_token" in resp.json():
                    self.token = resp.json()["access_token"]
                    self.auth_headers = {"Authorization": f"Bearer {self.token}"}
                    return
            except Exception:
                pass
            time.sleep(1)

        # Final fallback
        try:
            resp = self.client.post("/api/v1/auth/login", json={
                "username": "admin", "password": os.environ["PERF_PASSWORD"],
            })
            self.token = resp.json().get("access_token", "")
            self.auth_headers = {"Authorization": f"Bearer {self.token}"}
        except Exception:
            self.token = ""
            self.auth_headers = {}

    @task(6)
    def ask_question(self):
        """RAG问答 — 核心业务场景（权重6）"""
        question = random.choice(QUESTION_POOL)

        # 用 httpx 处理 SSE 流式响应
        start_time = time.time()
        try:
            with httpx.Client() as httpx_client:
                with httpx_client.stream(
                    "POST",
                    f"{self.client.base_url}/api/v1/chat/stream",
                    headers={
                        **self.auth_headers,
                        "Content-Type": "application/json",
                    },
                    json={"message": question, "stream": True},
                    timeout=30.0,
                ) as response:
                    if response.status_code != 200:
                        total_time = (time.time() - start_time) * 1000
                        events.request.fire(
                            request_type="POST",
                            name="/api/v1/chat/stream",
                            response_time=total_time,
                            response_length=0,
                            exception=Exception(f"HTTP {response.status_code}"),
                        )
                        return

                    # 读取SSE流，记录首字时间
                    first_token_time = None
                    total_content = ""
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                if data.get("type") == "text_delta":
                                    if first_token_time is None:
                                        first_token_time = time.time()
                                    total_content += data.get("content", "")
                            except json.JSONDecodeError:
                                pass

                    total_time = (time.time() - start_time) * 1000
                    ttft = ((first_token_time or start_time) - start_time) * 1000

                    events.request.fire(
                        request_type="POST",
                        name="/api/v1/chat/stream (SSE)",
                        response_time=total_time,
                        response_length=len(total_content),
                    )

                    # 将首字延迟记录为自定义指标
                    if first_token_time:
                        events.request.fire(
                            request_type="METRIC",
                            name="Time-To-First-Token",
                            response_time=ttft,
                            response_length=0,
                        )

        except Exception as e:
            total_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type="POST",
                name="/api/v1/chat/stream",
                response_time=total_time,
                response_length=0,
                exception=e,
            )

    @task(2)
    def list_conversations(self):
        """查看会话列表（权重2）"""
        self.client.get(
            "/api/v1/conversations",
            headers=self.auth_headers,
            name="/api/v1/conversations",
        )

    @task(1)
    def get_profile(self):
        """获取用户信息（权重1）"""
        self.client.get(
            "/api/v1/auth/me",
            headers=self.auth_headers,
            name="/api/v1/auth/me",
        )


class RAGAdminUser(HttpUser):
    """RAG知识库问答管理员"""

    wait_time = between(3, 8)

    def on_start(self):
        """管理员登录"""
        resp = self.client.post("/api/v1/auth/login", json={
            "username": "admin", "password": os.environ["PERF_PASSWORD"],
        })
        self.token = resp.json()["access_token"]
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def ask_question(self):
        """管理员也参与问答（权重3）"""
        question = random.choice(QUESTION_POOL)
        start_time = time.time()
        try:
            with httpx.Client() as httpx_client:
                with httpx_client.stream(
                    "POST",
                    f"{self.client.base_url}/api/v1/chat/stream",
                    headers={**self.auth_headers, "Content-Type": "application/json"},
                    json={"message": question, "stream": True},
                    timeout=30.0,
                ) as response:
                    total_content = ""
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                if data.get("type") == "text_delta":
                                    total_content += data.get("content", "")
                            except json.JSONDecodeError:
                                pass
                    total_time = (time.time() - start_time) * 1000
                    events.request.fire(
                        request_type="POST",
                        name="/api/v1/chat/stream (admin)",
                        response_time=total_time,
                        response_length=len(total_content),
                    )
        except Exception as e:
            total_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type="POST",
                name="/api/v1/chat/stream (admin)",
                response_time=total_time,
                response_length=0,
                exception=e,
            )

    @task(2)
    def view_dashboard(self):
        """查看仪表盘（权重2）"""
        self.client.get(
            "/api/v1/admin/dashboard",
            headers=self.auth_headers,
            name="/api/v1/admin/dashboard",
        )

    @task(1)
    def list_documents(self):
        """查看文档列表（权重1）"""
        self.client.get(
            "/api/v1/knowledge/documents",
            headers=self.auth_headers,
            name="/api/v1/knowledge/documents",
        )
