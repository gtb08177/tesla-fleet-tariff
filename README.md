# Tesla Fleet Tariff

A Home Assistant custom integration that lets HA set your **Powerwall's
utility rate plan** (the Tesla app's *Settings → Utility Rate Plan*). You use
it as a lever: during a demand session from your supplier (e.g. an Octopus
Energy Power Down or Power Up session), the plan is changed so Time-Based
Control exports or charges, and then your normal plan comes back.

It uses the login of the core **Tesla Fleet** integration (no separate
sign-in) and Tesla's `time_of_use_settings` API.

## How it works

* **Normal plan:** your everyday rate plan, stored once in HA.
* **Sessions (events):** time windows such as "Thu 18:00–19:00". A session
  normally **reuses one of your normal plan's labels**, e.g. `Peak` for an
  export session, `Super Off-Peak` for a cheap/free session. It then takes
  that label's prices and the Tesla app keeps its colours.
* **Relabel (optional):** e.g. Peak → Mid-Peak for the rest of the session's
  day, so the session is the only Peak window.
* **Drain (optional, for cheap/free sessions):** the same length of time right
  before each block of sessions gets another label (usually Peak), so the
  battery exports first and refills during the session.
  * One hour at 13:00 drains 12:00–13:00.
  * 13:00–15:00 drains 11:00–13:00.
  * 11:00–12:00 plus 13:00–14:00 drains 10:00–11:00 and 12:00–13:00.
  * A drain never overlaps an earlier session.
  * Set a **drain length** (e.g. 90 minutes) for a fixed length instead; 0
    (the default) matches each block's length.
* **Timing:**
  * A session for today goes into the plan straight away.
  * A session for a later day goes in at 00:00 that day, or as soon as it's
    known with `activate: now`.
  * Once the **last** session of the day ends, the normal plan is pushed back.
  * Windows that have already passed are never removed on their own, because
    that would waste a Tesla plan change.
* **Only when needed:** the plan sent to Tesla is recalculated whenever
  something changes, but **only sent when it's different**. Re-checking a
  session list every few minutes costs nothing.
* **Plan name in the app:** "My plan (Normal)", "My plan (Power Down)",
  "My plan (Power Up + Power Down)" and so on, built from your plan name and
  the session names.

```
Wed 10:00  session joined for Wed 18:00-19:00 -> push: Peak 18:00-19:00, 20:30-23:30 Mid-Peak
Wed 10:00  session joined for Thu 17:30-18:30 -> held (not today)
Wed 10:30  supplier refresh, nothing new      -> no Tesla call
Wed 19:00  last session of the day ends       -> push the normal plan
Thu 00:00  Thursday begins                    -> push: Peak 17:30-18:30, ...
Thu 18:30  session ends                       -> push the normal plan
```

### Tesla app schedules

By default the Tesla app keeps **one all-week schedule**. While a session is
live, its changes appear on every day's rows. Only the session's own day ever
runs with them, because the normal plan is back before the next day. Overnight
blocks show as one row (e.g. Super Off-Peak 23:30–05:30).

If your weekday and weekend rates differ, use separate schedules:

```yaml
tesla_fleet_tariff:
  schedule: weekday_weekend
```

A session then only changes the Mon–Fri or the Sat–Sun schedule. The Tesla
app only supports these two groups, so per-day changes aren't possible.

## Install

1. Copy `custom_components/tesla_fleet_tariff` into `/config/custom_components/`.
   The **Samba share** add-on makes this a drag-and-drop from your computer.
   The integration's `brand/` folder holds the Tesla icon (the same one Tesla
   Fleet uses), so it shows on the Integrations page instead of a blank icon.
2. Add this to `configuration.yaml`, then do a **full restart**:
   ```yaml
   tesla_fleet_tariff:
   ```
3. The Tesla Fleet login needs the `energy_cmds` permission. If HA can already
   change Operation Mode or Backup Reserve, it has it.
4. Copy the contents of `blueprints/` into `/config/blueprints/`, keeping the
   folder structure.
5. Create a script from the **Powerwall - Set normal rate plan** blueprint
   with your rates, and run it once. Check the Tesla app shows your plan as
   "<name> (Normal)".
6. Create automations from the **Powerwall - Follow demand sessions**
   blueprint, one per session type (see below). Optionally also add the notify
   and forced-charging blueprints.

`examples/` has the same setup as hand-written YAML, with Ryan's entity IDs,
plus `first_test.yaml` for a step-by-step first test.

## Blueprints

| Blueprint | Type | What it's for |
|---|---|---|
| Powerwall - Set normal rate plan | script | Your normal plan: Powerwall, plan name, utility provider (as shown in the Tesla app; defaults to Home Assistant) and rate rows (start, label, buy, sell) |
| Powerwall - Follow demand sessions | automation | Keeps the plan in step with a session source. One automation per session type |
| Powerwall - Notify when the rate plan changes | automation | Your notify actions, with `{{ notify_title }}` and `{{ plan_message }}` |
| Powerwall - Export session overrides forced charging | automation | Stops a forced-charge mode (e.g. Intelligent Go dispatch charging) during an export session |

