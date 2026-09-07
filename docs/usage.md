## Quickstart

### User API

Cocat consists of the following objects:

- [DB][cocat.DB]: the object representing the database, which allows creating events and catalogues, modifying them and being notified of the changes.
- [Event][cocat.Event]: an object representing an event, which allows modifying its properties and being notified of the changes.
- [Catalogue][cocat.Catalogue]: an object representing a catalogue, which allows modifying its properties and being notified of the changes. A catalogue contains a set of events, but events are in general independent of catalogues.

User A on one machine could start creating catalogues and events:

```py
from cocat import DB

db0 = DB()
event0 = db0.create_event(
    start="2025-01-31",
    stop="2026-01-31",
    author="John",
    attributes={"foo": "bar"},
)
catalogue0 = db0.create_catalogue(name="cat0", author="Paul", attributes={"baz": 3})
catalogue0.add_events(event0)
event0.stop = "2026-01-30"
```

Up to now their data is local, but they could share the database using a web server. Here is how such a web server can be set up:

```py
from anyio import run, sleep_forever
from wire_websocket import AsyncWebSocketServer


async def main():
    async with AsyncWebSocketServer(host="localhost", port=8000):
        await sleep_forever()


run(main)
```

And here is how user A can connect to this server:

```py
from anyio import run, sleep_forever
from wire_websocket import AsyncWebSocketClient


async def main():
    async with AsyncWebSocketClient(doc=db0.doc, host="http://localhost", port=8000):
        await sleep_forever()


run(main)
```

User B on another machine could connect their database to user A's and synchronize their catalogues and events:

```py
from anyio import run, sleep_forever
from cocat import DB
from wire_websocket import AsyncWebSocketClient


async def main():
    db1 = DB()
    async with AsyncWebSocketClient(doc=db1.doc, host="http://localhost", port=8000):
        print(db1.catalogues)
        print(db1.events)


run(main)
```

### High-level API

A higher-level API is also provided, which is more suited to interactive workflows with
a Python REPL or a Jupyter notebook. This API is not async, and as such it requires manual
synchronization using `refresh()` and `save()` to pull the remote changes and push the local changes,
respectively.

```py
from cocat import (
    create_catalogue,
    create_event,
    log_in,
    log_out,
    refresh,
    save,
    set_config,
)

set_config(host="http://localhost", port=8000, file_path="updates.y")
log_in("paul@example.com", "my_password")
refresh()

catalogue0 = create_catalogue(name="cat0", author="Paul")
save()

catalogue1 = get_catalogue("56c935a2-0109-49c0-b91d-dd5b2de8feef")

event0 = create_event(
    start="2025-01-31",
    stop="2026-01-31",
    author="John",
)
save()

refresh()
event1 = get_event("497393db-7dd6-4d7b-9ff1-8a8155bfed54")

log_out()
```

### CLI

A command-line interface allows to launch a server and manage users.
Because the server can only be accessed by authenticated users, one has to create users first:

```bash
cocat create-user --email "paul@example.com" --password "my_password" --db-path "users.db"
```

By default users have no permission to connect to any room. To allow a user in a room:

```bash
cocat add-user-to-room --email "paul@example.com" --room-id "my_room" --db-path "users.db"
```

If you wish to remove this user from a room at a later time:

```bash
cocat remove-user-from-room --email "paul@example.com" --room-id "my_room" --db-path "users.db"
```

Then the server can be launched:

```bash
cocat serve --host "127.0.0.1" --port 8000 --update-dir "update_dir" --db-path "users.db"
```

The server will listen to `http://127.0.0.1:8000` and the updates will be stored
in the `update_dir` directory, with one file per room. Rooms are identified with
the path a client connects to. For instance, if a client connects to
`http://127.0.0.1:8000/my_room`, then the room ID is `my_room` and the corresponding
file path is `update_dir/my_room.y`.
