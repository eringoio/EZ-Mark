from app.services.qr_token_service import generate_qr_token


def test_qr_token_is_opaque_and_unique() -> None:
    tokens = {generate_qr_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 32 for token in tokens)
    assert all("Student" not in token for token in tokens)
