from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASS: str
    DB_NAME: str
    ENCRYPTION_KEY: str

    @property
    def DATABASE_URL_psycopg(self):
        "postgresql+psycopg://postgres:111@localhost:5432/sync_db1"
        return f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()

# инициализация шифратора ключом из .env
fernet = Fernet(settings.ENCRYPTION_KEY.encode())

def encrypt_token(plain_text):
    """Шифрует токен перед записью в бд"""
    if not plain_text:
        return ""
    return fernet.encrypt(plain_text.encode()).decode()

def decrypt_token(cipher_text):
    """Расшифровывает токен из бд для использования в адаптере"""
    if not cipher_text:
        return ""
    try:
        return fernet.decrypt(cipher_text.encode()).decode()
    except Exception:
        # если токен не зашифрован, возвращаем как есть
        return cipher_text
