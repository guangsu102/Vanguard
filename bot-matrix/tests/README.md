# XBoard Bot Matrix - 测试套件

## 测试结构

```
tests/
├── conftest.py           # pytest 配置
├── unit/
│   ├── test_database.py
│   ├── test_cache.py
│   ├── test_poster.py
│   └── test_content.py
├── integration/
│   ├── test_api_client.py
│   └── test_bots.py
└── fixtures/
    └── __init__.py
```

## 运行测试

```bash
# 运行所有测试
pytest

# 运行单元测试
pytest tests/unit/

# 运行集成测试
pytest tests/integration/

# 生成覆盖率报告
pytest --cov=src --cov-report=html

# 详细输出
pytest -v -s
```

## Mock Fixtures

测试中使用的主要 fixtures：

- `mock_redis` - Mock Redis 客户端
- `mock_api_client` - Mock XBoard API
- `sample_user` - 测试用户数据
