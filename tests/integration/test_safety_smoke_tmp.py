from typing import Any


def test_smoke(safe: Any) -> None:
    r = safe.recover()
    print("RECOVERY_NO_BASE", r)
    safe.baseline()
    r = safe.recover()
    print("RECOVERY", r)
    assert r.complete, r
    out = safe.act("resume")
    print("RESUME", out)
    assert out.kind == "ok", out
    res = safe.pipeline.submit(safe.proposal(), source="test", slot="1", book=safe.book())
    print("SUBMIT", res)
    assert res.kind == "submitted", res
    run = safe.recon()
    print("RUN", run)
    assert run.outcome == "OK", run
