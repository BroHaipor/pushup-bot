"""
Запускается ОДИН РАЗ для заполнения базы данных текущими участниками.
После успешного запуска можно удалить этот файл с GitHub.

Использование: python seed.py
"""

import os
from datetime import datetime, timezone, timedelta
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

KYIV_TZ = timezone(timedelta(hours=3))

USERS = [
    (1916530492, "Даня",    820),
    (5194930596, "Юрчи",   309),
    (746077949,  "Андрей",  215),
    (693525547,  "Сеня",     75),
    (746804739,  "Женя",     50),
    (1299190629, "Масяня",   30),
]


def seed():
    now = datetime.now(KYIV_TZ)

    with psycopg2.connect(DATABASE_URL, sslmode="require") as conn:
        with conn.cursor() as cur:
            for user_id, name, pushups in USERS:
                cur.execute("""
                    INSERT INTO users (user_id, name, pushups, last_updated, joined_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                        SET name     = EXCLUDED.name,
                            pushups  = EXCLUDED.pushups
                """, (user_id, name, pushups, now, now))
                print(f"  ✓ {name} ({user_id}) — {pushups} отж.")

        conn.commit()
    print("\nГотово! Все участники добавлены.")


if __name__ == "__main__":
    seed()
