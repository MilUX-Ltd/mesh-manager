# Try it without a radio

The whole screen runs against a demo bridge on any machine with Python 3.11 or later. No
hardware, no TAK Server, nothing to undo afterwards.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
rm -f /tmp/mm-demo.sock
.venv/bin/python3 -m mesh_manager.demo /tmp/mm-demo.sock &
.venv/bin/python3 -m mesh_manager.web --config /nonexistent --socket /tmp/mm-demo.sock \
    --etc "$(mktemp -d)" --bind 127.0.0.1 --port 8095 --no-auth
```

Then open `http://127.0.0.1:8095`.

The demo bridge answers every action the real one does, with made-up devices and a few hours
of one tracker's morning, so the map draws trails and coverage, a node page draws battery and
voltage, and the health page has something to say. Every write is answered and none is sent:
there is no radio behind it.

Stop the screen with Ctrl-C, and the demo with `pkill -f mesh_manager.demo`.

## On a box with a radio

`README.md` covers the real install. It needs a Meshtastic device on USB and, if you want the
bridge to forward to TAK, a TAK Server to forward to.