**Session sources** for "Follow demand sessions":

* **An entity with a list of sessions:**
  * Octopus Energy Power Down: `event.octopus_energy_<account>_octoplus_power_down_events`, attribute `joined_events`
  * Octopus Energy Power Up: `event.octopus_energy_<account>_octoplus_power_up_events`, attribute `events`
* **Any calendar:** e.g. `calendar.octopus_energy_<account>_octoplus_power_down`,
  another supplier's calendar, or a **Local Calendar** where you add sessions
  by hand. All-day entries are ignored. Calendars are checked every 15 minutes
  for new or removed events.

**Typical settings:**

| Session type | Label | Relabel | Drain with |
|---|---|---|---|
| Export / saving (e.g. Octopus Power Down) | Peak | `{"Peak": "Mid-Peak"}` | (empty) |
| Cheap / free (e.g. Octopus Power Up) | Super Off-Peak | `{}` | Peak |

## Notifications

Every push fires a `tesla_fleet_tariff_pushed` event. It carries:
* `message`, ready to show: "Pushed: Normal rate plan", or "Pushed: Power
  Down (18:00 - 19:00)"
* `summary`, e.g. "Power Up (12:00 - 13:00 drain, 13:00 - 14:00)"
* `plan_name`, `kind`, `reason`, `events` and `device_id`

A minimal hand-written notification:

```yaml
triggers:
  - trigger: event
    event_type: tesla_fleet_tariff_pushed
actions:
  - action: notify.mobile_app_<your_phone>
    data:
      title: Powerwall - Rate Plan Changed
      message: "{{ trigger.event.data.message }}"
```

There's no notification when nothing changed, when a session for a later day
is first seen (it arrives at 00:00), or when a push fails. A failed push is
retried every 5 minutes and shown by `get_status` as `last_error`.

## Actions

| Action | What it does |
|---|---|
| `set_base_tariff` | Store the normal plan from `rates` (or a raw `tariff`) and push it, with any live sessions on top |
| `capture_base_tariff` | Read the plan currently in the Tesla app and store it as the normal plan |
| `sync_events` | Make one source's sessions match an entity's list attribute, or an `events` list (e.g. from `calendar.get_events`). Handles new, changed and removed sessions, and ignores past ones. Keeps what it has if the entity is unavailable |
| `add_event` / `remove_event` | Add or remove a single window, e.g. a test or a manual override. Removing also removes its drain |
| `clear_events` | Drop all sessions (or one source's) and force a push of the normal plan |
| `get_status` | Plan name, sessions (whether each is in the plan or running), last push, last error and next check |

Session options for `sync_events` and `add_event`:
* `label` (one of your normal plan's labels) or `buy_price` / `sell_price`
  (export defaults to the import price, because Tesla requires import ≥ export)
* `relabel`, `pre_label`, `pre_minutes` and `activate`
* `name`, which appears in notifications and the plan name

Every action that builds a plan accepts `dry_run: true`, and returns the plan
without sending it.

In **Developer tools → Actions**, use **YAML mode** and put `device_id` under
`data:`. The visual form's device picker sends it as a target, which these
actions don't accept.

## Caveats

* **Tesla rate limits:** a session day normally costs two plan changes (one
  in, one out). An old Tesla page listed a limit on energy-settings changes.
  The current limits page doesn't, but keep an eye on `last_error` at first.
* **Behaviour isn't forced:** Time-Based Control decides what to do with the
  prices. The Powerwall needs operation mode `autonomous` and export set to
  allow the battery. Anything that switches it to backup mode during a session
  stops the export (see the forced-charging blueprint).
* **Free sessions aren't priced at £0** when they reuse a label, so the
  Powerwall sees your Super Off-Peak price. Use `buy_price: 0` instead to show
  them as free. That needs a label your plan doesn't use, so a new colour
  appears in the app.
* **Export capped at import:** if a slot's export price is higher than its
  import price, export is capped at the import price. Otherwise Tesla would
  raise the import price to match.
* Sessions are limited to 24 hours. Times use HA's time zone, which is assumed
  to be the site's.

## Tests

```bash
uv venv -p 3.13 .venv
uv pip install -p .venv homeassistant tesla-fleet-api pytest pytest-homeassistant-custom-component pytest-timeout
.venv/bin/python -m pytest -q tests
```

Every plan is checked against Tesla's own offline resolver
(`tesla_fleet_api.tariff.get_tariff_periods`). The integration and blueprint
tests run in a real Home Assistant core, using a fake Tesla site, fake Octopus
entities and a fake calendar.
