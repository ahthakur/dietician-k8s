"""Users can only change their own food entries and fridge items (db layer and routes)."""

import db
from conftest import login


def _alice_entry():
    db.add_food_log_entry("2026-11-23", "alice", "chai", 100, 3)
    return db.get_food_log("2026-11-23", "alice")[0]["id"]


def test_food_log_scoped_to_owner():
    eid = _alice_entry()
    assert not db.update_food_log_entry("bob", eid, 999, 9)
    assert not db.delete_food_log_entry("bob", eid)
    assert db.get_food_log("2026-11-23", "alice")[0]["calories"] == 100

    assert db.update_food_log_entry("alice", eid, 120, 4, description="masala chai", quantity=2)
    row = db.get_food_log("2026-11-23", "alice")[0]
    assert (row["calories"], row["description"], row["quantity"]) == (120, "masala chai", 2)
    assert db.delete_food_log_entry("alice", eid)


def test_fridge_scoped_to_fridge_id():
    db.add_fridge_items_bulk("alice", [{"name": "paneer", "quantity": "1", "category": "dairy"}])
    fid = db.get_fridge_items("alice")[0]["id"]
    assert not db.update_fridge_item("bob", fid, quantity="5")
    assert not db.remove_fridge_item_by_id("bob", fid)
    assert db.update_fridge_item("alice", fid, quantity="2")
    assert db.get_fridge_items("alice")[0]["quantity"] == "2"
    assert db.remove_fridge_item_by_id("alice", fid)


def test_shared_fridge_members_can_edit():
    db.set_fridge_group("alice", "abc123")
    db.set_fridge_group("bob", "abc123")
    db.add_fridge_items_bulk(db.get_fridge_id("alice"), [{"name": "milk"}])
    fid = db.get_fridge_items("abc123")[0]["id"]
    assert db.update_fridge_item(db.get_fridge_id("bob"), fid, quantity="1L")


def test_routes_return_404_for_other_users_rows(client):
    eid = _alice_entry()
    db.add_fridge_items_bulk("alice", [{"name": "paneer"}])
    fid = db.get_fridge_items("alice")[0]["id"]

    assert client.delete(f"/api/food-log/{eid}").status_code == 401   # signed out
    login(client, "bob")
    assert client.delete(f"/api/food-log/{eid}").status_code == 404
    assert client.put(f"/api/food-log/{eid}", json={"calories": 1}).status_code == 404
    assert client.put(f"/api/fridge/{fid}", json={"quantity": "9"}).status_code == 404
    assert client.delete(f"/api/fridge/id/{fid}").status_code == 404

    login(client, "alice")
    assert client.put(f"/api/food-log/{eid}", json={"calories": 150, "protein": 4}).status_code == 200
    assert client.delete(f"/api/fridge/id/{fid}").status_code == 200
    assert client.delete(f"/api/food-log/{eid}").status_code == 200
