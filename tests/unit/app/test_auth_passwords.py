import hashlib

from lantai.app import auth


def _memory_auth_store(monkeypatch):
    state: dict = {}

    def load() -> dict:
        return dict(state)

    def save(value: dict) -> None:
        state.clear()
        state.update(value)

    monkeypatch.setattr(auth, "_load_auth_data", load)
    monkeypatch.setattr(auth, "_save_auth_data", save)
    return state


def test_new_passwords_use_scrypt_and_authenticate(monkeypatch):
    state = _memory_auth_store(monkeypatch)

    token = auth.register_user("sam", "correct horse battery staple")

    assert token
    assert state["user"]["password_hash_scheme"] == "scrypt"
    assert state["user"]["password_hash_params"]["n"] >= 2**14
    assert auth.authenticate("sam", "correct horse battery staple")
    assert auth.authenticate("sam", "wrong") is None


def test_legacy_sha256_password_is_upgraded_after_successful_login(monkeypatch):
    state = _memory_auth_store(monkeypatch)
    salt = "0123456789abcdef0123456789abcdef"
    password = "legacy password"
    state.update(
        {
            "user": {
                "username": "sam",
                "password_hash": hashlib.sha256(
                    (salt + password).encode("utf-8"),
                ).hexdigest(),
                "password_salt": salt,
            },
            "jwt_secret": "a" * 64,
        },
    )

    token = auth.authenticate("sam", password)

    assert token
    assert state["user"]["password_hash_scheme"] == "scrypt"
    assert auth.authenticate("sam", password)


def test_scrypt_verification_rejects_unbounded_stored_parameters(monkeypatch):
    def unexpected_scrypt(*_args, **_kwargs):
        raise AssertionError("unbounded scrypt parameters reached hashlib")

    monkeypatch.setattr(auth.hashlib, "scrypt", unexpected_scrypt)

    assert not auth.verify_password(
        "password",
        "00" * 32,
        "00" * 16,
        scheme="scrypt",
        params={"n": 2**30, "r": 8, "p": 1, "dklen": 32},
    )
