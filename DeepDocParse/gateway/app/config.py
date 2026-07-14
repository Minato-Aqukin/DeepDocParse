"""配置与模型注册表加载。

gateway 不 import 任何模型代码——只认 models.yaml 里的 endpoint。
"""
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    service_token: str = "change-me"
    redis_url: str = "redis://localhost:6379/0"
    models_config: str = "models.yaml"
    parse_queue_max: int = 200
    vqa_max_concurrency: int = 8
    result_ttl: int = 86400
    # ARQ 轮询 mineru 的退避参数（秒）
    poll_initial_delay: float = 1.0
    poll_max_delay: float = 10.0
    poll_timeout: float = 1800.0


class ModelEntry(BaseModel):
    endpoint: str
    default: bool = False
    # 引擎级默认透传选项（如 mineru 的 backend=pipeline|vlm），请求方 options 可覆盖。
    # 放注册表而非代码：dev/prod 换后端 = 改一行配置（铁律 3）
    options: dict = {}


class Registry(BaseModel):
    """models.yaml 的内存形态。"""

    vqa_models: dict[str, ModelEntry] = {}
    parse_engines: dict[str, ModelEntry] = {}
    embedding_models: dict[str, ModelEntry] = {}  # v2 (M4) 启用

    def default_of(self, section: dict[str, ModelEntry]) -> tuple[str, ModelEntry]:
        for name, entry in section.items():
            if entry.default:
                return name, entry
        # 没标 default 就取第一个
        name = next(iter(section))
        return name, section[name]


def load_registry(path: str | Path) -> Registry:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Registry(**{k: v for k, v in data.items() if v})


settings = Settings()
