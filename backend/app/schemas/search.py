from pydantic import BaseModel


class SearchResultItem(BaseModel):
    id: str
    title: str
    node_type: str
    visibility: str
    updated_at: str | None
    score: float


class SearchOut(BaseModel):
    items: list[SearchResultItem]
    total: int
    query: str
