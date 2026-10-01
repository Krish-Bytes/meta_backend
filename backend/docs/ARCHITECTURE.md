# Architecture: presence at WhatsApp scale

## What “online” and “last seen” actually mean

They are **not** the same signal.

| Signal | Source of truth | Written often? | Shown when |
| --- | --- | --- | --- |
| Online | At least one **live session** | No. Derived from session set | Any session is inside TTL |
| Last seen | Timestamp of **transition to zero sessions** | Once per real offline | User is offline and privacy allows it |

Heartbeats must **never** be written to disk or fanned out to contacts. At millions of concurrent users, a 15–30s ping would create a write storm and a notification storm if you treated it as a presence event.

## Connection path

```
Phone  --WebSocket / Noise socket-->  Edge gateway (sticky)
                                      |  session register
                                      v
                               Presence service
                                      |  pub/sub on STATE CHANGE only
                                      v
                               Online watchers' gateways --> their phones
```

This prototype collapses gateway + presence into one process. The **state machine is the same** one you would run in Redis.

## Heartbeats

Client interval `H` (demo 2s, production ~15–30s, plus traffic on an already-open socket).

Server session TTL `T ≈ 3H` (demo 6s). If `now - last_heartbeat > T`, the session is dead.

Why both TCP close **and** TTL?

- Clean leave / airplane mode / app killed gracefully: the gateway sees a close and can start an offline debounce immediately.
- Phone dies, kernel freeze, NAT blackhole: **no close frame**. Only TTL (and maybe TCP keepalive) sees it. That is the “without excessive delay” bound: worst-case detection ≈ `T`.

## Session state

A user is a set of sessions (`phone`, `web`, `tablet`).

- Connect: add session. If the set went `0 → 1`, emit **online**.
- Extra device: no emit.
- Heartbeat: `EXPIRE session T` (here: bump `last_heartbeat`). No emit.
- Last session gone: set `last_seen = now` (after debounce on clean close) and emit **offline**.

Production sketch (Redis):

```
SADD  user:{id}:sessions {session}
SET   session:{sid} {user}  EX {T}     # heartbeat = EXPIRE
# keyspace notification or a sweeper when session keys vanish
SET   user:{id}:last_seen {ts}         # only on 1->0
```

Last-seen is durable. Session keys are ephemeral.

## Sudden disconnect

| Event | Detection | Delay |
| --- | --- | --- |
| User locks phone / leaves app, socket closes | Gateway `close` | Debounce ~0.5–2s (demo 0.8s) |
| Radio blip, immediate reconnect | Debounce window | **No offline event** |
| Battery death | Missed heartbeats | Up to TTL |
| One of two devices dies | Remaining session | User stays online |

Debounce is how you avoid “online / last seen just now / online” flashing when a train goes through a tunnel.

## Push, not poll

Contacts do **not** ask “is Alice online?” every few seconds.

1. Viewer opens the chat list (or a chat) → **subscribe** to those user ids.
2. Presence service keeps `watchers[target] = {online viewers}`.
3. On a **state change**, fan out only to watchers who are themselves online.

That cuts load three ways:

- Offline friends do not receive or buffer presence.
- Closed chat lists can unsubscribe (this demo subscribes to the full contact list while the “phone” is open — a production app would subscribe to viewport + open chat).
- Heartbeats stay a 1:1 session refresh, complexity O(online users), not O(contacts × users).

Metrics in the demo: `heartbeats` grow continuously; `state_broadcasts` only move on connect/offline.

## Privacy (WhatsApp-shaped)

- `everyone` / `contacts` / `nobody`
- `nobody` hides **online and last seen**
- Reciprocity: if you hide last seen, you do not get others’ last seen
- Enforcement is **server-side** on every view and every push. The client never receives a timestamp it is not allowed to show.

## Minimizing server load (checklist)

1. Heartbeat ≠ event.
2. Multi-device coalescing.
3. Offline debounce.
4. Interest-based pub/sub, not global broadcast.
5. Shard presence by `user_id`. A gateway only talks to the shard that owns that user.
6. Bucket last-seen display (“today at 16:40”) so you can coarsen storage; do not coarsen **online** (that must be session-accurate).
7. Do not persist heartbeats; persist last-seen on transition only.
8. Optional: suppress “online” for users with huge fan-out (broadcast channels / celebrities) or fan out through a tree.

## What we deliberately did not fake

This is not a pixel clone of WhatsApp. It rebuilds the **control plane** the problem statement asks for: heartbeat, session, dead-phone TTL, push-on-change, and privacy. The UI is a lab of five phones so you can *see* those rules.
