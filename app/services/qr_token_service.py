import secrets


def generate_qr_token() -> str:
    return secrets.token_urlsafe(24)
