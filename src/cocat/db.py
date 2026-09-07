from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from functools import partial
from typing import Any
from uuid import UUID

from pycrdt import (
    ArrayEvent,
    Doc,
    Map,
    MapEvent,
    Transaction,
    TransactionEvent,
    YMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)
from rich.console import Console
from rich.pretty import pprint

from .catalogue import Catalogue
from .event import Event
from .models import CatalogueModel, EventModel


class DB:
    """
    A database which holds events and catalogues.
    """

    def __init__(self, doc: Doc | None = None) -> None:
        """
        Creates a database.

        Args:
            doc: An optional [Doc](https://y-crdt.github.io/pycrdt/api_reference/#pycrdt.Doc).
        """
        self._doc: Doc = Doc() if doc is None else doc
        self._catalogue_maps = self._doc.get("catalogues", type=Map)
        self._event_maps = self._doc.get("events", type=Map)
        self._synced: list[DB] = []
        self._catalogue_maps.observe_deep(self._catalogues_changed)
        self._catalogue_delete_callbacks: dict[str, list[Callable[[Any], None]]] = (
            defaultdict(list)
        )
        self._catalogue_create_callbacks: list[Callable[[Any, Any], None]] = []
        self._catalogue_change_callbacks: dict[
            str, dict[str, list[Callable[[Any, Any], None]]]
        ] = defaultdict(lambda: defaultdict(list))
        self._catalogues: dict[str, Catalogue] = {}
        self._event_maps.observe_deep(self._events_changed)
        self._event_delete_callbacks: dict[str, list[Callable[[Any], None]]] = (
            defaultdict(list)
        )
        self._event_create_callbacks: list[Callable[[Any, Any], None]] = []
        self._event_change_callbacks: dict[
            str, dict[str, list[Callable[[Any, Any], None]]]
        ] = defaultdict(lambda: defaultdict(list))
        self._events: dict[str, Event] = {}

    def __repr__(self) -> str:
        console = Console()
        with console.capture() as capture:
            pprint(self.to_dict(), console=console, max_length=8)
        return capture.get()

    def _callback(
        self, callback: Callable[..., None], origin: DB | None, *args: Any
    ) -> None:
        if origin is not self:
            callback(*args)

    def transaction(self) -> Transaction:
        return self._doc.transaction(self)

    @classmethod
    def from_dict(cls, db_dict: dict[str, Any], doc: Doc | None = None) -> DB:
        """
        Creates a database from a dictionary.

        Args:
            db_dict: The dictionary.
            doc: An optional [Doc](https://y-crdt.github.io/pycrdt/api_reference/#pycrdt.Doc).

        Returns:
            The created database.
        """
        db = DB(doc=doc)
        with db.transaction():
            for item in db_dict["events"]:
                db.create_event(**item)
            for item in db_dict["catalogues"]:
                events = [db.get_event(uuid) for uuid in item.pop("events", [])]
                db.create_catalogue(events=events, **item)
            return db

    @classmethod
    def from_json(cls, data: str, doc: Doc | None = None) -> DB:
        """
        Creates a database from a JSON string.

        Args:
            data: The JSON string.
            doc: An optional [Doc](https://y-crdt.github.io/pycrdt/api_reference/#pycrdt.Doc).

        Returns:
            The created database.
        """
        return DB.from_dict(json.loads(data), doc)

    @property
    def doc(self) -> Doc:
        """
        Returns:
            The database [Doc](https://y-crdt.github.io/pycrdt/api_reference/#pycrdt.Doc).
        """
        return self._doc

    def _catalogues_changed(
        self, events: list[ArrayEvent | MapEvent], transaction: Transaction
    ) -> None:
        for event in events:
            path = event.path  # type: ignore[union-attr]
            if len(path) == 0:
                # catalogue created or deleted
                assert isinstance(event, MapEvent)
                keys = event.keys  # type: ignore[attr-defined]
                for uuid in keys:
                    action = keys[uuid]["action"]
                    if action == "delete":
                        for delete_callback in self._catalogue_delete_callbacks[uuid]:
                            delete_callback(transaction.origin)
                        if uuid in self._catalogues:
                            del self._catalogues[uuid]
                        del self._catalogue_delete_callbacks[uuid]
                        self._catalogue_change_callbacks
                        del self._catalogue_change_callbacks[uuid]
                    elif action == "add":
                        for create_callback in self._catalogue_create_callbacks:
                            create_callback(
                                transaction.origin, self.get_catalogue(uuid)
                            )
            elif len(path) == 1:
                # property of catalogue changed (not events)
                assert isinstance(event, MapEvent)
                uuid = path[0]
                changed_keys = event.keys  # type: ignore[attr-defined]
                for key in changed_keys:
                    if key in self._catalogue_change_callbacks[uuid]:
                        callbacks = self._catalogue_change_callbacks[uuid][key]
                        for callback in callbacks:
                            value = changed_keys[key]["newValue"]
                            model = CatalogueModel.__pydantic_validator__.validate_assignment(
                                CatalogueModel.model_construct(), key, value
                            )
                            callback(transaction.origin, getattr(model, key))
            elif len(path) == 2:
                if path[1] == "events":
                    # catalogue events changed
                    assert isinstance(event, MapEvent)
                    uuid = path[0]
                    if (
                        "add_events" in self._catalogue_change_callbacks[uuid]
                        or "remove_events" in self._catalogue_change_callbacks[uuid]
                    ):
                        added_uuids = []
                        removed_uuids = []
                        keys = event.keys  # type: ignore[attr-defined]
                        for key, val in keys.items():
                            if val["action"] == "delete":
                                removed_uuids.append(key)
                            else:
                                added_uuids.append(key)
                        if removed_uuids:
                            callbacks = self._catalogue_change_callbacks[uuid][
                                "remove_events"
                            ]
                            for callback in callbacks:
                                callback(transaction.origin, set(removed_uuids))
                        if added_uuids:
                            result = {
                                Event._from_map(self._event_maps[added_uuid], self)
                                for added_uuid in added_uuids
                            }
                            callbacks = self._catalogue_change_callbacks[uuid][
                                "add_events"
                            ]
                            for callback in callbacks:
                                callback(transaction.origin, result)
                else:
                    assert isinstance(event, MapEvent)
                    uuid, name = path
                    added = {}
                    removed = set()
                    keys = event.keys  # type: ignore[attr-defined]
                    for key, val in keys.items():
                        if val["action"] == "delete":
                            removed.add(key)
                        elif val["action"] == "add" or val["action"] == "update":
                            added[key] = val["newValue"]
                    if removed:
                        callbacks = self._catalogue_change_callbacks[uuid][
                            f"remove_{name}"
                        ]
                        for callback in callbacks:
                            callback(transaction.origin, removed)
                    if added:
                        callbacks = self._catalogue_change_callbacks[uuid][
                            f"add_{name}"
                        ]
                        for callback in callbacks:
                            callback(transaction.origin, added)

    def _events_changed(self, events: list[MapEvent], transaction: Transaction) -> None:
        for event in events:
            path = event.path  # type: ignore[attr-defined]
            if len(path) == 0:
                assert isinstance(event, MapEvent)
                keys = event.keys  # type: ignore[attr-defined]
                for uuid in keys:
                    action = keys[uuid]["action"]
                    if action == "delete":
                        for delete_callback in self._event_delete_callbacks[uuid]:
                            delete_callback(transaction.origin)
                        if uuid in self._events:
                            del self._events[uuid]
                        del self._event_delete_callbacks[uuid]
                        self._event_change_callbacks[uuid]
                        del self._event_change_callbacks[uuid]
                    elif action == "add":
                        for create_callback in self._event_create_callbacks:
                            create_callback(transaction.origin, self.get_event(uuid))
            elif len(path) == 1:
                assert isinstance(event, MapEvent)
                uuid = path[0]
                changed_keys = event.keys  # type: ignore[attr-defined]
                for key in changed_keys:
                    if key in self._event_change_callbacks[uuid]:
                        callbacks = self._event_change_callbacks[uuid][key]
                        for callback in callbacks:
                            value = changed_keys[key]["newValue"]
                            model = (
                                EventModel.__pydantic_validator__.validate_assignment(
                                    EventModel.model_construct(), key, value
                                )
                            )
                            callback(transaction.origin, getattr(model, key))
            elif len(path) == 2:
                assert isinstance(event, MapEvent)
                uuid, name = path
                added = {}
                removed = set()
                keys = event.keys  # type: ignore[attr-defined]
                for key, val in keys.items():
                    if val["action"] == "delete":
                        removed.add(key)
                    elif val["action"] == "add" or val["action"] == "update":
                        added[key] = val["newValue"]
                if removed:
                    callbacks = self._event_change_callbacks[uuid][f"remove_{name}"]
                    for callback in callbacks:
                        callback(transaction.origin, removed)
                if added:
                    callbacks = self._event_change_callbacks[uuid][f"add_{name}"]
                    for callback in callbacks:
                        callback(transaction.origin, added)

    @property
    def catalogues(self) -> set[Catalogue]:
        """
        Returns:
            The catalogues in the database.
        """
        return {
            Catalogue._from_map(self._catalogue_maps[uuid], self)
            for uuid in sorted(self._catalogue_maps.keys())
        }

    @property
    def events(self) -> set[Event]:
        """
        Returns:
            The events in the database.
        """
        return {
            Event._from_map(self._event_maps[uuid], self)
            for uuid in sorted(self._event_maps.keys())
        }

    def create_catalogue(
        self,
        *,
        name: str,
        author: str,
        uuid: UUID | str | bytes | bytearray | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
        events: Iterable[Event] | Event | None = None,
    ) -> Catalogue:
        """
        Creates a catalogue in the database.

        Args:
            name: The name of the catalogue.
            author: The author of the catalogue.
            uuid: The optional UUID of the catalogue.
            tags: The optional tags of the catalogue.
            attributes: The optional attributes of the catalogue.
            events: The initial event(s) in the catalogue.

        Returns:
            The created [Catalogue][cocat.Catalogue].
        """
        with self.transaction():
            kwargs: dict[str, Any] = {
                "name": name,
                "author": author,
            }
            if uuid is not None:
                kwargs["uuid"] = uuid
            if tags is not None:
                kwargs["tags"] = tags
            if attributes is not None:
                kwargs["attributes"] = attributes
            model = CatalogueModel(**kwargs)
            catalogue = Catalogue._new(model, self)
            self._catalogue_maps[str(model.uuid)] = catalogue._map
            if events is not None:
                if isinstance(events, Event):
                    events = [events]
                catalogue.add_events(events)

        return catalogue

    def create_event(
        self,
        *,
        start: datetime | float | str,
        stop: datetime | float | str,
        author: str,
        uuid: UUID | str | bytes | bytearray | None = None,
        tags: list[str] | None = None,
        products: list[str] | None = None,
        rating: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        """
        Creates an event in the database.

        Args:
            start: The start date of the event.
            stop: The stop date of the event.
            author: The author of the event.
            uuid: The optional UUID of the event.
            tags: The optional tags of the event.
            products: The optional products of the event.
            rating: The optional rating of the event.
            attributes: The optional attributes of the catalogue.

        Returns:
            The created [Event][cocat.Event].
        """
        with self.transaction():
            kwargs: dict[str, Any] = {
                "author": author,
                "start": start,
                "stop": stop,
            }
            if uuid is not None:
                kwargs["uuid"] = uuid
            if tags is not None:
                kwargs["tags"] = tags
            if attributes is not None:
                kwargs["attributes"] = attributes
            if products is not None:
                kwargs["products"] = products
            if rating is not None:
                kwargs["rating"] = rating
            model = EventModel(**kwargs)
            event = Event._new(model, self)
            self._event_maps[str(model.uuid)] = event._map
            return event

    def on_create_catalogue(self, callback: Callable[[Catalogue], None]) -> None:
        """
        Registers a callback to be called when a catalogue is created.

        Args:
            callback: The callback to call with the created catalogue.
        """
        self._catalogue_create_callbacks.append(partial(self._callback, callback))

    def on_create_event(self, callback: Callable[[Event], None]) -> None:
        """
        Registers a callback to be called when an event is created.

        Args:
            callback: The callback to call with the created event.
        """
        self._event_create_callbacks.append(partial(self._callback, callback))

    def get_catalogue(self, uuid_or_name: UUID | str) -> Catalogue:
        """
        Args:
            uuid_or_name: The UUID of the catalogue to get, or its name.

        Returns:
            The catalogue with the given UUID or name.
        """
        uuid_or_name = str(uuid_or_name)
        try:
            catalogue = Catalogue._from_uuid(uuid_or_name, self)
        except KeyError:
            for uuid in self._catalogue_maps:
                if self._catalogue_maps[uuid]["name"] == uuid_or_name:
                    catalogue = Catalogue._from_uuid(uuid, self)
                    break
            else:
                raise RuntimeError(
                    f"No catalogue found with name or UUID: {uuid_or_name}"
                )
        return catalogue

    def get_event(self, uuid: UUID | str) -> Event:
        """
        Args:
            uuid: The UUID of the event to get.

        Returns:
            The event with the given UUID.
        """
        uuid = str(uuid)
        try:
            return Event._from_uuid(uuid, self)
        except KeyError:
            raise RuntimeError(f"No event found with UUID: {uuid}")

    def _handle_sync_message(self, message: bytes, db: DB, init: bool = False) -> None:
        if init:
            _message = create_sync_message(self._doc)
            db._handle_sync_message(_message, self)

        message_type = message[0]
        if message_type == YMessageType.SYNC:
            try:
                reply = handle_sync_message(message[1:], self._doc)
                if reply is not None:
                    db._handle_sync_message(reply, self)
            except RuntimeError as exc:
                if str(exc) not in (
                    "Already mutably borrowed",
                    "Already in a transaction",
                ):  # pragma: nocover
                    raise

    def sync(self, db: DB) -> None:
        """
        Keeps the database in sync with another database. Mostly used for tests.

        Args:
            db: The database to keep in sync with this one.
        """
        if db in self._synced or self in db._synced:
            return

        self._synced.append(db)
        message = create_sync_message(self._doc)
        db._handle_sync_message(message, self, True)

        self._doc.observe(partial(send_update, db, self))
        db._doc.observe(partial(send_update, self, db))

    def to_dict(self) -> dict[str, Any]:
        """
        Returns:
            The database as a dictionary.
        """
        db_dict = {
            "events": sorted(
                [event.to_dict() for event in self.events],
                key=lambda event: event["uuid"],
            ),
            "catalogues": sorted(
                [catalogue.to_dict(True) for catalogue in self.catalogues],
                key=lambda catalogue: catalogue["uuid"],
            ),
        }
        events = db_dict["events"]
        for idx, event_dict in enumerate(events):
            event_dict = {
                key: event_dict[key] for key in EventModel.model_fields.keys()
            }
            events[idx] = event_dict
        return db_dict

    def to_json(self) -> str:
        """
        Returns:
            The database as a JSON string.
        """
        return json.dumps(self.to_dict())


def send_update(destination: DB, source: DB, event: TransactionEvent) -> None:
    message = create_update_message(event.update)
    destination._handle_sync_message(message, source)
