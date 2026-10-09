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
  either **reuses one of your normal plan's labels** (e.g. `Peak` for an
  export session, taking that label's prices and colour) or has **its own
  prices** (e.g. £0 for free electricity, which gets a label your plan
  doesn't use).
* **Only export window (optional):** e.g. Peak → Mid-Peak for the rest of
  the session's day, so the session is the only Peak window.
* **Empty the battery first (optional, for free sessions):** a window right
  before each block of sessions is Peak, so the battery exports first and
  refills during the session.
  * **Length:** blank (default) = the same length as the block, or a
    number of minutes.
  * With the length left blank:
    * One hour at 13:00 drains 12:00–13:00.
    * 13:00–15:00 drains 11:00–13:00.
    * 11:00–12:00 plus 13:00–14:00 drains 10:00–11:00 and 12:00–13:00.
  * With 90: 13:00–15:00 drains 11:30–13:00.
  * A drain never overlaps an earlier session.
* **Timing:**
  * A session for today goes into the plan straight away.
  * A session for a later day goes in at 00:00 that day, or as soon as it's
    known with `activate: now`.
  * Once the **last** session of the day ends, the normal plan is pushed back.
* **The past is frozen:** a window that has already ended (a drain, or the
  first of two back-to-back sessions) stays in the plan until the day's last
  session ends, whatever the session list says. Re-checks therefore never
  push just because time moved on. Changes to current or future sessions
  still push: a session joined later that day, or one pulled before it
  starts, during its drain or while it's running.
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

**Settings** (in sections, most people only need the first three):

| Section | Setting | What it does |
|---|---|---|
| Sessions | Where the sessions come from, Name | The entity or calendar, and the name shown in notifications and the Tesla app |
| During a session | **Session type** | *Export* (e.g. Power Down): the session is Peak. *Free electricity* (e.g. Power Up): the session is £0 to buy and sell |
| | **Make it the only export window that day** (on) | Your normal Peak becomes Mid-Peak on the session's day |
| Before a free session | **Empty the battery first** (on), **For how long** (blank = same as the session) | A Peak window just before the session |
| Advanced | Which list to read (automatic), when later days go in, a label instead of the session type, the drain label, a custom relabel map | Rarely needed. Filled-in advanced fields win over the simple ones |

For free sessions to show as **Super Off-Peak** in the Tesla app, don't use
Super Off-Peak in your normal plan (e.g. call your overnight rate Off-Peak).
Otherwise the free window shows under whichever label is spare.

Automations made with the 0.1 blueprint keep working: their `label`,
`relabel`, `pre_label` and `pre_minutes` settings are now the advanced fields.
One difference: an empty `relabel` used to mean "no change" and now means
"use the switch", which is on by default. To keep every Peak window on
session days, turn **Make it the only export window that day** off.

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
| `sync_events` | Make one source's sessions match an entity's list attribute, or an `events` list (e.g. from `calendar.get_events`). Handles new, changed and removed sessions. Windows that have already ended are left as they are. Keeps what it has if the entity is unavailable |
| `add_event` / `remove_event` | Add or remove a single window, e.g. a test or a manual override. Removing also removes its drain |
| `clear_events` | Drop all sessions (or one source's) and force a push of the normal plan |
| `get_status` | Plan name, sessions (whether each is in the plan or running), last push, last error and next check |

Session options for `sync_events` and `add_event`:
* `label` (one of your normal plan's labels) or `buy_price` / `sell_price`
  (export defaults to the import price, because Tesla requires import ≥ export)
* `relabel`, `pre_label`, `pre_minutes` (1–720; leave out for the same length as the block) and `activate`
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
* **Free sessions and labels:** a £0 session needs a label your normal plan
  doesn't use. The blueprint's *Free electricity* type does this for you; if
  you reuse a label instead (e.g. `label: Super Off-Peak`), the Powerwall sees
  that label's normal price rather than £0.
* **Export prices:** Time-Based Control exports when export pays. If you're
  paid the same at all times but want to choose when it exports, set export
  to £0 everywhere except the windows you want (e.g. Peak).
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
