# Technical defense — PS-14

Use this as a 3–5 minute oral. Point at the running demo while you talk.

## 1. Problem in one sentence

Show contacts a truthful **online / last seen** bit for millions of flapping connections without polling and without turning heartbeats into a thundering herd.

## 2. Design bets

1. **Session set** is the source of truth for online; last seen is an edge timestamp.
2. **TTL + close** together cover both polite disconnects and dead phones.
3. **Push on state change only**, to **online subscribers only**.
4. **Privacy is a view**, applied on the server before the payload leaves.

## 3. Live proof (do this)

- Two phones connected: Alice goes online on Bob’s list with no HTTP poll of Alice.
- Kill battery: wait ~6s, Bob sees last seen, metrics `ttl_expiries` +1.
- Heartbeat counter keeps rising while `state_broadcasts` stays still if nobody changes state.
- Airplane + instant reopen: no flap (debounce).
- Last seen nobody: Bob cannot see Alice online.

## 4. Questions they will ask

**Why not poll last-seen from Postgres every 5s?**
N viewers × M contacts × 1/5s is a read storm, and it is still stale by up to 5s. Pub/sub is O(state changes).

**Isn’t a 6s TTL too fast / too slow?**
Demo is compressed. Production: pick `H` from radio cost, `T=2–3H`. User-visible “online” may also require the app to be foregrounded; the protocol is the same.

**How do you scale watchers?**
Shard by target user. Each shard owns sessions + watcher set. Gateways hold the actual sockets and subscribe to the shard’s channel.

**What if Redis loses a session key?**
Treat as offline. Presence is **soft state**. Last seen is the durable field. False offline is better than false online.

**Clock skew?**
Use server time for last seen. Heartbeat is “packet arrived”, not the client’s clock.

## 5. What we would add next (honesty)

- Sticky gateway + Redis TTL + Redis pub/sub or NATS
- Viewport subscriptions
- Foreground vs connected (WhatsApp “online” is app-visible, not merely socket-up)
- Auth tokens, not `userId` in the first message
- Load test: 100k fake sessions, measure fan-out vs heartbeat CPU
