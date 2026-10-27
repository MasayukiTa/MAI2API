# Token lifecycle: the regression suite for the 2026-10-27 incident,
# where every component accepted an expired JWT because it checked
# only that a token STRING existed. Expiry must be decoded and
# acted on everywhere a token is accepted.
import base64
import json
import os
import time

import mai_proxy as mp
import rpa_keeper as rk


def make_jwt(exp):
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


NOW = int(time.time())


class TestJwtExp:
    def test_future_exp(self):
        assert rk.jwt_exp(make_jwt(NOW + 600)) == NOW + 600

    def test_past_exp(self):
        assert rk.seconds_left(make_jwt(NOW - 60)) < 0

    def test_garbage_tokens(self):
        assert rk.jwt_exp("") == 0
        assert rk.jwt_exp("not-a-jwt") == 0
        assert rk.jwt_exp("a.b.c") == 0
        assert rk.jwt_exp(None) == 0


class TestProxyTokenInfo:
    def _with_token(self, monkeypatch, tmp_path, tok):
        f = tmp_path / "token.txt"
        if tok is not None:
            f.write_text(tok)
        monkeypatch.setattr(mp, "TOKEN_FILE", str(f))
        return mp.token_info()

    def test_fresh_token_is_valid(self, monkeypatch, tmp_path):
        info = self._with_token(monkeypatch, tmp_path, make_jwt(NOW + 3600))
        assert info["loaded"] is True
        assert info["valid"] is True
        assert info["seconds_left"] > 3000

    def test_expired_token_is_loaded_but_not_valid(self, monkeypatch,
                                                    tmp_path):
        # The exact incident shape: file present, JWT expired.
        info = self._with_token(monkeypatch, tmp_path, make_jwt(NOW - 4000))
        assert info["loaded"] is True
        assert info["valid"] is False
        assert info["seconds_left"] < 0

    def test_missing_token(self, monkeypatch, tmp_path):
        info = self._with_token(monkeypatch, tmp_path, None)
        assert info == {"loaded": False, "valid": False}


class TestKeeperAccounting:
    """handle_result: rc 1 (stale) is a FAILURE, never 'delivered';
    three in a row raise the stale alert; a genuine success resets
    and reports recovery."""

    def _run(self, monkeypatch, tmp_path, rcs):
        st_file = tmp_path / "keeper_state.json"
        monkeypatch.setattr(rk, "STATE", str(st_file))
        monkeypatch.setattr(rk, "notify", lambda *a, **k: True)
        st = rk._load_state()
        for rc in rcs:
            rk.handle_result(st, rc)
        return json.loads(st_file.read_text())

    def test_stale_counts_as_failure_and_alerts(self, monkeypatch,
                                                tmp_path):
        state = self._run(monkeypatch, tmp_path, [1, 1, 1])
        assert state["consec"] == 3
        assert state["alert"] == "stale"

    def test_success_resets_after_stale(self, monkeypatch, tmp_path):
        state = self._run(monkeypatch, tmp_path, [1, 1, 1, 0])
        assert state["consec"] == 0
        assert state["alert"] is None

    def test_single_stale_does_not_alert(self, monkeypatch, tmp_path):
        state = self._run(monkeypatch, tmp_path, [1])
        assert state["consec"] == 1
        assert state["alert"] is None

    def test_needs_signin_alerts_immediately(self, monkeypatch,
                                             tmp_path):
        state = self._run(monkeypatch, tmp_path, [2])
        assert state["alert"] == "needs_signin"
        assert state["consec"] == 0

    def test_state_dir_is_the_test_dir(self):
        # Guard rail for the whole suite: no test may touch the real
        # state directory.
        assert os.environ["MAI2API_STATE_DIR"] in rk.STATE_DIR
