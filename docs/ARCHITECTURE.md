# Architecture notes

## Why Open-RMF, and why no simulation

Open-RMF solves the multi-vendor problem. Robots from different brands share
corridors, doors and lifts, and each vendor's fleet manager only knows about
its own robots. RMF keeps one **traffic schedule** that every fleet adapter
writes its planned itineraries into. It resolves conflicts between them and
assigns tasks through **bidding**: every fleet estimates the cost, and the
best one wins.

For all of that, RMF needs a 2D navigation graph and each robot's live pose.
It doesn't need rendered geometry or physics, so a simulation is optional. In
the demos, simulation only replaces real robots.

## Request flow: "send brand_b_1 to station_1"

1. The UI calls `POST /api/robots/brand_b/brand_b_1/go_to {"place":"station_1"}` on the portal.
2. The portal builds a `robot_task_request`: a `compose` task with one
   `go_to_place` phase. It posts that to the api-server at `/tasks/robot_task`
   with a server-side JWT.
3. The api-server forwards the JSON over the ROS 2 `task_api_requests` topic
   and waits for the reply on `task_api_responses`.
4. The `brand_b` fleet adapter accepts the task and plans a path on the nav
   graph. It then negotiates with the traffic schedule, so it may wait for
   brand_a robots.
5. For each path segment, EasyFullControl calls `RobotAdapter.navigate()`.
   That calls the driver, e.g. `POST /robots/brand_b_1/navigate` on the vendor bridge.
6. The adapter polls `driver.get_data()` a few times a second and reports the
   pose and battery to RMF. `is_command_completed()` tells RMF when to send
   the next segment.
7. Fleet and task states flow back to the api-server over
   `ws://…/_internal`. The portal polls `/fleets` and `/tasks` from there.

## Patrol / dispatch across brands

`POST /api/tasks/patrol` without a fleet sends a `dispatch_task_request`. The
dispatcher asks every fleet adapter for a bid. Each adapter prices the task
from its robots' positions, speeds, battery and queue, and the lowest cost
wins. That's how one operator request can land on any brand.

## Components and ports (defaults)

| Component | Port | Source |
|---|---|---|
| Portal | 8080 | `portal/server.py` |
| rmf-web api-server | 8000 | open-rmf/rmf-web `packages/api-server` |
| RMF core + fleet adapters | ROS 2 DDS | `launch/site.launch.xml` |
| Mock robots / vendor bridge | 7001 | `tools/mock_robot_server.py` |

## Why a backend-for-frontend instead of calling the api-server from the browser

- The api-server authenticates with JWTs. Minting or storing those in the browser would leak control of every robot.
- The portal limits operators to a few safe operations and records who asked (`requester`, labels).
- CORS and deployment stay simple: one origin serves the UI and the API.

For more features (task scheduling, alerts, doors/lifts panels), the full
[rmf-web dashboard](https://github.com/open-rmf/rmf-web) can run next to
this portal against the same api-server.

## Known limitations of this starter

- The portal polls every 2 s. The api-server also offers socket.io
  subscriptions (`/fleets/{name}/state`) if you need lower latency.
- There's no teleoperation or manual joystick. RMF is waypoint- and task-based by design.
- `drivers/mir.py` is written from the MiR REST API v2.0.0 docs and hasn't been
  tested on hardware here. Check field names against your robot's API docs.
- Only the Full Control level is implemented. Robots that can only be paused
  (Traffic Light) or only observed (Read Only) need a different adapter; see
  the README's control-level table.
- Multi-floor sites need lifts in the nav graph and a lift adapter. They aren't covered here.
