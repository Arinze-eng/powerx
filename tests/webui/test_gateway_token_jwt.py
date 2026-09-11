from nanobot.webui.gateway_tokens import GatewayTokenStore


def test_attach_and_consume_jwt_roundtrip():
    store = GatewayTokenStore()
    token = store.issue_token(60, audience="webui")
    store.attach_issued_token_user(token, "user-123", "raw.supabase.jwt")

    # User id and JWT are both retrievable exactly once (consume semantics).
    assert store.consume_issued_token_jwt(token) == "raw.supabase.jwt"
    assert store.consume_issued_token_user(token) == "user-123"
    # Second consume returns empty (already dropped).
    assert store.consume_issued_token_jwt(token) == ""


def test_attach_without_jwt_leaves_empty():
    store = GatewayTokenStore()
    token = store.issue_token(60, audience="webui")
    store.attach_issued_token_user(token, "user-abc")  # no jwt arg
    assert store.consume_issued_token_user(token) == "user-abc"
    assert store.consume_issued_token_jwt(token) == ""


def test_expired_token_purges_jwt():
    store = GatewayTokenStore()
    token = store.issue_token(-1, audience="webui")  # already expired
    store.attach_issued_token_user(token, "u", "jwt-value")
    # take_issued_token_audience purges expired entries incl. bound jwt.
    assert store.take_issued_token_audience(token) is None
    assert store.consume_issued_token_jwt(token) == ""


def test_clear_drops_jwts():
    store = GatewayTokenStore()
    token = store.issue_token(60, audience="webui")
    store.attach_issued_token_user(token, "u", "j")
    store.clear()
    assert store.consume_issued_token_jwt(token) == ""
