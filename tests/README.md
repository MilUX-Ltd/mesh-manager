# The suites

Every file here named `test_*.py` is a suite. `tests/run.sh` runs them all and prints one line
per check; a change is not merged until the whole run prints `failing: 0`, and the run is done
by a person, not reported by an agent.

They need no radio, no TAK Server and no internet. The Meshtastic gateway is faked in
`fakegw_lib.py` with real protobuf state, so a write is proven against the device's own answer
and never against a cache; the bridge is faked in `fakebridge_lib.py` as the same unix-socket
protocol the screen speaks to on a box. Two of the suites extract the shipped bridge code and
exercise it directly.

## Running them

```bash
python3 -m pip install "meshtastic==2.7.11" "PyQRCode==1.2.1"
tests/run.sh
```

`PYTHON=/path/to/python3 tests/run.sh` picks the interpreter. In the source repository a
`.venv` beside the tree is used when one exists.

## What says `skip`

A handful of checks exercise the release tooling (the cut script, the publish script). That
tooling is part of the private source repository and does not travel with the product, so in
this tree those checks print `skip` with the reason. A skip is never a pass and never a failure:
it is a check that could not run here, said out loud.

## What is not here

One suite stays in the source repository: `test_bridge_takv2.py` decodes a packet captured
on a real kit, and that capture carries a device's uid and a position. Everything else runs
anywhere, and the same suites run on every push to this repository.
