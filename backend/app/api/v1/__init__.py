from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.conversation import router as conversation_router
from app.api.v1.feedback import router as feedback_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.admin import router as admin_router
from app.api.v1.openai_compat import router as openai_router
from app.api.v1.dify_compat import router as dify_router
from app.api.v1.wecom_channel import router as wecom_channel_router

routers = [auth_router, chat_router, conversation_router, feedback_router, knowledge_router, admin_router, openai_router, dify_router, wecom_channel_router]
