import pytest

from app.core.config import get_settings


def test_settings_load():
    s = get_settings()
    assert s.hrms_db_name
    assert s.ocr_db_name
    assert s.ops_min_bps == 17
    assert s.tps_max_bps == 16


@pytest.mark.integration
def test_db_pings():
    from app.db.hrms import hrms_ping
    from app.db.ocr import ocr_ping

    assert hrms_ping() is True
    assert ocr_ping() is True
