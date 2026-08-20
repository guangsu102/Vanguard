"""Pytest 配置文件"""
import pytest
import asyncio
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path

# 添加项目根目录到 path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_redis():
    """Mock Redis 客户端"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=False)
    redis.incr = AsyncMock(return_value=1)
    redis.ttl = AsyncMock(return_value=3600)
    redis.expire = AsyncMock(return_value=True)
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock(return_value=1)
    redis.client = MagicMock()
    return redis


@pytest.fixture
def mock_api_client():
    """Mock XBoard API 客户端"""
    api = AsyncMock()
    api.create_trial_user = AsyncMock(return_value={
        "success": True,
        "data": {
            "user_id": 1,
            "email": "test@example.com",
            "expires_at": "2026-05-18 20:00:00"
        }
    })
    api.get_user_info = AsyncMock(return_value={
        "success": True,
        "data": {
            "user_id": 1,
            "traffic_remaining_gb": 50,
            "expiry_date": "2026-05-18"
        }
    })
    api.add_traffic = AsyncMock(return_value={
        "success": True,
        "data": {"added_mb": 512, "new_total_gb": 50.5}
    })
    api.get_subscription_link = AsyncMock(return_value={
        "success": True,
        "data": {
            "clash": "https://xboard.com/api/v1/client/subscribe?token=xxx",
            "v2ray": "https://xboard.com/api/v1/client/subscribe?token=xxx"
        }
    })
    api.get_affiliate_link = AsyncMock(return_value={
        "success": True,
        "data": {
            "aff_link": "https://xboard.com/register?aff=ABC123",
            "invite_code": "ABC123"
        }
    })
    return api


@pytest.fixture
def sample_user():
    """测试用户数据"""
    return {
        "tg_uid": 123456789,
        "username": "test_user",
        "first_name": "Test",
        "last_name": "User"
    }


@pytest.fixture
def sample_config():
    """测试配置"""
    return {
        "app": {
            "log_level": "DEBUG"
        },
        "telegram": {
            "lead_gen_bot_token": "test_token",
            "service_bot_token": "test_token",
            "group_ops_bot_token": "test_token",
            "official_channels": ["@test_channel"]
        },
        "xboard_api": {
            "base_url": "http://localhost:8080/api",
            "api_key": "test_api_key",
            "timeout": 30,
            "retry_times": 3
        },
        "database": {
            "host": "localhost",
            "port": 5432,
            "user": "test",
            "password": "test",
            "database": "test_db"
        },
        "redis": {
            "host": "localhost",
            "port": 6379,
            "password": "",
            "db": 0
        },
        "lead_gen": {
            "anti_ban": {
                "message_interval": 30,
                "max_messages_per_day": 30,
                "max_groups_per_day": 10,
                "typing_delay": [2000, 8000],
                "random_timing": True
            },
        },
        "service": {
            "checkin": {
                "enabled": True,
                "min_traffic_mb": 100,
                "max_traffic_mb": 1024,
                "cooldown_hours": 24
            },
            "abandoned_cart": {
                "enabled": True,
                "unpaid_timeout_minutes": 30,
                "discount_percent": 20,
                "coupon_validity_hours": 2
            },
            "poster": {
                "width": 800,
                "height": 1200,
                "qr_size": 200,
                "output_dir": "./assets/posters"
            }
        },
        "group_ops": {
            "competitor_keywords": [
                "机场",
                "t\\.me/\\w+",
                "\\.vip"
            ],
            "warning": {
                "max_warnings": 3,
                "ban_duration_hours": 0
            },
            "node_report": {
                "enabled": True,
                "schedule": "20:30"
            }
        },
        "monitoring": {
            "admin_chat_id": 123456,
            "node_report_chat_id": 654321
        }
    }
