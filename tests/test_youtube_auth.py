import json


class FakeCredentials:
    token = "token"
    refresh_token = "refresh"
    token_uri = "https://oauth2.googleapis.com/token"
    client_id = "client-id"
    client_secret = "client-secret"
    scopes = ["scope"]
    expiry = None


class FakeFlow:
    created = []

    def __init__(self, *, code_verifier=None):
        self.code_verifier = code_verifier or "generated-verifier"
        self.credentials = FakeCredentials()
        self.fetch_kwargs = None
        FakeFlow.created.append(self)

    @classmethod
    def from_client_secrets_file(cls, *_args, **kwargs):
        return cls(code_verifier=kwargs.get("code_verifier"))

    def authorization_url(self, **_kwargs):
        return "https://accounts.google.com/o/oauth2/auth", "saved-state"

    def fetch_token(self, **kwargs):
        self.fetch_kwargs = kwargs


def test_youtube_oauth_persists_pkce_code_verifier(tmp_path, monkeypatch):
    import youtube_auth

    yt_dir = tmp_path / ".youtube"
    client_secret = yt_dir / "client_secret.json"
    token_path = yt_dir / "token.json"
    state_path = yt_dir / "auth_state.json"
    yt_dir.mkdir()
    client_secret.write_text('{"web": {"client_id": "id", "client_secret": "secret"}}', encoding="utf-8")

    monkeypatch.setattr(youtube_auth, "YT_DIR", yt_dir)
    monkeypatch.setattr(youtube_auth, "CLIENT_SECRET_PATH", client_secret)
    monkeypatch.setattr(youtube_auth, "TOKEN_PATH", token_path)
    monkeypatch.setattr(youtube_auth, "STATE_PATH", state_path)
    monkeypatch.setattr(youtube_auth, "Flow", FakeFlow)
    FakeFlow.created = []

    redirect_uri = "http://127.0.0.1:8876/content/youtube/auth/callback"
    youtube_auth.start_auth(redirect_uri)

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["state"] == "saved-state"
    assert saved["code_verifier"] == "generated-verifier"

    youtube_auth.finish_auth("auth-code", redirect_uri, "saved-state")

    callback_flow = FakeFlow.created[-1]
    assert callback_flow.code_verifier == "generated-verifier"
    assert callback_flow.fetch_kwargs["code"] == "auth-code"
    assert token_path.exists()
    assert not state_path.exists()


# --- connection validity (regression: a dead token used to read as "connected"
#     so the daily pull failed silently and the UI showed no way to reconnect) ---


def test_connection_state_needs_reconnect_when_token_unusable(monkeypatch):
    import youtube_auth

    monkeypatch.setattr(youtube_auth, "has_client_secret", lambda: True)
    monkeypatch.setattr(youtube_auth, "token_on_disk", lambda: True)
    monkeypatch.setattr(youtube_auth, "get_authenticated_credentials", lambda: None)

    assert youtube_auth.connection_state() == "needs_reconnect"
    # is_connected must reflect real usability, not mere file existence
    assert youtube_auth.is_connected() is False


def test_connection_state_ok_when_credentials_valid(monkeypatch):
    import youtube_auth

    monkeypatch.setattr(youtube_auth, "has_client_secret", lambda: True)
    monkeypatch.setattr(youtube_auth, "token_on_disk", lambda: True)
    monkeypatch.setattr(youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())

    assert youtube_auth.connection_state() == "ok"
    assert youtube_auth.is_connected() is True


def test_connection_state_not_setup_without_token(monkeypatch):
    import youtube_auth

    monkeypatch.setattr(youtube_auth, "has_client_secret", lambda: True)
    monkeypatch.setattr(youtube_auth, "token_on_disk", lambda: False)

    assert youtube_auth.connection_state() == "not_setup"


def test_get_status_flags_needs_reconnect_for_expired_token(monkeypatch):
    import youtube_auth

    class DeadCreds:
        valid = False
        expiry = None
        refresh_token = "present-but-revoked"  # the field that used to fool get_status

    monkeypatch.setattr(youtube_auth, "has_client_secret", lambda: True)
    monkeypatch.setattr(youtube_auth, "_load_credentials", lambda: DeadCreds())

    st = youtube_auth.get_status()
    assert st.connected is False
    assert st.needs_reconnect is True
    assert st.error and "reconnect" in st.error.lower()
