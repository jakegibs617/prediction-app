from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: str = "development"
    log_level: str = "INFO"
    secret_key: str = "changeme"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "experimental_prediction_app"
    postgres_user: str = "prediction_app"
    postgres_password: str = ""

    # AI Model
    ai_model_provider: str = "ollama"
    ai_model_name: str = "llama3.2:8b"
    ollama_base_url: str = "http://localhost:11434"
    alpha_vantage_api_key: str = ""
    coingecko_api_key: str = ""
    fred_api_key: str = ""
    news_api_key: str = ""
    sec_edgar_user_agent: str = ""
    noaa_user_agent: str = ""
    app_user_agent: str = "PredictionApp/1.0 jakegibs617@gmail.com"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    groq_api_key: str = ""
    model_context_window_tokens: int = 128000
    model_cost_input_per_m_usd: float = 0.0
    model_cost_output_per_m_usd: float = 0.0
    model_temperature: float = 0.1
    ai_model_provider_cheap: str = "ollama"
    ai_model_name_cheap: str = "llama3.2"

    # Safety limits
    max_agent_tool_calls: int = 20
    max_agent_iterations: int = 5
    max_agent_input_tokens: int = 6000
    max_agent_output_tokens: int = 2000
    max_features_per_prediction: int = 25
    max_evidence_summary_chars: int = 1000
    job_max_runtime_seconds: int = 300

    # Context compression
    context_warning_threshold_pct: float = 0.75
    context_critical_threshold_pct: float = 0.90
    max_features_critical: int = 10

    # Spend caps
    max_spend_per_run_usd: float = 0.50
    max_spend_daily_usd: float = 5.00

    # Hallucination guards
    hallucination_prob_low: float = 0.05
    hallucination_prob_high: float = 0.95
    max_output_validation_retries: int = 2

    # Alerting
    alert_min_probability: float = 0.85
    alert_max_horizon_hours: int = 72
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Logging
    log_file_path: str = "./logs/app.log"
    log_to_stdout: bool = False

    # Batching
    normalization_batch_size: int = 10

    # Scheduler intervals
    cron_discovery_interval_seconds: int = 43_200
    cron_price_ingest_interval_seconds: int = 900
    cron_news_ingest_interval_seconds: int = 3_600
    cron_macro_ingest_interval_seconds: int = 21_600
    cron_evaluation_interval_seconds: int = 3_600
    cron_alert_check_interval_seconds: int = 3_600

    # Admin
    admin_password: str = "changeme"
    admin_default_role: str = "viewer"

    # Input validation
    max_payload_bytes: int = 1_048_576
    max_text_field_for_prompt: int = 500


settings = Settings()
