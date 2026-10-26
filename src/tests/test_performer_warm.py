"""The warm-up asks for the same picture URL the title page does."""

from program.services.tpdb_warm import performer_picture_urls

record = {
    "performers": [
        {"name": "A", "image": "https://cdn.theporndb.net/a", "parent": {"image": "https://cdn.theporndb.net/pa"}},
        {"name": "B", "image": None, "parent": {"image": "https://cdn.theporndb.net/pb"}},
        {"name": "C", "image": None, "face": "https://thumb.theporndb.net/c", "parent": None},
        {"name": "D", "parent": {}},
        {"name": "E", "image": "https://cdn.theporndb.net/a"},
    ]
}

assert performer_picture_urls(record) == [
    "https://cdn.theporndb.net/a",
    "https://cdn.theporndb.net/pb",
    "https://thumb.theporndb.net/c",
], performer_picture_urls(record)
print("ok")
