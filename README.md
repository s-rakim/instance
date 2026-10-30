# instance

Tooling for grabbing an Oracle Cloud Always Free ARM instance
(`VM.Standard.A1.Flex`) past the perpetual `Out of host capacity` error.

## What's here

| File | What it is |
|---|---|
| `hunt.sh` | Tries every 8 minutes, forever, until an instance lands, then stops and notifies you. Wraps `oci-grab.sh`. |
| `oci-hunt.service` | systemd user unit for `hunt.sh`, so the hunt survives logout and reboots. |
| `oci_grab.py` | Standalone retry daemon. `start`/`stop`/`status`/`logs`, jittered polling, exponential backoff on HTTP 429, duplicate-instance guard. Needs `pip install oci`. |
| `oci-grab.sh` | Same idea as a shell loop over the `oci` CLI, if you'd rather not use the SDK. |
| `oci-grab.user.js` | Browser-console/Tampermonkey fallback that re-clicks **Create** on the console form. Last resort. |
| `oci-grab.service` | systemd user unit, so a run survives logout and reboots. |
| `smaller-shape.patch` | Fix for the upstream project — see below. |

## Hunt every 8 minutes until it lands

Run this on a machine that's always on. It needs the `oci` CLI with a working
`oci setup config`.

```sh
git clone https://github.com/s-rakim/instance ~/instance && cd ~/instance
./oci-grab.sh discover            # prints COMPARTMENT_ID, subnets, image
cp hunt.env.example hunt.env      # paste the OCIDs in, set NTFY_URL
./hunt.sh                         # watch the first attempt or two, then Ctrl-C

mkdir -p ~/.config/systemd/user && cp oci-hunt.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now oci-hunt
loginctl enable-linger $USER
journalctl --user -u oci-hunt -f  # follow it
```

It makes one launch attempt every 8 minutes (`INTERVAL=480`), rotating through
your availability domains, and keeps going through capacity misses, rate limits,
network drops and OCI 500s. It stops on its own when:

- **an instance lands**: you get a notification with the public IP and the ssh
  command, and `.hunt-landed` is written so a restart won't launch a second one;
- **retrying can't help**: a bad OCID, missing permissions, or a used-up Always
  Free quota. You get a notification for those too.

For phone notifications, install the [ntfy](https://ntfy.sh) app, subscribe to a
long random topic name, and set `NTFY_URL=https://ntfy.sh/<that-topic>` in
`hunt.env`. `DISCORD_WEBHOOK` works as well; set either or both.

Every 8 minutes is 180 attempts a day, which is above the ~144/day of the account
in oracle-freetier-instance-creation#75 that had its limits zeroed. Raise
`INTERVAL` if that worries you; upgrading to Pay As You Go (below) usually makes
the wait much shorter anyway.

## Quick start (`oci_grab.py`)

```sh
pip install oci
oci setup config          # if you haven't already
./oci_grab.py init        # writes oci_grab.json
./oci_grab.py discover    # prints the OCIDs to paste into it
./oci_grab.py start       # detaches into the background
./oci_grab.py status
./oci_grab.py logs -f
```

Defaults to **1 OCPU / 6 GB**, which gets capacity far more often than 4 / 24.
Four 1-OCPU instances fit inside the same Always Free 4 OCPU / 24 GB allowance,
so starting small costs you nothing.

## `smaller-shape.patch`

[mohankumarpaluru/oracle-freetier-instance-creation](https://github.com/mohankumarpaluru/oracle-freetier-instance-creation)
is the most widely used solution to this problem, and it's the one to reach for
first. But `main.py:475` hardcodes the A1 shape at **2 OCPU / 12 GB** with no way
to override it from `oci.env`:

```python
shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12)
```

That asks for a materially scarcer shape than you need. The patch adds
`OCI_OCPUS` / `OCI_MEMORY_GB` env vars and defaults them to 1 / 6:

```sh
git clone https://github.com/mohankumarpaluru/oracle-freetier-instance-creation
cd oracle-freetier-instance-creation
git apply ../smaller-shape.patch
```

Also worth changing there: `ASSIGN_PUBLIC_IP=true` in `oci.env` (it ships
`false`), and run it under `oci-grab.service` rather than `setup_init.sh`, which
backgrounds with a bare `&` and dies with your SSH session.

## Before you spend a week on retry logic

Upgrade the account to **Pay As You Go**. Always Free resources stay free, and
trial accounts are refused A1 capacity that paid accounts get. Set a $1 budget
alert afterwards. Oracle publishes no policy confirming the priority difference —
it's a consistent community observation, not a documented SLA — but it's free to
try and it's the biggest lever available.

## Credits

`smaller-shape.patch` is a diff against
[mohankumarpaluru/oracle-freetier-instance-creation](https://github.com/mohankumarpaluru/oracle-freetier-instance-creation)
(MIT, © 2023 Mohan Kumar Paluru). Everything else here is original.
