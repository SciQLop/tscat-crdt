import sys
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from pycrdt import Map
from rich.console import Console
from rich.pretty import pprint

from .base import Mixin
from .models import EventModel

if sys.version_info >= (3, 11):
    from typing import Self
else:  # pragma: nocover
    from typing_extensions import Self

if TYPE_CHECKING:
    from .db import DB


@dataclass(eq=False)
class Event(Mixin):
    _uuid: str
    _map: Map
    _db: "DB"

    def _check_deleted(self):
        if self._uuid not in self._db._event_maps:
            raise RuntimeError("Event has been deleted")

    def __eq__(self, other: object) -> bool:
        self._check_deleted()
        if not isinstance(other, Event):
            return NotImplemented

        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        console = Console()
        with console.capture() as capture:
            pprint(self.to_dict(), console=console, max_length=8)
        return capture.get()

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __iter__(self) -> Generator[datetime]:
        yield self.start
        yield self.stop

    def _get(self, name: str) -> Any:
        self._check_deleted()
        value = self._map[name]
        model = EventModel.__pydantic_validator__.validate_assignment(
            EventModel.model_construct(), name, value
        )
        return getattr(model, name)

    def _set(
        self, name: str, value: Any, func: Callable[[Any], Any] | None = None
    ) -> None:
        with self._db.transaction():
            self._check_deleted()
            model = EventModel.__pydantic_validator__.validate_assignment(
                EventModel.model_construct(), name, value
            )
            val = getattr(model, name)
            if func is not None:
                val = func(val)
            self._map[name] = val

    def _on_change(self, name: str, callback: Callable[[Any], None]) -> None:
        self._check_deleted()
        self._db._event_change_callbacks[self._uuid][name].append(
            partial(self._callback, callback)
        )

    def _on_add(self, field: str, callback: Callable[[Any], None]) -> None:
        self._check_deleted()
        self._db._event_change_callbacks[self._uuid][f"add_{field}"].append(
            partial(self._callback, callback)
        )

    def _on_remove(self, field: str, callback: Callable[[list[str]], None]) -> None:
        self._check_deleted()
        self._db._event_change_callbacks[self._uuid][f"remove_{field}"].append(
            partial(self._callback, callback)
        )

    @classmethod
    def _new(cls, model: EventModel, db: "DB") -> Self:
        uuid = str(model.uuid)
        map = Map(
            dict(
                uuid=uuid,
                start=str(model.start),
                stop=str(model.stop),
                author=model.author,
                tags=Map({val: True for val in model.tags}),
                products=Map({val: True for val in model.products}),
                rating=model.rating,
                attributes=Map(model.attributes),
            )
        )
        self = cls(uuid, map, db)
        db._events[uuid] = self
        return self

    @classmethod
    def _from_map(cls, map: Map, db: "DB") -> Self:
        uuid = map["uuid"]
        self = cls(uuid, map, db)
        db._events[uuid] = self
        return self

    @classmethod
    def _from_uuid(cls, uuid: str, db: "DB") -> Self:
        map = db._event_maps[uuid]
        self = cls(uuid, map, db)
        db._events[uuid] = self
        return self

    def to_dict(self) -> dict[str, Any]:
        """
        Returns:
            The event as a dictionary.
        """
        self._check_deleted()
        dct = self._map.to_py()
        assert dct is not None
        dct["tags"] = sorted(dct["tags"].keys())
        dct["products"] = sorted(dct["products"].keys())
        dct["attributes"] = dict(sorted(dct["attributes"].items()))
        return {key: dct[key] for key in EventModel.model_fields.keys()}

    def on_change_author(self, callback: Callable[[Any], None]) -> None:
        """
        Registers a callback to be called when the event author changes.

        Args:
            callback: The callback to call with the new author.
        """
        self._on_change("author", callback)

    def on_change_start(self, callback: Callable[[datetime], None]) -> None:
        """
        Registers a callback to be called when the event start date changes.

        Args:
            callback: The callback to call with the new start date.
        """
        self._on_change("start", callback)

    def on_change_stop(self, callback: Callable[[datetime], None]) -> None:
        """
        Registers a callback to be called when the event stop date changes.

        Args:
            callback: The callback to call with the new stop date.
        """
        self._on_change("stop", callback)

    def on_change_range(self, callback: Callable[[datetime, datetime], None]) -> None:
        """
        Registers a callback to be called when the event start and/or stop dates change.

        Args:
            callback: The callback to call with the new range.
        """
        range: list[None | datetime] = [None, None]

        def callback_start(start: datetime) -> None:
            if range != [start, self.stop]:
                range[0], range[1] = start, self.stop
                callback(start, self.stop)

        def callback_stop(stop: datetime) -> None:
            if range != [self.start, stop]:
                range[0], range[1] = self.start, stop
                callback(self.start, stop)

        self._on_change("start", callback_start)
        self._on_change("stop", callback_stop)

    def on_change_rating(self, callback: Callable[[Any], None]) -> None:
        """
        Registers a callback to be called when the event rating changes.

        Args:
            callback: The callback to call with the new rating.
        """
        self._on_change("rating", callback)

    def on_delete(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback to be called when the event is removed from the database.

        Args:
            callback: The callback to call.
        """
        self._check_deleted()
        self._db._event_delete_callbacks[self._uuid].append(
            partial(self._callback, callback)
        )

    def delete(self):
        """
        Removes the event from the database.
        """
        with self._db.transaction():
            self._check_deleted()
            del self._db._event_maps[self._uuid]
            for uuid, catalogue in self._db._catalogue_maps.items():
                catalogue_events = catalogue["events"]
                if self._uuid in catalogue_events:
                    del catalogue_events[self._uuid]

    @property
    def start(self) -> datetime:
        """
        Returns:
            The start date of the event.
        """
        return self._get("start")

    @start.setter
    def start(self, value: datetime) -> None:
        """
        Args:
            value: The start date of the event to set.
        """
        self._set("start", value, str)

    @property
    def stop(self) -> datetime:
        """
        Returns:
            The stop date of the event.
        """
        return self._get("stop")

    @stop.setter
    def stop(self, value: datetime) -> None:
        """
        Args:
            value: The stop date of the event to set.
        """
        self._set("stop", value, str)

    @property
    def range(self) -> tuple[datetime, datetime]:
        """
        Returns:
            The start and stop dates of the event.
        """
        return self._get("start"), self._get("stop")

    @range.setter
    def range(self, value: tuple[datetime, datetime]) -> None:
        """
        Args:
            value: The start and stop dates of the event to set.
        """
        with self._db.transaction():
            start, stop = value
            self._set("start", start, str)
            self._set("stop", stop, str)

    @property
    def rating(self) -> int:
        """
        Returns:
            The rating of the event.
        """
        return self._get("rating")

    @rating.setter
    def rating(self, value: int) -> None:
        """
        Args:
            value: The rating of the event to set.
        """
        self._set("rating", value)

    @property
    def products(self) -> set[str]:
        """
        Returns:
            The products of the event.
        """
        return set(self._get_from_map("products"))

    @products.setter
    def products(self, value: set[str]) -> None:
        """
        Args:
            value: The products of the event to set.
        """
        products = {val: True for val in value}
        self._set_in_map("products", products)

    def on_add_products(self, callback: Callable[[set[str]], None]) -> None:
        """
        Registers a callback to be called when products are added.

        Args:
            callback: The callback to call with the added products.
        """

        def _callback(values: dict[str, Any]) -> None:
            callback(set(values))

        self._on_add("products", _callback)

    def on_remove_products(self, callback: Callable[[list[str]], None]) -> None:
        """
        Registers a callback to be called when products are removed.

        Args:
            callback: The callback to call with the removed products.
        """
        self._on_remove("products", callback)

    def add_products(self, keys: Iterable[str] | str) -> None:
        """
        Adds product(s) to the event.

        Args:
            keys: The products to add to the event.
        """
        self._add_keys("products", keys)

    def remove_products(self, keys: Iterable[str] | str) -> None:
        """
        Removed product(s) from the event.

        Args:
            keys: The products to remove from the event.
        """
        self._remove_keys("products", keys)
