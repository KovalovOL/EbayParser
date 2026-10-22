import re

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginStart(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        value = value.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", value):
            raise ValueError("Enter your Telegram username, such as @your_username. Use your username, not a phone number or display name.")
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
            raise ValueError("Minimum price cannot be higher than maximum price.")
        if any(not k.strip() or not v.strip() or len(k) > 100 or len(v) > 300
               for k, v in self.attributes.items()):
            raise ValueError("Each attribute needs a name (up to 100 characters) and a value (up to 300 characters).")
        if any(k.strip().startswith("_") or k.strip().casefold().startswith("lh_")
               or k.strip().casefold() == "rt" for k in self.attributes):
            raise ValueError("Use eBay item-specific names such as Brand or Model, not search parameters.")
        self.attributes = {k.strip(): v.strip() for k, v in self.attributes.items()}
        if len({k.casefold() for k in self.attributes}) != len(self.attributes):
            raise ValueError("Use each item-specific filter name only once.")
        if self.attributes and not self.category_id:
            raise ValueError("Choose an eBay category ID before adding item-specific filters.")
        if any(not v.strip() or len(v) > 100 for v in self.exclude_keywords):
            raise ValueError("Each excluded word or phrase must contain 1 to 100 characters.")
        self.location = self.location.strip()
        self.seller = self.seller.strip()
        self.exclude_keywords = [value.strip() for value in self.exclude_keywords]
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
            raise ValueError("This field cannot be blank.")
        return value.strip()


class WatchToggle(BaseModel):
    active: bool
