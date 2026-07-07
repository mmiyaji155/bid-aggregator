"""
設定管理モジュール

環境変数と設定ファイルからの設定読み込みを管理する。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション設定"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # データベース
    database_url: str = Field(
        default="sqlite:///data/bid_aggregator.db",
        description="データベース接続URL",
    )
    db_host: str | None = Field(
        default=None,
        description="PostgreSQL host or Cloud SQL Unix socket path",
    )
    db_port: int | None = Field(default=None)
    db_name: str | None = Field(default=None)
    db_user: str | None = Field(default=None)
    db_password: str | None = Field(default=None)

    # ログ
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="ログレベル",
    )

    # KKJ API設定
    kkj_api_url: str = Field(
        default="http://www.kkj.go.jp/api/",
        description="KKJ APIエンドポイント",
    )
    kkj_request_interval: float = Field(
        default=1.0,
        description="リクエスト間隔（秒）",
    )
    kkj_request_timeout: float = Field(
        default=30.0,
        description="リクエストタイムアウト（秒）",
    )

    # 通知設定
    notify_max_items: int = Field(
        default=100,
        description="1回の通知件数上限",
    )
    slack_webhook_url: str | None = Field(
        default=None,
        description="Slack Webhook URL",
    )

    # SMTP設定
    smtp_host: str | None = Field(default=None)
    smtp_port: int = Field(default=587)
    smtp_user: str | None = Field(default=None)
    smtp_password: str | None = Field(default=None)
    smtp_from: str | None = Field(default=None)
    smtp_use_tls: bool = Field(default=True)

    # タイムゾーン
    timezone: str = Field(
        default="Asia/Tokyo",
        description="表示用タイムゾーン",
    )

    @property
    def data_dir(self) -> Path:
        """データディレクトリのパス"""
        return Path("data")

    @property
    def config_dir(self) -> Path:
        """設定ディレクトリのパス"""
        return Path("config")


# グローバル設定インスタンス
settings = Settings()
