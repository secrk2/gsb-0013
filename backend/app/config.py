import os


class Settings:
    APP_NAME = "报关通 · 报关行自研报关作业与合规风控平台"
    API_PREFIX = "/api"
    # 容器内默认写 /data，方便挂载卷；本地直接运行时回退到 backend/data
    DATA_DIR = os.environ.get("BGT_DATA_DIR", "/data")
    DB_PATH = os.environ.get("BGT_DB_PATH") or os.path.join(DATA_DIR, "baoguantong.db")
    TOKEN_TTL_SECONDS = 60 * 60 * 12
    SEED_ON_STARTUP = os.environ.get("BGT_SEED", "1") != "0"


settings = Settings()
