import re

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginStart(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        value = value.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", value):
            raise ValueError("Вкажіть коректний Telegram username без посилання")
        return value


class LoginVerify(BaseModel):
    challenge_id: str = Field(min_length=20, max_length=64)
    code: str = Field(pattern=r"^\d{6}$")


class Filters(BaseModel):
    min_price: float | None = Field(default=None, ge=0, le=100_000_000)
    max_price: float | None = Field(default=None, ge=0, le=100_000_000)
    currency: str = Field(default="USD", pattern=r"^(USD|EUR|GBP|CAD|AUD)$")
    condition: str = Field(default="any", pattern=r"^(any|new|used|refurbished|parts)$")
    buying_format: str = Field(default="any", pattern=r"^(any|buy_now|auction)$")
    location: str = Field(default="", max_length=100)
    seller: str = Field(default="", max_length=100)
    free_shipping: bool = False
    include_shipping: bool = False
    exclude_keywords: list[str] = Field(default_factory=list, max_length=30)
    category_id: str = Field(default="", pattern=r"^\d{0,12}$")
    attributes: dict[str, str] = Field(default_factory=dict, max_length=30)

    @model_validator(mode="after")
    def validate_filters(self):
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("Ціна від не може перевищувати ціну до")
        if any(not k.strip() or not v.strip() or len(k) > 100 or len(v) > 300
               for k, v in self.attributes.items()):
            raise ValueError("Вкажіть назву та значення кожного атрибута (до 100 / 300 символів)")
        if any(not v.strip() or len(v) > 100 for v in self.exclude_keywords):
            raise ValueError("Виключення мають містити від 1 до 100 символів")
        self.location = self.location.strip()
        self.seller = self.seller.strip()
        return self


class WatchInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=200)
    filters: Filters = Field(default_factory=Filters)
    interval_seconds: int = Field(default=300, ge=60, le=86400)

    @field_validator("name", "query")
    @classmethod
    def trim(cls, value):
        if not value.strip():
            raise ValueError("Поле не може бути порожнім")
        return value.strip()


class WatchToggle(BaseModel):
    active: bool
