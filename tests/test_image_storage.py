"""Storage-layer regression tests for note images.

These target `image_storage` (DatabaseImageStorage) and the underlying CRUD
directly, pinning two defects:

* `crud.get_user_image` previously never returned the row (so every read 404'd).
* `DatabaseImageStorage.get` built its URL from the integer PK instead of the
  `unique_id`, so a retrieved image's URL didn't match the one embedded into
  the note at save time.

The function-scoped pattern (create tables, then dispose the engine) mirrors
test_storage.py to keep the async engine bound to each test's event loop.
"""

import uuid

from beaverhabits.app.auth import user_authenticate, user_create
from beaverhabits.app.db import User as HabitUser
from beaverhabits.app.db import create_db_and_tables, engine
from beaverhabits.storage import image_storage

PASSWORD = "test"
# Non-UTF8 bytes ensure the blob is stored and returned verbatim.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03\xff\xfe storage-payload"


async def _make_user(prefix: str) -> HabitUser:
    email = f"{prefix}_{uuid.uuid4().hex}@test.com"
    user = await user_authenticate(email=email, password=PASSWORD)
    if not user:
        user = await user_create(email=email, password=PASSWORD)
    return user


async def test_save_and_get_roundtrip_with_consistent_url():
    await create_db_and_tables()
    user = await _make_user("owner")

    saved = await image_storage.save(PNG_BYTES, user)
    # The embedded URL uses the public unique_id, not the internal PK.
    assert saved.id
    assert saved.url == f"/assets/{saved.id}"
    assert saved.owner == user.email

    got = await image_storage.get(saved.id, user)
    assert got is not None
    assert got.blob == PNG_BYTES
    assert got.owner == user.email
    # The retrieved URL must match the one handed out at save time.
    assert got.url == saved.url

    await engine.dispose()


async def test_get_is_isolated_across_users():
    await create_db_and_tables()
    owner = await _make_user("owner")
    other = await _make_user("other")

    saved = await image_storage.save(PNG_BYTES, owner)

    # Owner retrieves their image.
    assert await image_storage.get(saved.id, owner) is not None
    # Another user cannot retrieve it.
    assert await image_storage.get(saved.id, other) is None

    await engine.dispose()


async def test_get_unknown_image_returns_none():
    await create_db_and_tables()
    user = await _make_user("owner")

    assert await image_storage.get(str(uuid.uuid4()), user) is None

    await engine.dispose()
