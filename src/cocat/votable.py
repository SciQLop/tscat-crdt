import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .catalogue import Catalogue
from .db import DB, Event

if TYPE_CHECKING:
    from astropy.io.votable.tree import Field as VOField  # type: ignore[import-untyped]
    from astropy.io.votable.tree import (  # type: ignore[import-untyped]
        Table,
        VOTableFile,
    )


@dataclass
class _VOTableCocatField:
    python_type: type
    attr: dict[str, str]
    convert_vot: Callable[[Any], Any]
    convert_cocat: Callable[[Any], Any]
    cocat_name: str | None = None

    def name(self, field: "VOField") -> str:
        if self.cocat_name:
            return self.cocat_name
        return field.name

    def match(self, field: "VOField") -> bool:
        for k, v in self.attr.items():
            if field.__getattribute__(k) != v:
                return False
        return True

    def make_vot_field(self, table: "Table", name: str) -> "VOField":
        from astropy.io.votable.tree import Field as VOField

        if "name" not in self.attr:
            return VOField(table, name=name, **self.attr)
        return VOField(table, **self.attr)


class _VOTableCocatFieldSpecialDateTime(_VOTableCocatField):
    def __init__(self, attrs: dict[str, str], cocat_name: str | None = None) -> None:
        attrs.update(
            {"datatype": "char", "xtype": "dateTime", "utype": "", "arraysize": "*"}
        )
        super().__init__(datetime, attrs, datetime.isoformat, str, cocat_name)


def _vo_table_field_from(arg: type | str) -> _VOTableCocatField:
    vtf: _VOTableCocatField | None = None
    if isinstance(arg, str):
        for vtf in VOTABLE_COCAT_FIELDS:
            if vtf.cocat_name == arg:
                break
    else:
        for vtf in VOTABLE_COCAT_FIELDS:
            if vtf.cocat_name is None and vtf.python_type is arg:
                break

    assert vtf is not None
    return vtf


VOTABLE_COCAT_FIELDS = [
    _VOTableCocatFieldSpecialDateTime(
        {"name": "Start Time", "ID": "TimeIntervalStart", "ucd": "time.start"}, "start"
    ),
    _VOTableCocatFieldSpecialDateTime(
        {"name": "Stop Time", "ID": "TimeIntervalStop", "ucd": "time.end"}, "stop"
    ),
    _VOTableCocatFieldSpecialDateTime({}),
    _VOTableCocatField(
        int,
        {"datatype": "long"},
        lambda x: 0 if x is None else x,
        lambda x: None if x == 0 else x,
        "rating",
    ),
    _VOTableCocatField(int, {"datatype": "long"}, int, int),
    _VOTableCocatField(float, {"datatype": "double"}, float, float),
    _VOTableCocatField(bool, {"datatype": "boolean"}, bool, bool),
    _VOTableCocatField(
        list,
        {"datatype": "char", "arraysize": "*", "utype": "json"},
        json.dumps,
        json.loads,
    ),
    _VOTableCocatField(
        list,
        {"datatype": "char", "arraysize": "*", "name": "products"},
        json.dumps,
        json.loads,
        "products",
    ),
    _VOTableCocatField(
        list,
        {"datatype": "char", "arraysize": "*", "name": "tags"},
        json.dumps,
        json.loads,
        "tags",
    ),
    _VOTableCocatField(
        str, {"datatype": "char", "arraysize": "*"}, str, str
    ),  # last item, catch all strings
]

ATTRIBUTES = [
    ("start", _vo_table_field_from("start")),
    ("stop", _vo_table_field_from("stop")),
    ("author", _vo_table_field_from(str)),
    ("uuid", _vo_table_field_from(str)),
    ("tags", _vo_table_field_from(list)),
    ("products", _vo_table_field_from(list)),
    ("rating", _vo_table_field_from("rating")),
]

STANDARD_FIELDS = [v[0] for v in ATTRIBUTES]


def export_votable(catalogues: Sequence[Catalogue] | Catalogue) -> "VOTableFile":
    from astropy.io.votable.tree import Resource, TableElement, VOTableFile

    votable = VOTableFile()

    catalogue_list = (
        [catalogues] if isinstance(catalogues, Catalogue) else list(catalogues)
    )

    if len(catalogue_list) == 1:
        catalogue = catalogue_list[0]
        votable.description = f"Contact:{catalogue.author};Name:{catalogue.name}"

    resource = Resource()
    votable.resources.append(resource)

    attributes = list(ATTRIBUTES)

    all_attributes = set(
        attribute
        for catalogue in catalogue_list
        for attribute in catalogue.attributes.keys()
    )
    common_attributes = set.intersection(
        *[set(catalogue.attributes) for catalogue in catalogue_list]
    )

    for catalogue in catalogue_list:
        table = TableElement(votable, name=catalogue.name.replace(" ", "_"))
        resource.tables.append(table)

        all_attributes = set(
            attribute
            for event in catalogue.events
            for attribute in event.attributes.keys()
        )
        common_attributes = set.intersection(
            *[set(event.attributes) for event in catalogue.events]
        )

        # check that all attributes are present in every event
        if all_attributes != common_attributes:
            raise ValueError(
                "Export VOTable: not all attributes are present in all events "
                + f"{tuple(sorted(all_attributes - common_attributes))}"
            )

        for attr in sorted(all_attributes):
            # check that the type of all attribute values is identical
            attrs_value_types = list(
                set(type(event.attributes[attr]) for event in catalogue.events)
            )
            if len(attrs_value_types) != 1:
                raise ValueError(
                    "Export VOTable: not all value types are "
                    + f"identical for all events for attribute {attr}"
                )

            attributes.append((attr, _vo_table_field_from(attrs_value_types[0])))

        table.fields.extend(
            [vtf.make_vot_field(votable, name) for name, vtf in attributes]
        )

        table.create_arrays(len(catalogue.events))
        for i, event in enumerate(catalogue.events):
            values = []
            for k, vtf in attributes:
                if k in STANDARD_FIELDS:
                    v = getattr(event, k)
                else:
                    v = event.attributes.get(k, "")
                if isinstance(v, set):
                    v = list(v)
                values.append(vtf.convert_vot(v))
            table.array[i] = tuple(values)

    return votable


