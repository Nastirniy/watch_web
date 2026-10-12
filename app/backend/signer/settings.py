"""Settings of the signing service (environment SIGNER_*; independent of the application settings)"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class SignerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIGNER_")

    ca_cert_path: str
    ca_key_path: str
    ca_key_password: str = ""
    socket_path: str = ""
    max_validity_days: int = 730
    max_crl_days: int = 31
