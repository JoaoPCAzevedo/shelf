"""A 977 scan in Add and Wishlist opens the periodical confirm card.

977 is GS1's ISSN prefix, so the barcode decides whatever the Media Type hint
says (G57). The dispatch sits ahead of the bare-legacy stop and the UPC branch
in `_scan_isbn_inner`; existing-item modes are deliberately untouched.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.features import FEATURES
from app.services import locations as location_svc
from app.services import provider_result
from tests.test_features_gate import off

ISSN_13 = "9772280147904"           # camera read, no add-on
ISSN_15 = "977016173700805"         # 13 + 2-digit add-on
ISSN_18 = "977228014790460066"      # 13 + 5-digit add-on (#155's code)
BAD_CHECK = "9771617370080"         # 977 prefix, bad check digit -> UPC path

SHAPES = [
    (ISSN_13, "2280-1472", None),
    (ISSN_15, "0161-7370", "05"),
    (ISSN_18, "2280-1472", "60066"),
]


def _found(issn, client):
    return provider_result.found("issn_portal", {"title": "Internazionale"})


@pytest.fixture
def portal():
    """Stub the ISSN portal where `periodical_scan` calls through it (G37)."""
    fake = AsyncMock(side_effect=_found)
    with patch("app.services.issn_portal.lookup", new=fake):
        yield fake


@pytest.fixture
def upc():
    """UPC Item DB must not be asked about a valid 977."""
    fake = AsyncMock(return_value=provider_result.no_match("upcitemdb"))
    with patch("app.services.upcitemdb.lookup", new=fake):
        yield fake


def _scan(client, code, *, mode="add", media_type="auto", url="/api/scan", **extra):
    return client.post(url, data={"isbn": code, "mode": mode, "media_type": media_type, **extra})


def _confirm(client, code, **extra):
    data = {"raw_barcode": code, "publication_title": "Internazionale", "issue_number": "1"}
    data.update(extra)
    return client.post("/api/periodicals/confirm", data=data, follow_redirects=False)


@pytest.mark.usefixtures("portal")
class TestA977ScanOpensTheConfirmCard:
    @pytest.mark.parametrize("code,issn,addon", SHAPES)
    @pytest.mark.parametrize("mode", ["add", "wishlist"])
    @pytest.mark.parametrize("media_type", ["auto", "book", "dvd", "magazine"])
    def test_every_shape_mode_and_hint(self, editor_client, upc, code, issn, addon, mode, media_type):
        resp = _scan(editor_client, code, mode=mode, media_type=media_type)

        assert resp.status_code == 200
        assert 'data-scan-status="periodical_confirm"' in resp.text
        assert issn in resp.text
        if addon:
            assert f"+ {addon}" in resp.text
        upc.assert_not_awaited()

    def test_wishlist_mode_is_carried_into_the_card(self, editor_client):
        resp = _scan(editor_client, ISSN_18, mode="wishlist")

        assert 'name="mode" value="wishlist"' in resp.text

    def test_with_every_feature_off_the_card_still_opens(self, editor_client, upc):
        off(*FEATURES)

        resp = _scan(editor_client, ISSN_18)

        assert 'data-scan-status="periodical_confirm"' in resp.text
        upc.assert_not_awaited()

    @pytest.mark.parametrize("mode", ["add", "wishlist"])
    def test_the_scan_is_logged_as_periodical_confirm(self, editor_client, db, mode):
        _scan(editor_client, ISSN_18, mode=mode)

        db.commit()  # G48: read a fresh snapshot
        row = db.execute(
            "SELECT isbn, media_type, result, item_id, mode FROM scan_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert dict(row) == {
            "isbn": ISSN_18, "media_type": "magazine", "result": "periodical_confirm",
            "item_id": None, "mode": mode,
        }


@pytest.mark.usefixtures("portal")
class TestOtherCodesKeepTodaysOutcome:
    def test_a_977_with_a_bad_check_digit_takes_the_upc_path(self, editor_client, upc):
        resp = _scan(editor_client, BAD_CHECK)

        upc.assert_awaited()
        assert "periodical_confirm" not in resp.text

    @pytest.mark.parametrize("code", ["978016173700805", "978228014790460066"])
    def test_a_non_977_long_code_is_still_invalid(self, editor_client, upc, code):
        resp = _scan(editor_client, code)

        assert 'data-scan-status="error"' in resp.text
        assert "Invalid ISBN" in resp.text
        assert "periodical_confirm" not in resp.text


@pytest.mark.usefixtures("portal")
class TestAnOwnedIssueRescanned:
    def test_rescanning_its_full_code_gives_the_duplicate_card(self, editor_client, db, upc):
        assert _confirm(editor_client, ISSN_18).status_code == 303
        db.commit()
        item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]

        resp = _scan(editor_client, ISSN_18)

        assert 'data-scan-status="duplicate"' in resp.text
        assert f"/item/{item_id}" in resp.text
        upc.assert_not_awaited()
        db.commit()
        row = db.execute("SELECT result, item_id FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
        assert (row["result"], row["item_id"]) == ("duplicate", item_id)


@pytest.mark.usefixtures("portal")
class TestExistingItemModesAreUnchanged:
    """977 in lookup/move/inventory answers what it answered before this plan:
    `_find_item_by_barcode` cannot see a magazine, so nothing is found."""

    @pytest.mark.parametrize("mode", ["lookup", "move", "inventory"])
    @pytest.mark.parametrize("code", [ISSN_13, ISSN_18])
    def test_no_periodical_card_in_existing_item_modes(self, editor_client, db, upc, mode, code):
        loc = db.execute("INSERT INTO locations (name) VALUES ('Shelf')").lastrowid
        db.commit()

        resp = _scan(editor_client, code, mode=mode, location_id=loc)

        assert resp.status_code == 200
        assert "periodical_confirm" not in resp.text
        assert 'data-scan-status="not_owned"' in resp.text
        upc.assert_not_awaited()


@pytest.mark.usefixtures("portal")
class TestShelfFillAndA977:
    """Decision 4: Shelf Fill renders the card for a new issue and files
    nothing; an owned issue rescanned with its add-on is placed."""

    @pytest.fixture
    def shelf(self, db):
        room = location_svc.create_location(db, "Room")
        shelf = location_svc.create_location(db, "Shelf 9", parent_id=room)
        db.commit()
        return shelf

    def test_a_new_977_renders_the_card_and_files_nothing(self, editor_client, db, upc, shelf):
        resp = _scan(editor_client, ISSN_18, url="/api/shelf-fill/scan", location_id=shelf)

        assert resp.status_code == 200
        assert 'data-scan-status="periodical_confirm"' in resp.text
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        upc.assert_not_awaited()

    def test_an_owned_issue_is_placed_on_the_shelf(self, editor_client, db, shelf):
        assert _confirm(editor_client, ISSN_18).status_code == 303
        db.commit()
        item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]

        resp = _scan(editor_client, ISSN_18, url="/api/shelf-fill/scan", location_id=shelf)

        assert resp.status_code == 200
        assert 'data-testid="shelf-fill-position"' in resp.text
        db.commit()
        copy = db.execute(
            "SELECT location_id, position_order FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()
        assert copy["location_id"] == shelf
        assert copy["position_order"] is not None


@pytest.mark.usefixtures("portal")
class TestTheConsumersClassifyItAsInfo:
    """`periodical_confirm` is a report awaiting the user, not an error: every
    consumer that classifies a status must list it as info (G62, G65, G128)."""

    ROOT = Path(__file__).resolve().parents[1]

    def _src(self, *parts):
        return self.ROOT.joinpath(*parts).read_text()

    def test_the_recent_scans_row_is_blue_never_red(self, editor_client):
        _scan(editor_client, ISSN_18)

        resp = editor_client.get("/api/recent-scans?mode=add")

        assert resp.status_code == 200
        assert "periodical_confirm" in resp.text
        i = resp.text.rindex("periodical_confirm")
        marker = resp.text[max(0, i - 400):i + 30]
        assert "bg-blue-500/20" in marker
        assert "bg-shelf-error" not in marker

    def test_the_card_carries_the_reader_attributes(self, editor_client):
        html = _scan(editor_client, ISSN_18).text

        for attr in ("data-scan-badge", "data-scan-title", "data-scan-detail"):
            assert attr in html
        assert "data-scan-error" not in html
        assert "Confirm the issue below" in html

    def test_the_info_set_lists_it(self):
        js = self._src("static", "js", "app.js")
        assert re.search(r"SCAN_INFO_STATUSES\s*=\s*\[[^\]]*'periodical_confirm'", js)

    def test_scan_js_copies_info_into_the_overlay_state(self):
        js = self._src("static", "js", "scan.js")
        assert "info: outcome.info" in js
        assert "info: false" in js

    def test_shelf_fill_copies_info_and_renders_a_blue_arm(self):
        js = self._src("static", "js", "shelf-fill.js")
        assert "info: outcome && outcome.info" in js
        tpl = self._src("app", "templates", "shelf_fill.html")
        assert "scanResult.info" in tpl
        assert "bg-shelf-success/30" in tpl and "scanResult.ok ?" in tpl
        for line in tpl.splitlines():
            if "bg-shelf-success/30" in line:
                assert "scanResult.ok" in line, "no unconditional green pill"