def import_votable(
    votable: "VOTableFile", db: DB, table_name: str | None = None
) -> None:
    author = "VOTable Import"
    table_name = table_name or f"Imported Catalogue from {datetime.now()}"

    if votable.description:
        for line in str(votable.description).split(";"):
            line = line.strip()
            values = line.split(":", 1)
            if len(values) != 2:
                continue  # pragma: nocover
            property, value = values
            value = value.strip()
            if property == "Contact":
                author = value
            elif property == "Name":
                table_name = value

    db_dict: dict[str, list[dict[str, Any]]] = {
        "catalogues": [],
        "events": [],
    }

    for i, table in enumerate(votable.iter_tables()):
        required_field_names: list[str] = ["Start Time", "Stop Time"]
        fields_vs_index: dict[tuple[int, str], _VOTableCocatField] = {}

        for j, field in enumerate(table.fields):
            if field.name in required_field_names:
                required_field_names.remove(field.name)

            # match field-event-signature to get converters/name
            for vtf in VOTABLE_COCAT_FIELDS:
                if vtf.match(field):
                    fields_vs_index[(j, vtf.name(field))] = vtf
                    break
            else:  # pragma: nocover
                raise ValueError(
                    f"VOTable import: cannot import field: {field.ID}, {field.name}, {field.datatype},"
                    + f" {field.xtype}"
                )

        if len(votable.resources[0].tables) == 1:
            this_name = table_name
        else:  # pragma: nocover
            this_name = f"{table_name}_{i}"

        if len(required_field_names) > 0:  # pragma: nocover
            raise ValueError(
                f"VOTable import: required fields are missing for table {this_name}"
            )

        catalogue: dict[str, Any] = {
            "name": this_name,
            "author": author,
            "events": [],
        }

        has_author_field = any(f[1] == "author" for f in fields_vs_index)
        has_uuid_field = any(f[1] == "uuid" for f in fields_vs_index)

        for el in table.array:
            event: dict[str, Any] = {"attributes": {}}
            if not has_author_field:  # pragma: nocover
                event["author"] = author

            if not has_uuid_field:  # pragma: nocover
                event["uuid"] = str(uuid4())

            for (index, name), vtf in fields_vs_index.items():
                if name in STANDARD_FIELDS:
                    event[name] = vtf.convert_cocat(el[index])
                else:
                    event["attributes"][name] = vtf.convert_cocat(el[index])

            if not any(event["uuid"] == e["uuid"] for e in db_dict["events"]):
                db_dict["events"].append(event)

            catalogue["events"].append(event["uuid"])  # type: ignore

        db_dict["catalogues"].append(catalogue)

    with db.transaction():
        for _event in db_dict["events"]:
            db.create_event(**_event)
        for _catalogue in db_dict["catalogues"]:
            events = _catalogue.pop("events", [])
            cat = db.create_catalogue(**_catalogue)
            cat.add_events([Event._from_uuid(uuid, db) for uuid in events])


def import_votable_file(
    file_path: str | Path, db: DB, table_name: str | None = None
) -> None:
    """
    Imports a VOTable file into a database.

    Args:
        file_path: The VOTable file path.
        db: The database into which to import the VOTable.
    """
    from astropy.io.votable import parse  # type: ignore[import-untyped]

    import_votable(parse(file_path), db, table_name=table_name)


def import_votable_str(xml_content: str, db: DB, table_name: str | None = None) -> None:
    """
    Imports a VOTable XML string into a database.

    Args:
        xml_content: The VOTable content as an XML string.
        db: The database into which to import the VOTable.
    """
    from astropy.io.votable import parse  # type: ignore[import-untyped]

    import_votable(parse(BytesIO(xml_content.encode())), db, table_name=table_name)


def export_votable_file(
    catalogues: Sequence[Catalogue] | Catalogue, file_path: str | Path
) -> None:
    """
    Exports catalogues to a VOTable file.

    Args:
        catalogues: The catalogue(s) to export.
        file_path: The path to the exported file.
    """
    with open(file_path, "wb") as f:
        export_votable(catalogues).to_xml(f)


def export_votable_str(catalogues: Sequence[Catalogue] | Catalogue) -> str:
    """
    Exports catalogues as a VOTable XML string.

    Args:
        catalogues: The catalogue(s) to export.

    Returns:
        The VOTable as an XML string.
    """
    content = BytesIO()
    export_votable(catalogues).to_xml(content)
    return content.getvalue().decode()
