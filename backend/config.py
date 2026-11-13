"""Configuration module for MemoryIllumination backend.

Loads configuration from .env file (secrets) and .config.json (non-secrets).
Exports a single Config object that app.py imports.
"""
import json
import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env file from project root
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
load_dotenv(env_path)


@dataclass
class Config:
    # Environment
    DEPLOY_ENV: str = 'development'

    # URLs and Ports
    SITE_BASE_URL: str = 'http://127.0.0.1:8000'
    API_BASE_URL: str = 'http://127.0.0.1:5000'
    FRONTEND_PORT: str = '8000'
    LOCAL_WORKER_URL: str = 'http://127.0.0.1:5001/generate'

    # Secrets
    SECRET_KEY: str = ''
    EMAIL_API_KEY: str = ''
    RESEND_WEBHOOK_SECRET: str = ''
    ADMIN_KEY: str = ''

    # Email
    MAIL_FROM: str = 'Memory Illumination <noreply@mail.memoryillumination.com>'
    MAIL_REPLY_TO: str = 'support@memoryillumination.com'

    # Testing
    SMOKE_TEST_DB: str = ''
    SMOKE_TEST_USER: str = ''

    # Timeouts and retention (from .config.json)
    CONFIRM_TOKEN_MAX_AGE: int = 3600
    JOB_RETENTION_SECONDS: int = 86400
    COLD_ESTIMATE_SECONDS: int = 60
    WARM_ESTIMATE_SECONDS: int = 30
    JOB_MAX_AGE_SECONDS: int = 3600

    # Rate limits (from .config.json)
    RATE_LIMITS: dict = None

    def __post_init__(self):
        if self.RATE_LIMITS is None:
            self.RATE_LIMITS = {}

    # Upload limits (from .config.json)
    MAX_UPLOAD_BYTES: int = 10485760
    MAX_UPLOAD_PX: int = 16777216

    # Watermarking (from .config.json)
    WATERMARK_FREE_TIER: bool = False
    WATERMARK_OPACITY: float = 0.12
    WATERMARK_SCALE: float = 0.1
    WATERMARK_SPACING: float = 0.33

    def validate(self):
        """Validate required configuration values."""
        errors = []
        if not self.SECRET_KEY:
            errors.append('SECRET_KEY is required')
        if self.DEPLOY_ENV not in ('development', 'production'):
            errors.append('DEPLOY_ENV must be "development" or "production"')
        if errors:
            raise ValueError('; '.join(errors))

    def load(self):
        """Load configuration from environment variables and .config.json."""
        # Environment variables
        self.DEPLOY_ENV = os.environ.get('DEPLOY_ENV', 'development').strip().lower()
        self.SITE_BASE_URL = os.environ.get('SITE_BASE_URL', self.SITE_BASE_URL)
        self.API_BASE_URL = os.environ.get('API_BASE_URL', self.API_BASE_URL)
        self.FRONTEND_PORT = os.environ.get('FRONTEND_PORT', self.FRONTEND_PORT)
        self.LOCAL_WORKER_URL = os.environ.get('LOCAL_WORKER_URL', self.LOCAL_WORKER_URL)
        self.SECRET_KEY = os.environ.get('SECRET_KEY', '')
        self.EMAIL_API_KEY = os.environ.get('EMAIL_API_KEY', '')
        self.RESEND_WEBHOOK_SECRET = os.environ.get('RESEND_WEBHOOK_SECRET', '')
        self.ADMIN_KEY = os.environ.get('ADMIN_KEY', '')
        self.MAIL_FROM = os.environ.get('MAIL_FROM', self.MAIL_FROM)
        self.MAIL_REPLY_TO = os.environ.get('MAIL_REPLY_TO', self.MAIL_REPLY_TO)
        self.SMOKE_TEST_DB = os.environ.get('SMOKE_TEST_DB', '')
        self.SMOKE_TEST_USER = os.environ.get('SMOKE_TEST_USER', '')

        # Load .config.json
        config_path = os.path.join(os.path.dirname(__file__), '.config.json')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)

            self.CONFIRM_TOKEN_MAX_AGE = config.get('confirm_token_max_age', self.CONFIRM_TOKEN_MAX_AGE)
            self.JOB_RETENTION_SECONDS = config.get('job_retention_seconds', self.JOB_RETENTION_SECONDS)
            self.COLD_ESTIMATE_SECONDS = config.get('cold_estimate_seconds', self.COLD_ESTIMATE_SECONDS)
            self.WARM_ESTIMATE_SECONDS = config.get('warm_estimate_seconds', self.WARM_ESTIMATE_SECONDS)
            self.JOB_MAX_AGE_SECONDS = config.get('job_max_age_seconds', self.JOB_MAX_AGE_SECONDS)
            self.RATE_LIMITS = config.get('rate_limits', self.RATE_LIMITS)
            self.MAX_UPLOAD_BYTES = config.get('max_upload_bytes', self.MAX_UPLOAD_BYTES)
            self.MAX_UPLOAD_PX = config.get('max_upload_px', self.MAX_UPLOAD_PX)
            self.WATERMARK_FREE_TIER = config.get('watermark_free_tier', self.WATERMARK_FREE_TIER)
            self.WATERMARK_OPACITY = config.get('watermark_opacity', self.WATERMARK_OPACITY)
            self.WATERMARK_SCALE = config.get('watermark_scale', self.WATERMARK_SCALE)
            self.WATERMARK_SPACING = config.get('watermark_spacing', self.WATERMARK_SPACING)

        return self


# Create and load the configuration
config = Config().load()
config.validate()
