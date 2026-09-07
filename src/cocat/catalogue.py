import sys
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from pycrdt import Map
from rich.console import Console
from rich.pretty import pprint
from simpleeval import SimpleEval  # type: ignore[import-untyped]

from .base import Mixin
from .event import Event
from .models import CatalogueModel, EventModel

if sys.version_info >= (3, 11):
    from typing import Self
else:  # pragma: nocover
    from typing_extensions import Self

if TYPE_CHECKING:
    from .db import DB


@dataclass(eq=False)
class Catalogue(Mixin):
    _uuid: str
    _map: Map
    _db: "DB"
    _condition: str | None = None

    def _check_deleted(self):
        if self._uuid not in self._db._catalogue_maps:
            raise RuntimeError("Catalogue has been deleted")

    def __eq__(self, other: object) -> bool:
        self._check_deleted()
        if not isinstance(other, Catalogue):
            return NotImplemented

        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        console = Console()
        with console.capture() as capture:
            pprint(self.to_dict(), console=console, max_length=8)
        return capture.get()

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __contains__(self, event: Event) -> bool:
        return str(event.uuid) in self._map["events"]

    def __iter__(self) -> Generator[Event]:
        uuids = sorted(self._map["events"].keys())
        for uuid in uuids:
            yield self._db.get_event(uuid)

    def _get(self, name: str) -> Any:
        self._check_deleted()
        value = self._map[name]
        model = CatalogueModel.__pydantic_validator__.validate_assignment(
            CatalogueModel.model_construct(), name, value
        )
        return getattr(model, name)

    def _set(self, name: str, value: Any) -> None:
        with self._db.transaction():
            self._check_deleted()
            model = CatalogueModel.__pydantic_validator__.validate_assignment(
                CatalogueModel.model_construct(), name, value
            )
            val = getattr(model, name)
            self._map[name] = val

    def _on_change(self, name: str, callback: Callable[[Any], None]) -> None:
        self._check_deleted()
        self._db._catalogue_change_callbacks[self._uuid][name].append(
            partial(self._callback, callback)
        )

    def _on_add(self, field: str, callback: Callable[[Any], None]) -> None:
        self._check_deleted()
        self._db._catalogue_change_callbacks[self._uuid][f"add_{field}"].append(
            partial(self._callback, callback)
        )

    def _on_remove(self, field: str, callback: Callable[[list[str]], None]) -> None:
        self._check_deleted()
        self._db._catalogue_change_callbacks[self._uuid][f"remove_{field}"].append(
            partial(self._callback, callback)
        )

    @classmethod
    def _new(cls, model: CatalogueModel, db: "DB") -> Self:
        uuid = str(model.uuid)
        map = Map(
            dict(
                uuid=uuid,
                name=model.name,
                author=model.author,
                tags=Map({val: True for val in model.tags}),
                events=Map({val: True for val in model.events}),
                attributes=Map(model.attributes),
            )
        )
        self = cls(uuid, map, db)
        db._catalogues[uuid] = self
        return self

    @classmethod
    def _from_map(cls, map: Map, db: "DB") -> Self:
        uuid = map["uuid"]
        self = cls(uuid, map, db)
        db._catalogues[uuid] = self
        return self

    @classmethod
    def _from_uuid(cls, uuid: str, db: "DB") -> Self:
        map = db._catalogue_maps[uuid]
        self = cls(uuid, map, db)
        db._catalogues[uuid] = self
        return self

    def to_dict(self, event_as_uuid: bool = False) -> dict[str, Any]:
        """
        Args:
            event_as_uuid: Whether to convert events to their UUID or keep their full content.

        Returns:
            The catalogue as a dictionary.
        """
        self._check_deleted()
        dct = self._map.to_py()
        assert dct is not None
        dct["tags"] = sorted(dct["tags"].keys())
        dct["events"] = [
            uuid if event_as_uuid else Event._from_uuid(uuid, self._db).to_dict()
            for uuid in sorted(dct["events"].keys())
        ]
        dct["attributes"] = dict(sorted(dct["attributes"].items()))
        if not event_as_uuid:
            events = dct["events"]
            for idx, event_dict in enumerate(events):
                event_dict = {
                    key: event_dict[key] for key in EventModel.model_fields.keys()
                }
                events[idx] = event_dict
        return {key: dct[key] for key in CatalogueModel.model_fields.keys()}

    def on_change_name(self, callback: Callable[[str], None]) -> None:
        """
        Registers a callback to be called when the catalogue name changes.

        Args:
            callback: The callback to call with the new name.
        """
        self._on_change("name", callback)

    def on_change_author(self, callback: Callable[[Any], None]) -> None:
        """
        Registers a callback to be called when the catalogue author changes.

        Args:
            callback: The callback to call with the new author.
        """
        self._on_change("author", callback)

    def on_delete(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback to be called when the catalogue is removed from the database.

        Args:
            callback: The callback to call.
        """
        self._check_deleted()
        self._db._catalogue_delete_callbacks[self._uuid].append(
            partial(self._callback, callback)
        )

    def delete(self) -> None:
        """
        Removes the catalogue from the database.
        """
        with self._db.transaction():
            self._check_deleted()
            del self._db._catalogue_maps[self._uuid]

    def on_add_events(self, callback: Callable[[list[Event]], None]) -> None:
        """
        Registers a callback to be called when events are added to the catalogue.

        Args:
            callback: The callback to call with a list of added events.
        """
        self._on_add("events", callback)

    def on_remove_events(self, callback: Callable[[list[str]], None]) -> None:
        """
        Registers a callback to be called when events are removed from the catalogue.

        Args:
            callback: The callback to call with a list of removed event UUIDs.
        """
        self._on_remove("events", callback)

    def add_events(self, events: Iterable[Event] | Event) -> None:
        """
        Add event(s) to the catalogue.

        Args:
            events: The events to add to the catalogue.
        """
        with self._db.transaction():
            event_list = [events] if isinstance(events, Event) else events
            self._check_deleted()
            map = cast(Map, self._map["events"])
            for event in event_list:
                map[event._uuid] = True

    def set_dynamic_filter(
        self,
        condition: str | None = None,
    ) -> None:
        """
        Sets a condition that will be evaluated when accessing `catalogue.dynamic_events`.
        The condition is an expression using the event attributes and/or references to other catalogues, for instance:
        ```py
        catalogue.set_dynamic_filter(f"event.start > datetime(2025, 1, 1) and event.stop <= datetime(2026, 1, 1) and event in catalogue('my_catalogue_name_or_uuid')")
        ```

        Args:
            condition: The condition an event needs to match to be part of the catalogue.
        """
        self._condition = condition

    def remove_events(self, events: Iterable[Event] | Event) -> None:
        """
        Removes event(s) from the catalogue.

        Args:
            events: The events to remove from the catalogue.
        """
        with self._db.transaction():
            event_list = [events] if isinstance(events, Event) else events
            self._check_deleted()
            map = cast(Map, self._map["events"])
            for event in event_list:
                del map[event._uuid]

    @property
    def name(self) -> str:
        """
        Returns:
            The name of the catalogue.
        """
        return self._get("name")

    @name.setter
    def name(self, value: str) -> None:
        """
        Args:
            value: The name of the catalogue to set.
        """
        self._set("name", value)

    @property
    def dynamic_events(self) -> set[Event]:
        """
        Returns:
            The dynamic events in the catalogue, as defined by `catalogue.set_dynamic_filter(condition)`.
        """
        if not self._condition:
            return set()

        s = SimpleEval()
        s.functions = {"datetime": datetime, "catalogue": self._db.get_catalogue}
        events = set()
        for event in self._db.events:
            s.names = {"event": event}
            if s.eval(self._condition):
                events.add(event)

        return events

    @property
    def all_events(self) -> set[Event]:
        """
        Returns:
            The (static and dynamic) events in the catalogue.
        """
        return self.events.union(self.dynamic_events)

    @property
    def events(self) -> set[Event]:
        """
        Returns:
            The (static) events in the catalogue.
        """
        self._check_deleted()
        event_uuids = cast(Map, self._map["events"])
        return {
            Event._from_map(self._db._event_maps[uuid], self._db)
            for uuid in event_uuids.keys()
        }

    @events.setter
    def events(self, value: set[Event]) -> None:
        """
        Args:
            value: The events to set in the catalogue.
        """
        with self._db.transaction():
            self._check_deleted()
            events = cast(Map, self._map["events"])
            events.clear()
            for event in value:
                self.add_events(event)
