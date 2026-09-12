from typing import Any


def find_user(connection: Any, username: str) -> Any:
    query = f"SELECT id, username, email FROM users WHERE username = '{username}'"
    return connection.execute(query).fetchone()
