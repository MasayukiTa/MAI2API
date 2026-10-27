# Assorted pure units: Retry-After parsing, transcript truncation,
# session keys, harvest key handling, rotating log writer.
import email.utils
import os
import time

import mai_proxy as mp
import rpa_keeper as rk


class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


class TestRetryAfter:
    def test_delta_seconds(self):
        assert mp._retry_after_seconds(_Headers({"Retry-After": "42"})) == 42

    def test_http_date(self):
        when = email.utils.formatdate(time.time() + 90, usegmt=True)
        got = mp._retry_after_seconds(_Headers({"Retry-After": when}))
        assert 80 <= got <= 95

    def test_garbage_and_missing(self):
        assert mp._retry_after_seconds(_Headers({"Retry-After": "soon"})) \
            is None
        assert mp._retry_after_seconds(_Headers()) is None


class TestTruncation:
    def test_stuff_cap(self, monkeypatch):
        monkeypatch.setattr(mp, "STUFF_CHAR_CAP", 1000)
        turns = [("user", "x" * 300), ("assistant", "y" * 300),
                 ("user", "z" * 300), ("assistant", "w" * 300)]
        text = mp.flatten_for_transcript("", turns, "")
        assert len(text) <= 1100  # cap + marker slack
        assert "omitted by the proxy" in text
        assert "w" * 300 in text  # the newest turn survives


class TestSessionKey:
    def test_stable_and_sensitive(self):
        t1 = [("user", "hi")]
        assert mp.session_key("s", t1, "") == mp.session_key("s", t1, "")
        assert mp.session_key("s", t1, "") != mp.session_key("s2", t1, "")
        assert mp.session_key("s", t1, "") != mp.session_key(
            "s", [("user", "other")], "")


class TestHarvestKey:
    def test_ensure_creates_and_reuses(self, tmp_path):
        key_file = tmp_path / "harvest_key.txt"
        import unittest.mock as mock
        with mock.patch.object(mp, "HARVEST_KEY_FILE", str(key_file)), \
                mock.patch.object(mp, "STATE_DIR", str(tmp_path)):
            k1 = mp._ensure_harvest_key()
            k2 = mp._ensure_harvest_key()
        assert k1 == k2 and len(k1) == 64
        assert key_file.read_text().strip() == k1


class TestRotatingStdout:
    def test_rotation_caps_the_log(self, tmp_path):
        path = str(tmp_path / "loop.log")
        w = rk._RotatingStdout(path, max_bytes=300, backups=2)
        for i in range(60):
            w.write(f"line {i:02d} xxxxxxxxxxxxxxxxxx\n")
        w.flush()
        assert os.path.exists(path)
        assert os.path.exists(path + ".1")
        total = sum(os.path.getsize(path + s) for s in ("", ".1", ".2"))
        assert total < 300 * 3 + 200  # bounded by cap x files + slack


class TestStateSeparation:
    def test_state_dir_comes_from_env(self):
        assert mp.STATE_DIR == os.environ["MAI2API_STATE_DIR"]
        assert rk.STATE_DIR == os.environ["MAI2API_STATE_DIR"]
        assert mp.TOKEN_FILE.startswith(mp.STATE_DIR)
