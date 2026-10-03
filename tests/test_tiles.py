"""Map layers (tiles.py): fetched once, then served from the cache; unknown layers and offline misses 404."""
from fastapi.testclient import TestClient


def test_satellite_tiles_are_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server, tiles
    monkeypatch.setattr(tiles, "TILE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(tiles, "fetch", lambda layer, z, x, y: calls.append((layer, z, x, y)) or b"JPEGDATA")
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        r = client.get("/tiles/sat/12/2451/1431.jpg")
        assert r.status_code == 200 and r.content == b"JPEGDATA" and r.headers["content-type"] == "image/jpeg"
        assert client.get("/tiles/sat/12/2451/1431.jpg").content == b"JPEGDATA"
        assert calls == [("sat", 12, 2451, 1431)]  # second request came from disk
        assert (tmp_path / "sat" / "12" / "2451" / "1431.jpg").exists()
        assert client.get("/tiles/nope/12/1/1.png").status_code == 404
        assert client.get("/tiles/sat/12/1/1.png").status_code == 404  # wrong extension for the layer

        def offline(*_):
            raise OSError("offline")
        monkeypatch.setattr(tiles, "fetch", offline)
        assert client.get("/tiles/roads/12/1/1.png").status_code == 404
